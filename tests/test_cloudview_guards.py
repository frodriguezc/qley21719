#!/usr/bin/env python3
"""Unit tests (sin red) del gate READ-ONLY allow-list-only del cliente CSPM CloudView.

Corre standalone:  .venv/bin/python tests/test_cloudview_guards.py
Verifica el invariante del motor cloud (DESIGN-cloud-posture.md §6): `cv_get` es
ALLOW-LIST-ONLY (rechaza por defecto). Cubre:
  - paths de lectura enumerados PASAN (controls/metadata, connectors, groups, evaluations,
    evaluation resources/stats, report list/download) — por proveedor y OCI.
  - paths NO enumerados o de MUTACIÓN (connector run/create, report create, qps/rest/3.0)
    se RECHAZAN antes de la red.
  - el cliente NO expone métodos de escritura; la URL se arma sobre cloudview-api/rest/v1.

NO toca la red: `_request` se reemplaza por un grabador que devuelve (200, "") sin enviar.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qualys_client import CloudViewClient, QualysReadOnlyError  # noqa: E402
from qualys_client import cloudview as cv  # noqa: E402


def _client_with_recorder():
    c = CloudViewClient("US03", "u", "p")
    calls = []

    def fake_request(url, params=None, _retry=0):
        calls.append({"url": url, "params": params})

        class _R:
            status_code = 200
            text = ""
            headers: dict = {}
        return _R()

    c._request = fake_request  # type: ignore[assignment]
    return c, calls


ALLOWED = [
    "/controls/metadata/list",
    "/aws/connectors",
    "/aws/connectors/1234",
    "/azure/connectors",
    "/gcp/connectors",
    "/oci/connectors",
    "/groups",
    "/groups/abc-uuid",
    "/aws/evaluations/111122223333",
    "/azure/evaluations/sub-guid",
    "/gcp/evaluations/my-project",
    "/oci/evaluations/",
    "/aws/evaluations/111122223333/resources/42",
    "/aws/evaluations/stats/42/conn-1",
    "/report/assessment/list",
    "/report/assessment/r-99/download",
]

BLOCKED = [
    "/aws/connectors/1234/run",           # disparar connector (mutación)
    "/aws/connectors/create",             # crear connector
    "/report/assessment/create",          # crear reporte (POST)
    "/report/assessment/r-99/rerun",      # re-correr reporte
    "/aws/remediation/activity",          # no enumerado
    "/azure/policy/new",                  # crear policy
    "/controls/custom/create",            # crear custom control
    "/something/totally/unlisted",        # path desconocido
    "/qps/rest/3.0/search/cloudview",     # otro namespace
]


def test_allowed_read_paths_pass():
    c, calls = _client_with_recorder()
    for p in ALLOWED:
        code, _ = c.cv_get(p)
        assert code == 200, f"path de lectura rechazado: {p}"
    assert len(calls) == len(ALLOWED)
    # cada URL se arma sobre el base cloudview-api/rest/v1
    assert all("/cloudview-api/rest/v1" in x["url"] for x in calls)


def test_blocked_paths_raise_before_network():
    c, calls = _client_with_recorder()
    for p in BLOCKED:
        try:
            c.cv_get(p)
            raise AssertionError(f"path NO enumerado/ mutación NO bloqueado: {p}")
        except QualysReadOnlyError:
            pass
    assert calls == [], f"un path bloqueado llegó a la red: {calls}"


def test_full_path_with_base_is_normalized():
    c, calls = _client_with_recorder()
    c.cv_get("/cloudview-api/rest/v1/controls/metadata/list")
    assert calls[0]["url"].endswith("/cloudview-api/rest/v1/controls/metadata/list")
    # y un full-path de mutación sigue bloqueado
    try:
        c.cv_get("/cloudview-api/rest/v1/aws/connectors/1/run")
        raise AssertionError("full-path de mutación no bloqueado")
    except QualysReadOnlyError:
        pass


def test_query_string_ignored_for_matching():
    c, _ = _client_with_recorder()
    code, _ = c.cv_get("/oci/evaluations/?tenantId=ocid1.tenancy.oc1..xxx")
    assert code == 200


def test_helpers_hit_allowed_paths():
    c, calls = _client_with_recorder()
    c.list_controls()
    c.list_connectors("AWS")
    c.list_evaluations("azure", "sub-guid")
    c.list_evaluations("oci", "ocid1.tenancy.oc1..xxx")
    c.list_assessment_reports()
    assert len(calls) == 5
    # OCI evaluations va por query param tenantId, no por path
    oci_call = calls[3]
    assert oci_call["url"].endswith("/oci/evaluations/")
    assert oci_call["params"]["tenantId"].startswith("ocid1.tenancy")


def test_unknown_pod_raises_unless_server_given():
    try:
        CloudViewClient("ZZ99", "u", "p")
        raise AssertionError("POD desconocido no levantó ValueError")
    except ValueError:
        pass
    # con server explícito, no exige POD conocido
    c = CloudViewClient("ZZ99", "u", "p", server="https://gateway.example.test")
    assert c.server == "https://gateway.example.test"


def test_no_write_methods_exposed():
    for forbidden in ["cv_post", "cv_put", "cv_delete", "post", "put", "delete", "patch"]:
        assert not hasattr(CloudViewClient, forbidden), f"método de escritura expuesto: {forbidden}"


def test_base_path_constant():
    assert cv.CV_BASE == "/cloudview-api/rest/v1"


# -- auth gateway+JWT (hermético: se reemplaza la sesión, no toca la red) ------ #
class _FakeResp:
    def __init__(self, status, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class _FakeSession:
    def __init__(self, post_resp, get_resp):
        self.headers: dict = {}
        self._post_resp = post_resp
        self._get_resp = get_resp
        self.posts: list = []
        self.gets: list = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data})
        return self._post_resp

    def get(self, url, params=None, timeout=None):
        self.gets.append({"url": url, "auth": self.headers.get("Authorization")})
        return self._get_resp


def test_gateway_host_for_pod():
    assert CloudViewClient("US03", "u", "p").server == "https://gateway.qg3.apps.qualys.com"
    assert cv.gateway_for("US01") == "https://gateway.qg1.apps.qualys.com"


def test_jwt_auth_flow_sets_bearer_and_targets_auth():
    c = CloudViewClient("US03", "u", "p")
    jwt = "aaa.bbb." + "c" * 50          # 2 puntos + largo -> "parece JWT"
    c.sess = _FakeSession(_FakeResp(201, jwt), _FakeResp(200, "{}"))
    code, _ = c.cv_get("/aws/connectors")
    assert code == 200
    # se autenticó con UN POST a /auth, mandando las credenciales
    assert len(c.sess.posts) == 1 and c.sess.posts[0]["url"].endswith("/auth")
    assert c.sess.posts[0]["data"]["username"] == "u"
    # el GET de datos llevó el Bearer y fue al gateway + cloudview-api
    g = c.sess.gets[0]
    assert g["auth"] == f"Bearer {jwt}"
    assert g["url"] == "https://gateway.qg3.apps.qualys.com/cloudview-api/rest/v1/aws/connectors"


def test_jwt_auth_failure_propagates_and_skips_data_get():
    c = CloudViewClient("US03", "u", "p")
    c.sess = _FakeSession(_FakeResp(401, '{"status":401}'), _FakeResp(200, "{}"))
    code, text = c.cv_get("/aws/connectors")
    assert code == 401              # la auth falló -> se propaga como (401, body)
    assert c.sess.gets == []        # nunca se hizo el GET de datos


def test_list_cloud_connectors_path_and_validation():
    c, calls = _client_with_recorder()
    code, _ = c.list_cloud_connectors("OCI")
    assert code == 200
    # va a la Connector Management API (no cloudview-api), con el TYPE en mayúsculas + /list
    assert calls[0]["url"].endswith("/connectors/v1.0/OCI/list")
    assert calls[0]["params"]["pageNumber"] == 0
    # cloud_type inválido -> rechazo ANTES de la red
    n = len(calls)
    try:
        c.list_cloud_connectors("ORACLE")
        raise AssertionError("cloud_type inválido no rechazado")
    except QualysReadOnlyError:
        pass
    assert len(calls) == n


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
