#!/usr/bin/env python3
"""Tests de la extracción de reportes CSPM (scripts/extract_cloud_reports.py): helpers puros +
flujo create/poll/download + el human-gate (DRY-RUN no hace POST; --run sí). Sin red ni pytest:
FakeClient que devuelve (code, text). Corre standalone (como en CI) y bajo pytest.
"""
import json
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
        if subpath == "/reports":
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
    assert b["cloudType"] == "OCI" and b["format"] == "CSV" and b["policyIds"] == ["P1", "P2"]
    assert b["connectorIds"] == ["C-OCI"] and b["resourceResults"] == ["FAIL"]
    assert b["executionType"] == "RUN_TIME" and b["resourceSummaryInclude"] is True


def test_mandate_body_shape():
    b = ecr._mandate_body("Ley afín", "OCI", "M-ISO", ["P1"], ["C-OCI"])
    assert b["type"] == "MANDATE_BASED" and b["mandateId"] == "M-ISO" and b["format"] == "PDF"
    assert b["policies"] == [{"cloudType": "OCI", "policyId": "P1"}]


def test_status_of_variantes():
    assert ecr._status_of('{"status":"Completed"}') == "completed"
    assert ecr._status_of('{"content":[{"status":"Processing"}]}') == "processing"
    assert ecr._status_of("no-json") == ""


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
