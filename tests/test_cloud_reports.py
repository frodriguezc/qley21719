#!/usr/bin/env python3
"""Tests de la extracción de reportes CSPM (scripts/extract_cloud_reports.py): helpers puros +
flujo create/poll/download + el human-gate (DRY-RUN no hace POST; --run sí). Sin red ni pytest:
FakeClient que devuelve (code, text). Corre standalone (como en CI) y bajo pytest.
"""
import json
import re
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scripts.extract_cloud_reports as ecr  # noqa: E402


class FakeClient:
    """Cliente fake: respuestas canónicas por substring de subpath; cuenta GET/POST."""

    def __init__(self, *, conns_oci="", conns_other="", policies="", mandates="",
                 create=(200, '{"reportId":"R1"}'), status_text='{"status":"Completed"}',
                 mandate_create=(200, '{"reportId":"M1"}')):
        self.server = "https://gateway.test"
        self.call_count = 0
        self.mutations = 0
        self.posted = []                       # (subpath, body)
        self._conns_oci = conns_oci
        self._conns_other = conns_other
        self._policies = policies
        self._mandates = mandates
        self._create = create
        self._status = status_text
        self._mandate_create = mandate_create

    def list_cloud_connectors(self, cloud_type, params=None):
        self.call_count += 1
        return (200, self._conns_oci) if self._conns_oci else (200, '{"content":[]}')

    def get(self, subpath, params=None):
        self.call_count += 1
        if "/connectors" in subpath:
            return (200, self._conns_other) if self._conns_other else (200, '{"content":[]}')
        if "/reports/policies" in subpath:
            return (200, self._policies) if self._policies else (200, '{"content":[]}')
        if "/reports/mandates" in subpath:
            return (200, self._mandates) if self._mandates else (200, '{"content":[]}')
        if "/report/assessment/list" in subpath:
            return (200, self._status)
        return (404, "{}")

    def post(self, subpath, body):
        self.mutations += 1
        self.posted.append((subpath, body))
        # enruta por PATH, ignorando la query string: el mandate va a
        # /reports?executionType=RUN_TIME (executionType es @RequestParam, no campo del body).
        if subpath.split("?", 1)[0] == "/reports":
            return self._mandate_create
        return self._create

    def download(self, subpath, out_path, params=None):
        self.call_count += 1
        Path(out_path).write_bytes(b"col1,col2\nFAIL,x\n")
        return 200, 18


_OCI_CONNS = json.dumps({"content": [{"tenantId": "ocid1.tenancy.oc1..aaaa", "connectorId": "C-OCI"}]})
_AWS_CONNS = json.dumps({"content": [{"awsAccountId": "111122223333", "id": "C-AWS"}]})
_POLICIES = json.dumps({"content": [{"policyId": "P1", "name": "CIS OCI Foundation"},
                                    {"policyId": "P2", "name": "OCI Best Practices"}]})
_MANDATES = json.dumps({"content": [{"mandateId": "M-ISO", "name": "ISO/IEC 27001:2022"},
                                    {"mandateId": "M-NIST", "name": "NIST 800-53 Rev5"},
                                    {"mandateId": "M-GDPR", "name": "GDPR"}]})


# ------------------------------------------------------------------ helpers puros

def test_extract_connectors_oci_y_aws():
    oci = ecr._extract_connectors("oci", _OCI_CONNS)
    assert oci == [{"account": "ocid1.tenancy.oc1..aaaa", "connector_id": "C-OCI", "name": ""}]
    aws = ecr._extract_connectors("aws", _AWS_CONNS)
    assert aws[0]["account"] == "111122223333" and aws[0]["connector_id"] == "C-AWS"


def test_extract_connectors_descarta_sin_id():
    txt = json.dumps({"content": [{"tenantId": "ocid1...", "name": "sin connectorId"}]})
    assert ecr._extract_connectors("oci", txt) == []


def test_extract_policies():
    pol = ecr._extract_policies(_POLICIES)
    assert [p["policy_id"] for p in pol] == ["P1", "P2"]


def test_pick_mandate_auto_prefiere_iso():
    m = ecr._pick_mandate(_MANDATES, None)
    assert m["mandate_id"] == "M-ISO"


def test_pick_mandate_hint_nist():
    m = ecr._pick_mandate(_MANDATES, "nist")
    assert m["mandate_id"] == "M-NIST"


def test_pick_mandate_sin_match():
    assert ecr._pick_mandate(json.dumps({"content": [{"mandateId": "X", "name": "PCI-DSS"}]}),
                             "gdpr") is None


def test_assessment_body_shape():
    b = ecr._assessment_body("Ley", "OCI", ["P1", "P2"], ["C-OCI"], ["FAIL"], "csv")
    assert b["cloudType"] == "OCI" and b["policyIds"] == ["P1", "P2"]
    assert b["connectorIds"] == ["C-OCI"] and b["resourceResults"] == ["FAIL"]
    assert b["executionType"] == "RUN_TIME" and b["resourceSummaryInclude"] is True


def test_assessment_format_es_case_sensitive():
    """'CSV' en mayúscula -> 400 'Parameter format has invalid value' (verificado live 2026-08-12).
    El .upper() previo hacía que el CSV NUNCA se pudiera crear."""
    b = ecr._assessment_body("Ley", "OCI", ["P1"], ["C"], ["FAIL"], "csv")
    assert b["format"] == "csv", "csv va en MINÚSCULA"
    b2 = ecr._assessment_body("Ley", "OCI", ["P1"], ["C"], ["FAIL"], "PDF")
    assert b2["format"] == "PDF", "pdf sí va en mayúscula"


def test_assessment_body_incluye_obligatorios_no_documentados():
    """startDate/endDate (ISO-8601 con Z) y query son obligatorios de facto: sin ellos la API
    responde 400, o 500 con NPE en el caso de query."""
    b = ecr._assessment_body("Ley", "OCI", ["P1"], ["C"], ["FAIL"], "csv")
    assert "query" in b, "query es obligatorio (NPE 500 si falta)"
    for k in ("startDate", "endDate"):
        assert k in b, f"{k} es obligatorio"
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", b[k]), \
            f"{k}={b[k]!r} no es ISO-8601 con Z (los demás formatos dan 400)"


def test_sanitize_report_name_saca_el_punto():
    """Qualys rechaza el reportName con cualquier char fuera de alfanumérico y _-'() (422).
    El punto de 'Ley 21.719' hacía fallar SIEMPRE el nombre por defecto del pack."""
    assert ecr.sanitize_report_name("Ley 21.719 - Sinacofi") == "Ley 21719 - Sinacofi"
    assert ecr.sanitize_report_name("a/b:c*d") == "abcd"
    assert ecr.sanitize_report_name("O'Higgins (prod)") == "O'Higgins (prod)"
    assert ecr.sanitize_report_name("...") == "Reporte"          # no deja el nombre vacío


def test_assessment_body_sanitiza_el_nombre():
    b = ecr._assessment_body("Ley 21.719 - Sinacofi", "OCI", ["P1"], ["C"], ["FAIL"], "csv")
    assert "." not in b["reportName"] and b["reportName"] == "Ley 21719 - Sinacofi"


def test_first_uuid_prefiere_el_uuid_sobre_el_id_numerico():
    """connectorIds deserializa como java.util.UUID: el id numérico da 400 (verificado live).
    La Connector Mgmt 3.0 devuelve AMBOS, así que se elige por forma."""
    item = {"connectorId": "1789081", "id": 1789081,
            "connectorUuid": "de91d5d6-7f4b-400c-bd70-def653fc8c25",
            "uuid": "de91d5d6-7f4b-400c-bd70-def653fc8c25"}
    got = ecr._first_uuid(item, ecr._CONNECTOR_ID_KEYS)
    assert got == "de91d5d6-7f4b-400c-bd70-def653fc8c25"
    # si el provider ya trae el UUID en connectorId, sirve igual
    assert ecr._first_uuid({"connectorId": "de91d5d6-7f4b-400c-bd70-def653fc8c25"},
                           ecr._CONNECTOR_ID_KEYS) == "de91d5d6-7f4b-400c-bd70-def653fc8c25"
    # sin ningún UUID -> fallback al orden normal (mejor mandar algo y ver el error real)
    assert ecr._first_uuid({"connectorId": "1789081"}, ecr._CONNECTOR_ID_KEYS) == "1789081"


def test_parse_report_id_acepta_uuid_pelado():
    """`create` devuelve el UUID PELADO, no JSON. Asumir JSON perdía el reportId en silencio:
    el reporte quedaba creado en el tenant y la corrida lo daba por fallido."""
    assert ecr._parse_report_id("b42c0530-9606-11f1-a3b5-57fd45169430") == \
        "b42c0530-9606-11f1-a3b5-57fd45169430"
    assert ecr._parse_report_id('"b42c0530-9606-11f1-a3b5-57fd45169430"') == \
        "b42c0530-9606-11f1-a3b5-57fd45169430"
    assert ecr._parse_report_id('{"reportId":"X-1"}') == "X-1"    # mandate sí devuelve JSON
    assert ecr._parse_report_id("") is None


def test_mandate_body_shape():
    b = ecr._mandate_body("Ley afín", "OCI", "M-ISO", ["P1"], ["C-OCI"])
    assert b["type"] == "MANDATE" and b["mandateId"] == "M-ISO" and b["format"] == "PDF"
    assert b["policies"] == [{"cloudType": "OCI", "policyId": "P1"}]


def test_mandate_type_es_MANDATE_no_MANDATE_BASED():
    """'MANDATE_BASED' no existe: 422 con `known type ids = [CreateReportRequest, MANDATE, POLICY]`
    (verificado live 2026-08-12)."""
    b = ecr._mandate_body("Ley", "OCI", "M-ISO", ["P1"], ["C"])
    assert b["type"] == "MANDATE"


def test_mandate_pdf_usa_una_sola_policy():
    """El PDF admite UNA policy (misma restricción que el assessment PDF): se toma la primera."""
    b = ecr._mandate_body("Ley", "OCI", "M-ISO", ["P1", "P2", "P3"], ["C"])
    assert b["policies"] == [{"cloudType": "OCI", "policyId": "P1"}]


def test_mandate_manda_executionType_como_query_param():
    """`executionType` es @RequestParam: en el body da 400 'Required request parameter
    executionType ... is not present'. Tiene que ir en la query string."""
    import logging
    log = logging.getLogger("test-mandate")
    log.addHandler(logging.NullHandler())
    fake = FakeClient(mandate_create=(201, '{"reportId":"M-1"}'))
    r = ecr._run_mandate(fake, ecr._mandate_body("Ley", "OCI", "M-ISO", ["P1"], ["C"]),
                         "oci/x", log)
    assert r["ok"] is True and r["report_id"] == "M-1"
    path = fake.posted[0][0]
    assert "executionType=RUN_TIME" in path, f"executionType debe ir en la query: {path!r}"
    assert "executionType" not in fake.posted[0][1], "y NO en el body"


def test_status_of_variantes():
    assert ecr._status_of('{"status":"Completed"}') == "completed"
    assert ecr._status_of('{"content":[{"status":"Processing"}]}') == "processing"
    assert ecr._status_of("no-json") == ""


def test_status_of_lee_la_envoltura_data():
    """assessment/list envuelve en `data`, NO en el `content` de Spring. Leer solo `content`
    devolvía "" para siempre y el poll moría por timeout con el reporte YA Completed."""
    assert ecr._status_of('{"data":[{"status":"ACCEPTED"}],"count":1}') == "accepted"
    assert ecr._status_of('{"data":[{"status":"COMPLETED"}],"count":1}') == "completed"
    assert ecr._status_of('{"data":[],"count":0}') == ""


# ------------------------------------------------------- flujo create/poll/download

def test_run_assessment_happy_path():
    c = FakeClient()
    with tempfile.TemporaryDirectory() as d:
        r = _run_assessment_silent(c, {"x": 1}, Path(d), "oci/acct", "csv")
        assert Path(r["path"]).exists()          # dentro del with: el tempdir aún vive
    assert r["ok"] and r["report_id"] == "R1" and r["bytes"] == 18
    assert c.mutations == 1


def test_run_assessment_create_falla_no_pollea():
    c = FakeClient(create=(400, '{"message":"bad"}'))
    with tempfile.TemporaryDirectory() as d:
        r = _run_assessment_silent(c, {"x": 1}, Path(d), "oci/acct", "csv")
    assert r["ok"] is False and r["stage"] == "create" and r["http"] == 400


def test_run_assessment_poll_timeout():
    c = FakeClient(status_text='{"status":"Processing"}')   # nunca Completed
    with tempfile.TemporaryDirectory() as d:
        r = _run_assessment_silent(c, {"x": 1}, Path(d), "oci/acct", "csv",
                                   poll_interval=1, poll_timeout=2)
    assert r["ok"] is False and r["stage"] == "poll-timeout"


# ------------------------------------------------------------- human-gate (main)

def test_main_dry_run_no_postea():
    c = FakeClient(conns_oci=_OCI_CONNS, policies=_POLICIES, mandates=_MANDATES)
    with tempfile.TemporaryDirectory() as d:
        rc = ecr.main(["--provider", "oci", "--out", d], client=c)
    assert rc == 0 and c.mutations == 0          # DRY-RUN: ningún POST


def test_main_run_postea():
    c = FakeClient(conns_oci=_OCI_CONNS, policies=_POLICIES, mandates=_MANDATES)
    with tempfile.TemporaryDirectory() as d:
        rc = ecr.main(["--provider", "oci", "--reports", "assessment", "--out", d,
                       "--poll-interval", "0", "--create-gap", "0", "--run"], client=c)
    assert rc == 0 and c.mutations >= 1          # RUN: al menos un create


def test_main_run_oci_fallback_no_crashea():
    # assessment create devuelve 4xx (p.ej. OCI no soportado en reportes) -> no crashea, sigue
    c = FakeClient(conns_oci=_OCI_CONNS, policies=_POLICIES, create=(400, '{"message":"OCI n/a"}'))
    with tempfile.TemporaryDirectory() as d:
        rc = ecr.main(["--provider", "oci", "--reports", "assessment", "--out", d,
                       "--poll-interval", "0", "--create-gap", "0", "--run"], client=c)
    assert rc == 0 and c.mutations == 1          # intentó el create, lo reportó, no abortó


# ------------------------------------------------------------- multi-tenant (TENANT)

def test_tenant_env_carga_envfile():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / ".env.demo").write_text(
            "QUALYS_POD=US03\nQUALYS_API_USER=u\nQUALYS_API_PASSWORD=p\n")
        slug, vals = ecr._tenant_env("demo", d)
    assert slug == "demo"
    assert vals == {"QUALYS_POD": "US03", "QUALYS_API_USER": "u", "QUALYS_API_PASSWORD": "p"}


def test_tenant_env_sin_archivo():
    with tempfile.TemporaryDirectory() as d:
        slug, vals = ecr._tenant_env("sinacofi", d)
    assert slug == "sinacofi" and vals == {}


def test_tenant_env_sin_tenant():
    assert ecr._tenant_env("", "/tmp") == (None, {})


def test_main_tenant_aisla_salida():
    c = FakeClient(conns_oci=_OCI_CONNS, policies=_POLICIES, mandates=_MANDATES)
    saved = os.environ.get("TENANT")
    try:
        os.environ["TENANT"] = "demo"
        with tempfile.TemporaryDirectory() as d:
            rc = ecr.main(["--provider", "oci", "--out", d], client=c)
            assert rc == 0 and (Path(d) / "demo").is_dir()   # <out>/<slug>/<run_id>
    finally:
        if saved is None:
            os.environ.pop("TENANT", None)
        else:
            os.environ["TENANT"] = saved


# helper: corre _run_assessment con sleep no-op y un log mudo
def _run_assessment_silent(c, body, out_dir, label, fmt, poll_interval=0, poll_timeout=10):
    import logging
    log = logging.getLogger("test-silent")
    log.addHandler(logging.NullHandler())
    return ecr._run_assessment(c, body, out_dir, label, fmt, poll_interval, poll_timeout, log,
                               sleep=lambda _s: None)


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e or 'assertion'}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERR   {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
