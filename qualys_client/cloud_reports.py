"""cloud_reports — cliente HTTP de REPORTES CSPM (TotalCloud / CloudView) con capacidad de MUTACIÓN.

SEPARADO a propósito del `CloudViewClient` read-only: crear/correr un Assessment o Mandate Report
es un POST que **muta el tenant**. Por el invariante read-only del motor, esa capacidad NO vive en
`CloudViewClient` (que rechaza todo no-GET); vive acá y la usa SOLO `scripts/extract_cloud_reports.py`,
que es **human-gated** (no hace POST salvo `--run` explícito).

Auth idéntica a CloudView: **API Gateway + JWT** (NO Basic contra `qualysguard.*`, que da 401).
Se obtiene el token con `POST /auth` y se usan los endpoints con `Authorization: Bearer`. Reusa el
backoff de rate-limit ya probado del paquete (`_retry_after_seconds` lee `X-RateLimit-ToWait-Sec` /
`Retry-After`). El password nunca se loguea.

⚠️ Este cliente PUEDE mutar el tenant (POST de creación de reportes). No lo importan el motor
read-only ni los flujos automáticos — solo el script de extracción con human-gate.
"""
from __future__ import annotations

import sys
import time

import requests

from .client import _read_dotenv, _retry_after_seconds, _throttle_note
from .cloudview import CONN_BASE, CV_BASE, _CLOUD_TYPES, gateway_for

_MAX_RETRY = 5


class CloudReportClient:
    """Cliente CSPM de reportes (API Gateway + JWT). Expone GET (descubrimiento + estado +
    descarga) y POST (create/rerun = MUTACIÓN). El único uso es el script human-gated."""

    def __init__(self, pod: str, user: str, password: str,
                 server: str | None = None, timeout: int = 120, debug: bool = False) -> None:
        base = server or gateway_for(pod)
        if not base:
            raise ValueError(
                f"POD desconocido: {pod!r}. Conocidos (gateway): pasá --server o un POD válido.")
        self.pod = (pod or "").upper()
        self.server = base.rstrip("/")
        self.timeout = timeout
        self.debug = debug
        self.call_count = 0          # GETs
        self.mutations = 0           # POSTs de datos (NO la auth)
        self._user = user
        self._password = password
        self._token: str | None = None
        self.sess = requests.Session()
        self.sess.headers.update({
            "X-Requested-With": "qley21719 (reports)",
            "Accept": "application/json",
        })

    # -- auth: API Gateway + JWT (mismo flujo que CloudViewClient) ------------ #
    def _authenticate(self) -> requests.Response | None:
        resp = self.sess.post(
            self.server + "/auth",
            data={"username": self._user, "password": self._password, "token": "true"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        token = (resp.text or "").strip()
        if resp.status_code in (200, 201) and token.count(".") == 2 and len(token) > 40:
            self._token = token
            self.sess.headers["Authorization"] = f"Bearer {token}"
            return None
        if self.debug:
            print(f"[cloud_reports] auth falló: HTTP {resp.status_code}", file=sys.stderr)
        return resp

    def _ensure_auth(self) -> requests.Response | None:
        if self._token is not None:
            return None
        return self._authenticate()

    # -- transporte: re-auth ante token vencido + backoff reactivo 409/429 ---- #
    def _request(self, method: str, url: str, json=None, params=None,
                 _retry: int = 0, _reauthed: bool = False) -> requests.Response:
        failed = self._ensure_auth()
        if failed is not None:
            return failed
        resp = self.sess.request(method, url, json=json, params=params, timeout=self.timeout)
        if method.upper() == "GET":
            self.call_count += 1
        else:
            self.mutations += 1
        if resp.status_code == 401 and not _reauthed:
            self._token = None
            self.sess.headers.pop("Authorization", None)
            return self._request(method, url, json, params, _retry, _reauthed=True)
        if resp.status_code in (409, 429) and _retry < _MAX_RETRY:
            wait = _retry_after_seconds(resp, _retry)
            if self.debug:
                print(f"[cloud_reports] {_throttle_note(resp)} -> sleep {wait}s "
                      f"(retry {_retry + 1}/{_MAX_RETRY})", file=sys.stderr)
            time.sleep(wait)
            return self._request(method, url, json, params, _retry + 1, _reauthed)
        return resp

    # -- superficies ---------------------------------------------------------- #
    def get(self, subpath: str, params: dict | None = None) -> tuple[int, str]:
        """GET a cloudview-api/rest/v1<subpath>. Devuelve (status, text)."""
        r = self._request("GET", self.server + CV_BASE + _norm(subpath), params=params)
        return r.status_code, r.text

    def post(self, subpath: str, body: dict) -> tuple[int, str]:
        """POST (MUTACIÓN) a cloudview-api/rest/v1<subpath>. Devuelve (status, text)."""
        r = self._request("POST", self.server + CV_BASE + _norm(subpath), json=body)
        return r.status_code, r.text

    def download(self, subpath: str, out_path: str,
                 params: dict | None = None) -> tuple[int, int]:
        """GET de descarga (CSV/PDF). Si 2xx, escribe el contenido binario a out_path.
        Devuelve (status, bytes_escritos)."""
        r = self._request("GET", self.server + CV_BASE + _norm(subpath), params=params)
        if 200 <= r.status_code < 300:
            with open(out_path, "wb") as fh:
                fh.write(r.content)
            return r.status_code, len(r.content)
        return r.status_code, 0

    def list_cloud_connectors(self, cloud_type: str,
                              params: dict | None = None) -> tuple[int, str]:
        """Connector Management API (GET): /connectors/v1.0/<TYPE>/list. Necesario para OCI."""
        ct = (cloud_type or "").upper()
        if ct not in _CLOUD_TYPES:
            raise ValueError(f"cloud_type inválido: {cloud_type!r} (uno de {_CLOUD_TYPES}).")
        p = {"pageNumber": 0, "pageSize": 100}
        if params:
            p.update(params)
        r = self._request("GET", f"{self.server}{CONN_BASE}/{ct}/list", params=p)
        return r.status_code, r.text


def _norm(subpath: str) -> str:
    sp = subpath if subpath.startswith("/") else "/" + subpath
    if sp.startswith(CV_BASE):
        sp = sp[len(CV_BASE):] or "/"
    return sp


def from_env(server: str | None = None) -> CloudReportClient:
    """Construye el cliente de reportes con las MISMAS credenciales que el resto del motor
    (env -> .env -> requeridas). El password nunca se loguea."""
    import os
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    dotenv = _read_dotenv(root / ".env")

    def pick(*keys):
        for k in keys:
            if os.environ.get(k):
                return os.environ[k]
        for k in keys:
            if dotenv.get(k):
                return dotenv[k]
        return None

    pod = pick("QUALYS_POD")
    user = pick("QUALYS_API_USER")
    password = pick("QUALYS_API_PASSWORD")
    if not (pod and user and password):
        raise RuntimeError(
            "Faltan credenciales. Definí QUALYS_POD / QUALYS_API_USER / QUALYS_API_PASSWORD "
            "por entorno o en un .env.")
    return CloudReportClient(pod, user, password, server=server)
