"""cloudview — cliente HTTP READ-ONLY para la API CSPM de Qualys (TotalCloud / CloudView).

Motor SEPARADO del de Policy Compliance (ver DESIGN-cloud-posture.md). Namespace nuevo
`cloudview-api/rest/v1` (REST JSON), NO la XML API `api/2.0|4.0/fo` que cubre QualysClient.

Garantía read-only **horneada y allow-list-only** (rechaza por defecto, no deny-list):
el único método de red de DATOS es `cv_get` (GET) y el path debe matchear el set enumerado
de endpoints de LECTURA (controls metadata, connectors, groups, evaluations, assessment
report list/download). Cualquier otro path —o cualquier intento de mutación (connector
run/create, report create)— levanta QualysReadOnlyError ANTES de tocar la red. No hay
método público POST/PUT/DELETE de datos: no hay forma de mutar el tenant.

Auth: **Qualys API Gateway + JWT** (NO Basic). CloudView/TotalCloud NO se sirve ni desde el
host FO (`qualysapi.*` -> 404) ni desde el portal (`qualysguard.*` -> 401 con Basic). Va por
el **API Gateway** `gateway.<seg>.apps.qualys.*`: se obtiene un token con `POST /auth`
(form-urlencoded username/password, `token=true`) y se usan los endpoints con
`Authorization: Bearer <jwt>`. El único POST que hace el cliente es ese `/auth` (emisión de
token; NO muta el tenant); todos los datos son GET allow-list-only. El password nunca se loguea.

> VERIFICADO LIVE US03 (2026-06): `POST gateway.qg3/auth` -> 201 + JWT; `/aws|azure|gcp/connectors`
> -> 200. (El approach anterior —Basic contra `qualysguard.qg3`— daba 401 contra un tenant real;
> el "200" histórico era un artefacto y se corrigió.) `controls/metadata/list` puede dar 401 aun con
> JWT válido si el API user no tiene el permiso de control-library de CloudView (es otro permiso);
> el harvest solo lo consulta cuando hay connectors -> se verifica en un tenant con cuentas cloud.

Endpoints verificados vs la coleccion Postman v1.23.0.0 + la guia TotalCloud/CloudView API
vigente (docs.qualys.com/en/tc/api, jun-2026); ver mapping/platform_coverage.yaml `cspm_api`.
"""
from __future__ import annotations

import re
import sys
import time

import requests

from .client import (QualysReadOnlyError, _read_dotenv,
                     _retry_after_seconds, _throttle_note)

CV_BASE = "/cloudview-api/rest/v1"
# Connector Management API (gateway + JWT): GET /connectors/v1.0/<TYPE>/list. Es el endpoint
# correcto para listar connectors —el ÚNICO para OCI: cloudview-api/oci/connectors NO existe (404)
# y OCI tampoco está en QPS (INVALID_API_VERSION)—. Soporta AWS/AZURE/GCP/OCI. Read-only (GET).
CONN_BASE = "/connectors/v1.0"
_CLOUD_TYPES = ("AWS", "AZURE", "GCP", "OCI")

# Host del API Gateway de Qualys por POD: CloudView/TotalCloud van por acá (con JWT). El host FO
# (qualysapi.*) NO sirve cloudview-api; el portal (qualysguard.*) lo rechaza con JWT/Basic.
# VERIFICADO LIVE US03 (2026-06). Otros PODs siguen el patrón gateway.<seg>.apps.qualys.<tld>
# (confirmar por POD; con `server=` se puede forzar el host).
GATEWAYS = {
    "US01": "https://gateway.qg1.apps.qualys.com",
    "US02": "https://gateway.qg2.apps.qualys.com",
    "US03": "https://gateway.qg3.apps.qualys.com",
    "US04": "https://gateway.qg4.apps.qualys.com",
    "EU01": "https://gateway.qg1.apps.qualys.eu",
    "EU02": "https://gateway.qg2.apps.qualys.eu",
    "EU03": "https://gateway.qg3.apps.qualys.eu",
    "IN01": "https://gateway.qg1.apps.qualys.in",
    "CA01": "https://gateway.qg1.apps.qualys.ca",
    "AE01": "https://gateway.qg1.apps.qualys.ae",
    "UK01": "https://gateway.qg1.apps.qualys.co.uk",
    "AU01": "https://gateway.qg1.apps.qualys.com.au",
}

# Allow-list de paths de LECTURA (relativos a CV_BASE). Si ninguno matchea -> se rechaza.
# Verificados vs Postman v1.23.0.0 + guia tc/api (jun-2026): controls/metadata, {aws|azure|gcp}
# connectors/evaluations/{resources,stats}, groups, /oci/evaluations/?tenantId=, report assessment
# list/download. Notas: /evaluations/stats confirmado para AWS/Azure/GCP (OCI no lo expone).
# Connectors: cloudview-api expone /{aws|azure|gcp}/connectors (200), pero /oci/connectors NO existe
# (404, VERIFICADO live) y OCI tampoco está en QPS -> los connectors (incl. OCI) se listan por la
# Connector Management API `GET /connectors/v1.0/<TYPE>/list` (ver `list_cloud_connectors`). El
# patrón /oci/connectors abajo queda defensivo (GET-only); la OCI evaluations sí está en v1.
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


def gateway_for(pod: str) -> str | None:
    """Host del API Gateway (CloudView/TotalCloud, JWT) para un POD. None si no se conoce."""
    return GATEWAYS.get((pod or "").upper())


class CloudViewClient:
    """Cliente CSPM read-only (API Gateway + JWT). Exposición de datos mínima: `cv_get`
    (GET allow-list-only) + helpers. El único no-GET es la auth (`POST /auth`)."""

    def __init__(self, pod: str, user: str, password: str,
                 server: str | None = None, timeout: int = 120, debug: bool = False) -> None:
        base = server or gateway_for(pod)
        if not base:
            raise ValueError(
                f"POD desconocido: {pod!r}. Conocidos: {sorted(GATEWAYS)} (o pasa `server=`).")
        self.pod = (pod or "").upper()
        self.server = base.rstrip("/")
        self.timeout = timeout
        self.debug = debug
        self.call_count = 0          # cuenta GETs de datos (la auth no cuenta)
        self._user = user
        self._password = password
        self._token: str | None = None
        self.sess = requests.Session()
        self.sess.headers.update({
            "X-Requested-With": "qley21719 (read-only)",
            "Accept": "application/json",
        })

    # -- auth: API Gateway + JWT (único POST; NO muta el tenant) -------------- #
    def _authenticate(self) -> requests.Response | None:
        """POST /auth -> JWT. Setea `Authorization: Bearer` y devuelve None si OK; si el gateway
        rechaza (p.ej. 401), devuelve la Response para que el caller la propague como (code, text)."""
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
            print(f"[cloudview] auth falló: HTTP {resp.status_code}", file=sys.stderr)
        return resp

    def _ensure_auth(self) -> requests.Response | None:
        """Asegura un token. None si ya hay/se obtuvo; la Response fallida si la auth falló."""
        if self._token is not None:
            return None
        return self._authenticate()

    # -- transporte con backoff reactivo + re-auth ante token vencido -------- #
    def _request(self, url: str, params=None, _retry: int = 0,
                 _reauthed: bool = False) -> requests.Response:
        failed = self._ensure_auth()
        if failed is not None:                       # auth rechazada -> propagar tal cual
            return failed
        resp = self.sess.get(url, params=params, timeout=self.timeout)
        self.call_count += 1
        # token vencido a mitad de corrida -> re-autenticar UNA vez y reintentar
        if resp.status_code == 401 and not _reauthed:
            self._token = None
            self.sess.headers.pop("Authorization", None)
            return self._request(url, params, _retry, _reauthed=True)
        if resp.status_code in (409, 429) and _retry < _MAX_RETRY:
            wait = _retry_after_seconds(resp, _retry)
            if self.debug:
                print(f"[cloudview] {_throttle_note(resp)} -> sleep {wait}s "
                      f"(retry {_retry + 1}/{_MAX_RETRY})", file=sys.stderr)
            time.sleep(wait)
            return self._request(url, params, _retry + 1, _reauthed)
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

    def list_cloud_connectors(self, cloud_type: str,
                              params: dict | None = None) -> tuple[int, str]:
        """Lista connectors vía la Connector Management API (gateway + JWT, read-only):
        GET /connectors/v1.0/<TYPE>/list. Es el endpoint correcto para **OCI** (cloudview-api/oci/
        connectors NO existe). `cloud_type` ∈ AWS/AZURE/GCP/OCI. Cada item trae el id de cuenta
        (OCI: `tenantId`) para luego leer evaluations. GET-only: no hay forma de mutar."""
        ct = (cloud_type or "").upper()
        if ct not in _CLOUD_TYPES:
            raise QualysReadOnlyError(
                f"cloud_type inválido: {cloud_type!r} (esperado uno de {_CLOUD_TYPES}).")
        p = {"pageNumber": 0, "pageSize": 100}
        if params:
            p.update(params)
        r = self._request(f"{self.server}{CONN_BASE}/{ct}/list", params=p)
        return r.status_code, r.text


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
