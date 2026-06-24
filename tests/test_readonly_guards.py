#!/usr/bin/env python3
"""Unit tests (sin dependencias de red) de los GUARDS read-only del cliente Qualys.

Corre standalone:  .venv/bin/python tests/test_readonly_guards.py
Verifica el INVARIANTE central del proyecto: el cliente solo puede LEER. Cubre:
  - FO: solo list/fetch/count/list_id_range/export pasan; import/add/update/delete/...
    levantan QualysReadOnlyError ANTES de tocar la red.
  - QPS: solo paths con /search/ o /count/; cualquier otro path se bloquea.
  - Validacion de POD (desconocido -> ValueError; conocido -> server correcto; case-insensitive).
  - from_env: precedencia de credenciales y RuntimeError si faltan.
  - _read_dotenv: parser minimo (comentarios, comillas, blancos).

NO toca la red: `_request` se reemplaza por un grabador que devuelve (200, "") sin enviar nada.
Asi cualquier write que se "colara" haria fallar el test al registrar una llamada de red.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import qualys_client.client as qc  # noqa: E402
from qualys_client import QualysClient, QualysReadOnlyError, from_env  # noqa: E402


def _client_with_recorder():
    """Cliente con `_request` instrumentado: graba (method,url,params) y no toca la red."""
    c = QualysClient("US03", "u", "p")
    calls = []

    def fake_request(method, url, params=None, data=None, headers=None, _retry=0):
        calls.append({"method": method, "url": url, "params": params, "data": data})

        class _R:  # respuesta minima que imita requests.Response
            status_code = 200
            text = ""
            headers: dict = {}
        return _R()

    c._request = fake_request  # type: ignore[assignment]
    return c, calls


# --- FO: acciones de escritura bloqueadas antes de la red ------------------ #
def test_fo_write_actions_blocked_before_network():
    c, calls = _client_with_recorder()
    for act in ["import", "add", "update", "delete", "edit", "set", "purge", ""]:
        try:
            c.fo_get("/api/4.0/fo/compliance/policy/", {"action": act})
            raise AssertionError(f"action={act!r} NO fue bloqueada")
        except QualysReadOnlyError:
            pass
    assert calls == [], f"un write llego a la red: {calls}"


def test_fo_read_actions_pass_guard():
    c, calls = _client_with_recorder()
    for act in sorted(qc.READ_FO_ACTIONS):
        code, _ = c.fo_get("/api/2.0/fo/asset/host/", {"action": act})
        assert code == 200
    # todas las lecturas deben haber llegado al transporte (una por accion)
    assert len(calls) == len(qc.READ_FO_ACTIONS)
    assert all(x["method"] == "GET" for x in calls)


def test_fo_action_passes_params_through():
    c, calls = _client_with_recorder()
    c.fo_get("/api/2.0/fo/asset/host/", {"action": "list", "truncation_limit": "3"})
    assert calls[0]["params"]["action"] == "list"
    assert calls[0]["params"]["truncation_limit"] == "3"
    assert calls[0]["url"].endswith("/api/2.0/fo/asset/host/")


# --- QPS: solo /search/ o /count/ ------------------------------------------ #
def test_qps_non_search_paths_blocked():
    c, calls = _client_with_recorder()
    for path in [
        "/qps/rest/2.0/create/am/tag",
        "/qps/rest/2.0/update/am/asset",
        "/qps/rest/2.0/delete/am/hostasset",
        "/qps/rest/2.0/activate/am/asset",
    ]:
        try:
            c.qps_search(path)
            raise AssertionError(f"path={path!r} NO fue bloqueado")
        except QualysReadOnlyError:
            pass
    assert calls == [], f"un QPS de escritura llego a la red: {calls}"


def test_qps_search_and_count_paths_pass():
    c, calls = _client_with_recorder()
    c.qps_search("/qps/rest/2.0/search/am/hostasset", limit=3)
    c.qps_search("/qps/rest/2.0/count/am/hostasset")
    assert len(calls) == 2
    assert all(x["method"] == "POST" for x in calls)


def test_qps_body_builder_escapes_and_limits():
    # El body es XML; valores con caracteres especiales deben escaparse, y el limit aparece.
    body = QualysClient._qps_body(limit=5, criteria=[{"field": "name", "operator": "CONTAINS", "value": "a & b <x>"}])
    assert "<limitResults>5</limitResults>" in body
    assert "&amp;" in body and "&lt;x&gt;" in body
    assert "<ServiceRequest>" in body
    # criterio con value vacio se omite
    assert QualysClient._qps_body(criteria=[{"field": "f", "operator": "EQ", "value": ""}]) == ""


def test_qps_body_start_from_id_cursor():
    # El cursor de paginacion emite <startFromId> junto al <limitResults>, dentro de <preferences>.
    body = QualysClient._qps_body(limit=500, start_from_id=42)
    assert "<startFromId>42</startFromId>" in body
    assert "<limitResults>500</limitResults>" in body
    assert body.index("<startFromId>") < body.index("<limitResults>")
    assert "<preferences>" in body
    # sin cursor: no aparece startFromId (retrocompat)
    assert "<startFromId>" not in QualysClient._qps_body(limit=10)


# --- Backoff: honra el wait que pide el server (sin truncar a 60s) --------- #
class _Resp:
    """Response minima con headers para probar el calculo de backoff (sin red, sin sleep)."""
    def __init__(self, status=429, headers=None):
        self.status_code = status
        self.headers = headers or {}


def test_retry_after_honors_long_server_wait():
    # Antes se truncaba a 60s; ahora honra hasta _MAX_BACKOFF_SEC (300s) lo que pide Qualys.
    assert qc._retry_after_seconds(_Resp(headers={"X-RateLimit-ToWait-Sec": "300"}), 0) == 300
    assert qc._retry_after_seconds(_Resp(headers={"Retry-After": "120"}), 0) == 120


def test_retry_after_caps_at_max_backoff():
    # Un valor absurdo del server no debe dormir indefinido: se acota al techo.
    assert qc._retry_after_seconds(_Resp(headers={"X-RateLimit-ToWait-Sec": "99999"}), 0) == qc._MAX_BACKOFF_SEC


def test_retry_after_linear_when_no_header():
    # Sin header de espera -> backoff lineal 15*(retry+1).
    assert qc._retry_after_seconds(_Resp(headers={}), 0) == 15
    assert qc._retry_after_seconds(_Resp(headers={}), 2) == 45


def test_throttle_note_distinguishes_concurrency():
    # 409 o (429 con running>=limit) -> concurrency; nunca incluye credenciales.
    note_409 = qc._throttle_note(_Resp(status=409, headers={}))
    assert "concurrency" in note_409
    note_sat = qc._throttle_note(_Resp(status=429, headers={
        "X-Concurrency-Limit-Running": "10", "X-Concurrency-Limit-Limit": "10"}))
    assert "concurrency" in note_sat
    note_rate = qc._throttle_note(_Resp(status=429, headers={"X-RateLimit-Remaining": "0"}))
    assert "rate" in note_rate


# --- on_throttle: persistencia del backoff (p.ej. run.log), sin dormir de verdad ---------- #
def _client_with_fake_session(responses):
    """Cliente cuyo `sess.request` devuelve `responses` en orden y cuyo `time.sleep` no duerme.
    Devuelve (cliente, restore); llamar restore() para deshacer el patch de sleep."""
    c = QualysClient("US03", "alice", "s3cr3t")
    seq = list(responses)
    c.sess.request = lambda *a, **k: seq.pop(0)  # type: ignore[assignment]
    orig_sleep = qc.time.sleep
    qc.time.sleep = lambda _s: None
    return c, (lambda: setattr(qc.time, "sleep", orig_sleep))


def test_on_throttle_fires_and_is_secret_safe():
    # 429 (server pide 7s) -> backoff -> 200: on_throttle recibe UNA linea secret-safe.
    c, restore = _client_with_fake_session(
        [_Resp(429, {"X-RateLimit-ToWait-Sec": "7"}), _Resp(200)])
    notes = []
    c.on_throttle = notes.append
    try:
        resp = c._request("GET", "https://x/api")
    finally:
        restore()
    assert resp.status_code == 200
    assert c.call_count == 2                       # 429 + 200 (ambas contadas)
    assert len(notes) == 1, notes
    assert "sleep 7s" in notes[0] and "retry 1/" in notes[0]
    assert "s3cr3t" not in notes[0] and "alice" not in notes[0]  # jamas credenciales


def test_on_throttle_absent_does_not_crash():
    # Sin sink (default None) un throttle no debe romper el sweep read-only.
    c, restore = _client_with_fake_session([_Resp(409), _Resp(200)])
    assert c.on_throttle is None
    try:
        resp = c._request("GET", "https://x/api")
    finally:
        restore()
    assert resp.status_code == 200


def test_on_throttle_broken_sink_is_swallowed():
    # Un sink que levanta excepcion NO debe abortar el request (el sweep read-only sigue).
    c, restore = _client_with_fake_session([_Resp(429, {"Retry-After": "1"}), _Resp(200)])
    def _boom(_note):
        raise RuntimeError("sink roto")
    c.on_throttle = _boom
    try:
        resp = c._request("GET", "https://x/api")
    finally:
        restore()
    assert resp.status_code == 200


# --- Validacion de POD ----------------------------------------------------- #
def test_unknown_pod_raises():
    try:
        QualysClient("ZZ99", "u", "p")
        raise AssertionError("POD desconocido no levanto ValueError")
    except ValueError:
        pass


def test_known_pod_sets_server_case_insensitive():
    c = QualysClient("us03", "u", "p")
    assert c.pod == "US03"
    assert c.server == qc.PODS["US03"]


def test_no_write_methods_exposed():
    # El cliente no debe exponer metodos de escritura (fo_post/put/delete/...).
    for forbidden in ["fo_post", "fo_put", "fo_delete", "post", "put", "delete"]:
        assert not hasattr(QualysClient, forbidden), f"metodo de escritura expuesto: {forbidden}"


# --- from_env / _read_dotenv ----------------------------------------------- #
def test_read_dotenv_parser():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        p.write_text(
            "# comentario\n"
            "\n"
            'QUALYS_POD="US03"\n'
            "QUALYS_API_USER = alice \n"
            "QUALYS_API_PASSWORD='s3cr3t'\n"
            "MALFORMED_LINE_NO_EQUALS\n",
            encoding="utf-8",
        )
        env = qc._read_dotenv(p)
        assert env["QUALYS_POD"] == "US03"
        assert env["QUALYS_API_USER"] == "alice"
        assert env["QUALYS_API_PASSWORD"] == "s3cr3t"
        assert "MALFORMED_LINE_NO_EQUALS" not in env
    # archivo inexistente -> dict vacio (no crashea)
    assert qc._read_dotenv(Path(d) / "nope.env") == {}


def test_from_env_uses_environment():
    saved = {k: os.environ.get(k) for k in ("QUALYS_POD", "QUALYS_API_USER", "QUALYS_API_PASSWORD")}
    try:
        os.environ["QUALYS_POD"] = "EU01"
        os.environ["QUALYS_API_USER"] = "u"
        os.environ["QUALYS_API_PASSWORD"] = "p"
        c = from_env()
        assert c.pod == "EU01"
        assert c.server == qc.PODS["EU01"]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_from_env_missing_credentials_raises():
    saved = {k: os.environ.get(k) for k in ("QUALYS_POD", "QUALYS_API_USER", "QUALYS_API_PASSWORD")}
    orig_dotenv = qc._read_dotenv
    try:
        for k in saved:
            os.environ.pop(k, None)
        qc._read_dotenv = lambda _p: {}  # neutraliza un .env local (gitignored)
        try:
            from_env()
            raise AssertionError("from_env no levanto RuntimeError sin credenciales")
        except RuntimeError:
            pass
    finally:
        qc._read_dotenv = orig_dotenv
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


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
        except Exception as e:  # noqa: BLE001 — un error inesperado tambien es fallo
            failed += 1
            print(f"ERR   {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
