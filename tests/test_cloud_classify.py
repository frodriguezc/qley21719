#!/usr/bin/env python3
"""Unit tests (sin red) de la clasificación cloud y el emit del pack CSPM.

Corre standalone:  .venv/bin/python tests/test_cloud_classify.py
Usa el spec REAL (mapping/ley21719-cloud.yaml) para `classify_control` y prueba los parsers
defensivos + build_pack en un tmp. No toca el tenant.
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud_pack.generator import (  # noqa: E402
    _html_to_text, build_pack, classify_control, load_spec, parse_controls, parse_evaluations,
    parse_resource_counts,
)

SPEC = load_spec()


def test_classify_each_family():
    cases = {
        "Ensure MFA is enabled for the root account": "acceso",
        "Ensure S3 bucket default encryption with KMS": "cifrado",
        "Ensure CloudTrail is enabled in all regions": "auditoria",
        "Ensure no security group allows 0.0.0.0/0 to port 22": "hardening",
        "Ensure RDS automated backups are enabled": "disponibilidad",
    }
    for name, fam in cases.items():
        got, route = classify_control(name, SPEC)
        assert got == fam, f"{name!r} -> {got} (esperaba {fam})"
        assert route == "keyword"


def test_classify_precedence_first_family_wins():
    # contiene keyword de acceso (iam/password) Y de cifrado (encryption) -> gana acceso (orden).
    got, route = classify_control("Ensure IAM password policy enforces encryption", SPEC)
    assert got == "acceso" and route == "keyword"


def test_classify_priority_log_alert_beats_identity():
    # compuesto de detección/registro que menciona role/IAM/MFA -> gana auditoria (priority),
    # no acceso (validado live: GCP 52013/52017, AWS monitoring 27-40).
    for name in (
        "Ensure log metric filter and alerts exists for Custom Role changes",
        "Ensure log metric filter and alerts exists for Cloud Storage IAM permission changes",
        "Ensure management console sign-in without multi-factor authentication (MFA) is monitored",
        "Ensure IAM policy changes are monitored",
    ):
        got, route = classify_control(name, SPEC)
        assert (got, route) == ("auditoria", "priority"), f"{name!r} -> {got}/{route}"


def test_classify_default_family_for_unmatched():
    got, route = classify_control("Ensure some unrelated tuning parameter is set", SPEC)
    assert got == "hardening" and route == "default"


def test_classify_uses_benchmark_type_too():
    # nombre sin keyword, pero benchmarkType aporta 'encryption' -> cifrado
    got, route = classify_control("Ensure setting 42", SPEC, benchmark_type="Encryption baseline")
    assert got == "cifrado" and route == "keyword"


def test_classify_real_cspm_controls_route_to_correct_family():
    """Bloquea el mapeo de los controles CSPM reales que antes caían en default->hardening.
    Cada caso = un control.name real (AWS/GCP) -> familia esperada. Si una edición futura del
    spec rompe un keyword, este test lo caza. Veredicto del mapeo Ley 21.719 (jun-2026)."""
    cases = {
        # acceso — identidad / credenciales / exposición por permisos
        "S3 Bucket Policy Grant Access to Everyone": "acceso",
        "Ensure there are no API keys associated with your Google Cloud Platform": "acceso",
        "Ensure AWS Route 53 Registered domain has Transfer lock enabled": "acceso",
        "Ensure Lambda function does not have Cross-Account Access": "acceso",
        # cifrado — TLS / gestión de llaves y secretos
        "Ensure Relational Database Service (RDS) instances certificates are rotated": "cifrado",
        "Ensure secrets should be auto rotated after not more than 90 days": "cifrado",
        "Ensure S3 Bucket Policy is set to deny HTTP requests": "cifrado",
        # auditoria — detección / inventario / respuesta a incidentes
        "Ensure AWS Security Hub is enabled in all regions": "auditoria",
        "Ensure GuardDuty is enabled to specific org/region": "auditoria",
        "Ensure image Scanning on push is enabled for ECR Repositories": "auditoria",
        "Ensure Cloud Asset Inventory Is Enabled": "auditoria",
        "Ensure security contact information is registered": "auditoria",
        # disponibilidad — continuidad / anti-pérdida
        "Ensure Deletion Protection is enabled for Relational Database Service": "disponibilidad",
        "Ensure that AWS Lambda function is configured for function-level concurrent execution limit": "disponibilidad",
        "Ensure backtracking is enabled for AWS Aurora MySQL clusters": "disponibilidad",
        # hardening — baseline de config / reducción de superficie
        "Ensure that DNSSEC Signing is enabled for Route 53 Hosted Zones": "hardening",
        "Ensure that EC2 Metadata Service only allows IMDSv2": "hardening",
        # no_aplica — performance / costo / housekeeping (sin obligación de seguridad de datos)
        "Ensure that Lambda function has tracing enabled": "no_aplica",
        "Ensure that EC2 is EBS optimized": "no_aplica",
        "Ensure that Images (AMIs) are not unused more than 90 days": "no_aplica",
        "Ensure that Multiple Triggers are not configured in $Latest Lambda Function": "no_aplica",
    }
    for name, fam in cases.items():
        got, route = classify_control(name, SPEC)
        assert got == fam, f"{name!r} -> {got} (esperaba {fam})"
        assert route == "keyword", f"{name!r} -> route {route} (esperaba keyword, no default)"


def test_no_aplica_family_exists_without_law_articles():
    # el bucket no_aplica es una 6a 'familia' de exclusión: existe pero NO traza a artículos legales
    fam = next((f for f in SPEC["families"] if f["id"] == "no_aplica"), None)
    assert fam is not None, "falta la familia no_aplica en el spec"
    assert not fam.get("law_articles"), "no_aplica NO debe tener law_articles (no es familia legal)"
    assert not fam.get("default_family"), "no_aplica NO debe ser default_family (lo es hardening)"


def test_parse_controls_tolerates_shapes():
    # shape real de controls/metadata/list: lista bajo 'control', serviceType, policyNames (verificado live)
    meta = {"control": [{"cid": 1, "controlName": "Ensure MFA on IAM users", "criticality": "HIGH",
                         "serviceType": "IAM", "provider": "AWS",
                         "policyNames": ["CIS Amazon Web Services Foundations Benchmark"]}]}
    rows = parse_controls(meta)
    assert rows[0]["cid"] == "1" and rows[0]["service"] == "IAM" and rows[0]["provider"] == "AWS"
    assert "CIS Amazon" in rows[0]["benchmark"]
    # shape de evaluations: 'content' + controlId/service (autosuficiente para derivar controles)
    ev = {"content": [{"controlId": 7, "controlName": "x", "service": "KMS", "result": "FAIL"}]}
    rows2 = parse_controls(ev)
    assert rows2[0]["cid"] == "7" and rows2[0]["service"] == "KMS"
    # lista plana + json string
    rows3 = parse_controls(json.dumps([{"cid": 9, "name": "y"}]))
    assert rows3[0]["cid"] == "9" and rows3[0]["name"] == "y"


def test_parse_evaluations_normalizes_result():
    blob = {"content": [{"controlId": 11, "result": "fail"}, {"cid": 12, "controlResult": "PASS"}]}
    posture = parse_evaluations(blob)
    assert posture == {"11": "FAIL", "12": "PASS"}


def test_parse_resource_counts_from_evaluations():
    """Las evaluations traen el desglose por recurso (shape live OCI 2026-08-12)."""
    blob = {"content": [
        {"controlId": 40003, "result": "FAIL", "failedResources": 1,
         "passedResources": 19, "passWithExceptionResources": 0},
        {"controlId": 40102, "result": "FAIL", "failedResources": 3,
         "passedResources": 0, "passWithExceptionResources": 2},
        {"controlId": 999, "result": "PASS"},          # provider sin el desglose -> fuera del dict
    ]}
    counts = parse_resource_counts(blob)
    assert counts["40003"] == {"failed": 1, "passed": 19,
                               "pass_with_exception": 0, "total": 20}
    assert counts["40102"]["total"] == 5               # 3 + 0 + 2
    assert "999" not in counts, "sin campos de recurso NO se inventa un 0"


def test_parse_resource_counts_tolerates_garbage():
    blob = [{"controlId": 1, "failedResources": "2", "passedResources": None},
            {"controlId": 2, "failedResources": "n/a"},
            {"no_cid": True}]
    counts = parse_resource_counts(blob)
    assert counts["1"] == {"failed": 2, "passed": None,
                           "pass_with_exception": None, "total": 2}
    assert "2" not in counts                           # "n/a" -> None -> nada utilizable


def test_build_pack_resource_columns():
    """Las columnas de alcance permiten priorizar entre FAILs sin llamadas extra."""
    controls = [{"cid": "40003", "name": "Ensure no Object Storage buckets are publicly visible",
                 "criticality": "HIGH", "benchmark": "CIS"},
                {"cid": "40102", "name": "Ensure no resources in root compartment",
                 "criticality": "HIGH", "benchmark": "CIS"}]
    posture = {"40003": "FAIL", "40102": "FAIL"}
    counts = {"40003": {"failed": 1, "passed": 19, "pass_with_exception": 0, "total": 20}}
    with tempfile.TemporaryDirectory() as d:
        build_pack(controls, posture, SPEC, d, provider="oci", account="ocid1..x",
                   resource_counts=counts)
        rows = list(csv.DictReader((Path(d) / "fails.csv").open(encoding="utf-8")))
        by_cid = {r["cid"]: r for r in rows}
        assert by_cid["40003"]["failed_resources"] == "1"
        assert by_cid["40003"]["total_resources"] == "20"
        assert by_cid["40003"]["resource_summary"] == "1 de 20 recursos"
        # sin datos -> columnas en blanco (fail-soft), NO ceros que mientan sobre el alcance
        assert by_cid["40102"]["failed_resources"] == ""
        assert by_cid["40102"]["resource_summary"] == ""


def test_build_pack_resource_summary_marks_exceptions():
    controls = [{"cid": "7", "name": "Ensure MFA on root account", "criticality": "HIGH"}]
    counts = {"7": {"failed": 2, "passed": 5, "pass_with_exception": 3, "total": 10}}
    with tempfile.TemporaryDirectory() as d:
        build_pack(controls, {"7": "FAIL"}, SPEC, d, provider="aws", account="a",
                   resource_counts=counts)
        row = next(csv.DictReader((Path(d) / "fails.csv").open(encoding="utf-8")))
        assert row["resource_summary"] == "2 de 10 recursos · 3 con excepción"


def test_build_pack_emits_artifacts_and_stats():
    controls = [
        {"cid": "11", "name": "Ensure MFA on root account", "criticality": "HIGH", "benchmark": "CIS"},
        {"cid": "20", "name": "Ensure S3 encryption with KMS", "criticality": "HIGH", "benchmark": "CIS"},
        {"cid": "30", "name": "Ensure unrelated thing", "criticality": "LOW", "benchmark": "CIS"},
    ]
    posture = {"11": "FAIL", "20": "PASS"}  # 30 sin evaluar
    with tempfile.TemporaryDirectory() as d:
        stats = build_pack(controls, posture, SPEC, d, provider="aws", account="acct1")
        for fn in ("mapping.csv", "fails.csv", "gaps.md", "apply-instructions.md", "summary.json"):
            assert (Path(d) / fn).exists(), f"falta {fn}"
        assert stats["controls"] == 3
        assert stats["classified"] == 3            # 11->acceso, 20->cifrado, 30->default(hardening)
        assert stats["fails"] == 1                 # solo el 11
        assert stats["gaps"] == 1                  # el 30 cae en default -> revisar
        assert stats["by_family"]["acceso"] == 1
        assert stats["by_family"]["cifrado"] == 1
        assert stats["no_policy_xml"] is True and stats["no_mutation"] is True
        # el FAIL aparece en fails.csv
        fails = (Path(d) / "fails.csv").read_text(encoding="utf-8")
        assert "11" in fails and "PASS" not in fails.split("\n", 1)[1]


def test_apply_instructions_report_api_section_aws():
    # El emisor agrega la subsección §3b (curl de reporte por API) provider-aware, sin credenciales.
    with tempfile.TemporaryDirectory() as d:
        build_pack([], {}, SPEC, d, provider="aws", account="687245677417")
        md = (Path(d) / "apply-instructions.md").read_text(encoding="utf-8")
    # la subsección y los endpoints verificados (assessment = POST verificado)
    assert "## 3b" in md
    assert "report/assessment/create" in md and "report/assessment/list" in md
    assert "/download?reportFormat=csv" in md
    assert "/cloudview-api/rest/v1/reports/mandates" in md      # mandate report
    # provider-aware: sustituye PROVIDER/CLOUD_TYPE/SCOPE_ID del scope
    assert "PROVIDER=aws CLOUD_TYPE=AWS SCOPE_ID=687245677417" in md
    # host + auth correctos (API Gateway + JWT Bearer, NO el FO ni el portal con Basic) + permiso
    assert "gateway.<seg>.apps.qualys.com" in md
    assert "Authorization: Bearer" in md and "POST /auth" in md
    assert "HTTP Basic" not in md                               # auth stale corregida
    assert "extract_cloud_reports.py" in md                     # puntero a la automatización
    assert "Reporting Permission" in md
    # human-gate / read-only honesto: es POST = mutación que corre el cliente
    assert "MUTACIÓN" in md and "la herramienta NO lo" in md
    # SIN credenciales en claro: solo placeholders por entorno
    assert "$QUALYS_API_PASSWORD" in md
    assert "QUALYS_API_PASSWORD=" not in md                     # nunca un valor asignado en claro
    # citas a doc público (no paths locales)
    assert "docs.qualys.com/en/cloudview/latest/reports/" in md


def test_apply_instructions_oci_confirmed_and_mandate_download_console():
    # OCI: el audit (jun-2026) lo CONFIRMÓ por API (cloudType=OCI aceptado) -> la doc lo marca
    # confirmado, NO "no ejercitado"; y el único paso sin API es la descarga del Mandate (consola,
    # todos los providers).
    with tempfile.TemporaryDirectory() as d:
        build_pack([], {}, SPEC, d, provider="oci", account="ocid1.tenancy.oc1..xxxx")
        md = (Path(d) / "apply-instructions.md").read_text(encoding="utf-8")
    assert "PROVIDER=oci CLOUD_TYPE=OCI" in md
    assert "OCI confirmado por API" in md                       # área 2: confirmado por el audit
    assert "smoke no lo ejercitó" not in md                     # caveat viejo (pesimista) removido
    assert "Descarga del Mandate Report = solo consola" in md   # área 1: download del mandate sin API


def test_html_to_text_strips_and_joins():
    h = "<p><strong>Hacer:</strong></p> <ol> <li>Paso&nbsp;uno</li> <li>Paso dos</li> </ol>"
    t = _html_to_text(h)
    assert "<" not in t and ">" not in t          # sin tags
    assert "Paso uno" in t and "Paso dos" in t     # texto preservado (&nbsp; -> espacio)
    assert "·" in t                                # los <li> quedan separados
    assert _html_to_text("") == "" and _html_to_text(None) == ""


def test_build_pack_remediation_rationale_references_columns():
    controls = [{"cid": "40001", "name": "Ensure Secure Boot", "criticality": "MEDIUM", "benchmark": "CIS OCI"}]
    posture = {"40001": "FAIL"}
    control_meta = {"40001": {"remediation": "OCI Console: enable Secure Boot at instance creation",
                              "rationale": "Protege contra bootkits y firmware no firmado",
                              "references": "CIS OCI Foundations: Recommendation 4.x"}}
    with tempfile.TemporaryDirectory() as d:
        build_pack(controls, posture, SPEC, d, provider="oci", account="ocid1..x", control_meta=control_meta)
        m = (Path(d) / "mapping.csv").read_text(encoding="utf-8")
        f = (Path(d) / "fails.csv").read_text(encoding="utf-8")
    header = m.splitlines()[0]
    assert "remediation" in header and "rationale" in header and "references" in header
    assert "enable Secure Boot at instance creation" in m
    assert "Protege contra bootkits" in m and "Recommendation 4.x" in m
    assert "enable Secure Boot at instance creation" in f and "Recommendation 4.x" in f  # fails.csv idem


def test_build_pack_control_meta_optional():
    # sin control_meta -> las columnas existen pero vacías (fail-soft, no rompe)
    with tempfile.TemporaryDirectory() as d:
        build_pack([{"cid": "1", "name": "X", "benchmark": "b"}], {"1": "FAIL"}, SPEC, d)
        header = (Path(d) / "mapping.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "remediation" in header and "rationale" in header and "references" in header


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
