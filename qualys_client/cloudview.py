"""cloudview — cliente HTTP READ-ONLY para la API CSPM de Qualys (TotalCloud / CloudView).

Motor SEPARADO del de Policy Compliance (ver DESIGN-cloud-posture.md). Namespace nuevo
`cloudview-api/rest/v1` (REST JSON), NO la XML API `api/2.0|4.0/fo` que cubre QualysClient.

Garantía read-only **horneada y allow-list-only** (rechaza por defecto, no deny-list):
el único método de red es `cv_get` (GET) y el path debe matchear el set enumerado de
endpoints de LECTURA (controls metadata, connectors, groups, evaluations, assessment
report list/download). Cualquier otro path —o cualquier intento de mutación (connector
run/create, report create)— levanta QualysReadOnlyError ANTES de tocar la red. No hay
método POST/PUT/DELETE: no hay forma de mutar el tenant.

Endpoints verificados vs el CloudView API User Guide (jun-2026); ver
mapping/platform_coverage.yaml `cspm_api`. Auth: HTTP Basic + X-Requested-With.

CAVEATS (mapping/platform_coverage.yaml `cspm_api.caveats`):
  - El host del gateway CSPM puede diferir del host FO por POD -> `server` es override-able;
    por defecto reutiliza el host de PODS (verificar contra el tenant si CSPM usa otro gateway).
  - Rol read-only (§7#8): confirmar que un usuario Reader puede leer evaluations/reports
    SIN permisos de mutación. Este cliente solo emite GETs; el invariante de ROL es del tenant.
"""
from __future__ import annotations

import re
import sys
import time

import requests

from .client import (PODS, QualysReadOnlyError, _read_dotenv,
                     _retry_after_seconds, _throttle_note)

CV_BASE = "/cloudview-api/rest/v1"

# Allow-list de paths de LECTURA (relativos a CV_BASE). Si ninguno matchea -> se rechaza.
# Verificados vs CloudView API User Guide. GCP per-resource/stats por simetría (PARTIAL).
_CV_READ_PATTERNS = tuple(re.compile(p) for p in (
    r"^/controls/metadata/list/?$",
    # connector detail acepta cualquier id, MENOS verbos de mutación (defensa en profundidad
    # además del GET-only): /connectors/create|run|enable|disable|delete se rechazan.
    r"^/(aws|azure|gcp|oci)/connectors(/(?!(?:create|run|enable|disable|delete)$)[^/]+)?/?$",
    r"^/groups(/[^/]+)?/?$",
    r"^/(aws|azure|gcp)/evaluations/[^/]+/?$",
    r"^/oci/evaluations/?$",
    r"^/(aws|azure|gcp|oci)/evaluations/[^/]+/resources/[^/]+/?$",
    r"^/(aws|azure|gcp|oci)/evaluations/stats/[^/]+/[^/]+/?$",
    r"^/report/assessment/list/?$",
    r"^/report/assessment/[^/]+/download/?$",
))

_MAX_RETRY = 5


def cspm_server(fo_server: str) -> str:
    """Host CSPM (CloudView/TotalCloud) a partir del host FO. CloudView NO se sirve desde el host
    FO (`qualysapi.*` -> 404 en cloudview-api): va por el host del portal `qualysguard.<seg>`.
    Convención: `qualysapi.<seg>.apps.qualys.*` -> `qualysguard.<seg>.apps.qualys.*`. Override con `server=`.
    VERIFICADO LIVE en US03 (jun-2026) con credenciales válidas: controls/metadata/list -> HTTP 200
    en `qualysguard.qg3.apps.qualys.com`; `gateway.qg3` dio 401 y el host FO `qualysapi.qg3` dio 404.
    Para otros PODs, confirmar (el patrón del portal qualysguard.<seg> es el esperado)."""
    return fo_server.replace("://qualysapi.", "://qualysguard.", 1)


class CloudViewClient:
    """Cliente CSPM read-only. Exposición mínima: `cv_get` (GET allow-list-only) + helpers."""

    def __init__(self, pod: str, user: str, password: str,
                 server: str | None = None, timeout: int = 120, debug: bool = False) -> None:
        fo = PODS.get((pod or "").upper())
        base = server or (cspm_server(fo) if fo else None)
        if not base:
            raise ValueError(f"POD desconocido: {pod!r}. Conocidos: {sorted(PODS)} (o pasa `server=`).")
        self.pod = (pod or "").upper()
        self.server = base.rstrip("/")
        self.timeout = timeout
        self.debug = debug
        self.call_count = 0
        self.sess = requests.Session()
        self.sess.auth = (user, password)
        self.sess.headers.update({
            "X-Requested-With": "qley21719 (read-only)",
            "Accept": "application/json",
        })

    # -- transporte con backoff reactivo (igual filosofía que QualysClient) -- #
    def _request(self, url: str, params=None, _retry: int = 0) -> requests.Response:
        resp = self.sess.get(url, params=params, timeout=self.timeout)
        self.call_count += 1
        if resp.status_code in (409, 429) and _retry < _MAX_RETRY:
            wait = _retry_after_seconds(resp, _retry)
            if self.debug:
                print(f"[cloudview] {_throttle_note(resp)} -> sleep {wait}s "
                      f"(retry {_retry + 1}/{_MAX_RETRY})", file=sys.stderr)
            time.sleep(wait)
            return self._request(url, params, _retry + 1)
        return resp

    # -- CloudView REST (read-only, allow-list-only) ------------------------- #
    def cv_get(self, subpath: str, params: dict | None = None) -> tuple[int, str]:
        """GET a `cloudview-api/rest/v1<subpath>`. `subpath` se valida contra la allow-list
        de lectura; si no matchea, levanta QualysReadOnlyError (no se ejecuta)."""
        sp = subpath if subpath.startswith("/") else "/" + subpath
        # Permitir pasar el path completo con el base incluido.
        if sp.startswith(CV_BASE):
            sp = sp[len(CV_BASE):] or "/"
        path = sp.split("?", 1)[0]
        if not any(p.match(path) for p in _CV_READ_PATTERNS):
            raise QualysReadOnlyError(
                f"CloudView read-only: path {sp!r} no está en la allow-list de lectura "
                f"(controls/metadata, connectors, groups, evaluations, report list/download).")
        r = self._request(self.server + CV_BASE + sp, params=params)
        return r.status_code, r.text

    # -- helpers de conveniencia (todos read-only) --------------------------- #
    def list_controls(self, params: dict | None = None) -> tuple[int, str]:
        return self.cv_get("/controls/metadata/list", params)

    def list_connectors(self, provider: str, params: dict | None = None) -> tuple[int, str]:
        return self.cv_get(f"/{provider.lower()}/connectors", params)

    def list_evaluations(self, provider: str, account: str,
                         params: dict | None = None) -> tuple[int, str]:
        provider = provider.lower()
        if provider == "oci":
            p = dict(params or {})
            p.setdefault("tenantId", account)
            return self.cv_get("/oci/evaluations/", p)
        return self.cv_get(f"/{provider}/evaluations/{account}", params)

    def list_assessment_reports(self, params: dict | None = None) -> tuple[int, str]:
        return self.cv_get("/report/assessment/list", params)


def from_env(server: str | None = None) -> CloudViewClient:
    """Construye el cliente CSPM con las MISMAS credenciales que QualysClient.from_env()
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
    return CloudViewClient(pod, user, password, server=server)
