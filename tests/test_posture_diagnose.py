#!/usr/bin/env python3
"""Tests del diagnóstico de POSTURE de cloud_posture_pack.py: congela los 4 estados
distinguibles (auth / empty / not_evaluated / ok) para que no se vuelvan a colapsar en un
solo "no encontró nada", + la plomería de status que los habilita (_fetch_all_evaluations
devuelve el http_status, _discover_accounts devuelve (accounts, (state, why))).

Sin red ni pytest: usa Fake clients que devuelven (code, text). Corre standalone (como en CI):
    .venv/bin/python tests/test_posture_diagnose.py
y también bajo pytest:  .venv/bin/python -m pytest tests/test_posture_diagnose.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scripts.cloud_posture_pack as cpp  # noqa: E402


class FakeClient:
    """Cliente CSPM falso: cada método devuelve un (code, text) fijo (read-only)."""

    def __init__(self, eval_resp=None, conn_resp=None):
        self._eval = eval_resp        # respuesta de list_evaluations
        self._conn = conn_resp        # respuesta de list_connectors / list_cloud_connectors

    def list_evaluations(self, provider, account, params=None):
        return self._eval

    def list_connectors(self, provider, params=None):
        return self._conn

    def list_cloud_connectors(self, cloud_type, params=None):
        return self._conn


# --------------------------------------------------------------------------------------
# _diagnose_posture — los 4 estados (el corazón del fix)
# --------------------------------------------------------------------------------------

def test_diagnose_auth_401_and_403():
    st, why = cpp._diagnose_posture(401, [{"cid": "1"}], {"1": "PASS"})
    assert st == "auth" and "401" in why
    st2, why2 = cpp._diagnose_posture(403, [], {})
    assert st2 == "auth" and "403" in why2


def test_diagnose_auth_precede_a_los_datos():
    # un 401/403 manda aunque haya controles evaluados: NO es "ok" ni "vacío".
    st, _ = cpp._diagnose_posture(401, [{"cid": "1"}], {"1": "FAIL"})
    assert st == "auth"


def test_diagnose_empty_200_sin_evaluations():
    st, why = cpp._diagnose_posture(200, [], {})
    assert st == "empty" and "sin evaluations" in why


def test_diagnose_not_evaluated_todo_not_evaluated():
    controls = [{"cid": "1"}, {"cid": "2"}]
    posture = {"1": "NOT_EVALUATED", "2": "NOT_EVALUATED"}
    st, why = cpp._diagnose_posture(200, controls, posture)
    assert st == "not_evaluated" and "Run Connector" in why


def test_diagnose_not_evaluated_unknown_y_blank_cuentan_como_no_evaluado():
    controls = [{"cid": "1"}, {"cid": "2"}, {"cid": "3"}]
    posture = {"1": "UNKNOWN", "2": "", "3": "NOT_EVALUATED"}
    st, _ = cpp._diagnose_posture(200, controls, posture)
    assert st == "not_evaluated"


def test_diagnose_ok_con_al_menos_un_pass_o_fail():
    controls = [{"cid": "1"}, {"cid": "2"}]
    st, why = cpp._diagnose_posture(200, controls, {"1": "PASS", "2": "NOT_EVALUATED"})
    assert st == "ok" and "evaluado" in why
    st2, _ = cpp._diagnose_posture(200, controls, {"1": "FAIL", "2": "FAIL"})
    assert st2 == "ok"


# --------------------------------------------------------------------------------------
# _fetch_all_evaluations — propagación del http_status (caso A vs B sin tragar el 401)
# --------------------------------------------------------------------------------------

def test_fetch_auth_devuelve_vacio_y_codigo_sin_abortar():
    items, status = cpp._fetch_all_evaluations(FakeClient(eval_resp=(401, '{"m":"unauth"}')),
                                               "oci", "ocid1.tenancy.oc1..aaaa")
    assert items == [] and status == 401
    items2, status2 = cpp._fetch_all_evaluations(FakeClient(eval_resp=(403, "forbidden")),
                                                 "aws", "111122223333")
    assert items2 == [] and status2 == 403


def test_fetch_ok_con_content():
    page = json.dumps({"content": [{"controlId": 1, "result": "FAIL"}], "last": True})
    items, status = cpp._fetch_all_evaluations(FakeClient(eval_resp=(200, page)),
                                               "aws", "111122223333")
    assert status == 200 and len(items) == 1


def test_fetch_ok_vacio():
    items, status = cpp._fetch_all_evaluations(
        FakeClient(eval_resp=(200, json.dumps({"content": []}))), "aws", "x")
    assert items == [] and status == 200


def test_fetch_5xx_aborta():
    raised = False
    try:
        cpp._fetch_all_evaluations(FakeClient(eval_resp=(500, "boom")), "aws", "x")
    except SystemExit:
        raised = True
    assert raised, "un 5xx (otro non-200) debe abortar con SystemExit vía _http_ok"


# --------------------------------------------------------------------------------------
# _discover_accounts — (accounts, (state, why)): auth/error/ok/empty distinguibles
# --------------------------------------------------------------------------------------

def test_discover_auth():
    accts, (state, _) = cpp._discover_accounts(FakeClient(conn_resp=(401, "{}")), "aws")
    assert accts == [] and state == "auth"


def test_discover_error_non200():
    accts, (state, _) = cpp._discover_accounts(FakeClient(conn_resp=(500, "boom")), "aws")
    assert accts == [] and state == "error"


def test_discover_error_ante_excepcion_del_cliente():
    class Boom:
        def list_connectors(self, provider, params=None):
            raise RuntimeError("network down")

    accts, (state, why) = cpp._discover_accounts(Boom(), "aws")
    assert accts == [] and state == "error" and "RuntimeError" in why


def test_discover_ok_aws():
    body = json.dumps({"content": [{"awsAccountId": "111122223333"}]})
    accts, (state, _) = cpp._discover_accounts(FakeClient(conn_resp=(200, body)), "aws")
    assert accts == ["111122223333"] and state == "ok"


def test_discover_empty_sin_account_extraible():
    body = json.dumps({"content": [{"name": "connector-sin-id"}]})
    accts, (state, _) = cpp._discover_accounts(FakeClient(conn_resp=(200, body)), "aws")
    assert accts == [] and state == "empty"


def test_discover_oci_via_connector_management_api():
    # OCI va por list_cloud_connectors y la clave del tenancy es tenancyId.
    body = json.dumps({"content": [{"tenancyId": "ocid1.tenancy.oc1..aaaa"}]})
    accts, (state, _) = cpp._discover_accounts(FakeClient(conn_resp=(200, body)), "oci")
    assert accts == ["ocid1.tenancy.oc1..aaaa"] and state == "ok"


# --------------------------------------------------------------------------------------
# _run_one — el estado diagnosticado se thread-ea al resultado (integración mínima)
# --------------------------------------------------------------------------------------

def test_run_one_threadea_estado_empty():
    from cloud_pack.generator import load_spec
    with tempfile.TemporaryDirectory() as d:
        r = cpp._run_one(FakeClient(eval_resp=(200, json.dumps({"content": []}))),
                         load_spec(), "aws", "demo", d)
    assert r["posture_state"] == "empty"
    assert r["provider"] == "aws" and r["account"] == "demo"


def test_run_one_threadea_estado_auth():
    from cloud_pack.generator import load_spec
    with tempfile.TemporaryDirectory() as d:
        r = cpp._run_one(FakeClient(eval_resp=(401, "{}")),
                         load_spec(), "oci", "ocid1.tenancy.oc1..aaaa", d)
    assert r["posture_state"] == "auth"


# --------------------------------------------------------------------------------------
# Runner standalone (como en CI: `python tests/test_posture_diagnose.py`, sin pytest)
# --------------------------------------------------------------------------------------

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
