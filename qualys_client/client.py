"""qualys_client — cliente HTTP READ-ONLY de la API de Qualys.

Implementación **propia e independiente**, escrita desde cero contra la API pública de Qualys
(FO XML + QPS REST). No deriva de ningún script de Qualys ni de terceros.

Garantía read-only **horneada**: la FO API solo acepta `action` de lectura (list/fetch/count/
export) y la QPS solo paths que contengan `/search/` o `/count/`. Cualquier otra cosa levanta
`QualysReadOnlyError` antes de tocar la red. Sin métodos de escritura: no hay forma de mutar el tenant.

Auth: HTTP Basic + header `X-Requested-With` (requerido por la API). Backoff reactivo ante
rate/concurrency (HTTP 429/409), honrando los headers de espera que devuelve Qualys.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import requests

# Plataformas/PODs de Qualys: clave (identificador del POD) -> API server URL pública.
# Fuente: "Qualys Platform Identification / API Server URLs" (doc pública de Qualys). Verifica el
# tuyo en tu consola si tu POD no está acá.
PODS = {
    "US01": "https://qualysapi.qualys.com",
    "US02": "https://qualysapi.qg2.apps.qualys.com",
    "US03": "https://qualysapi.qg3.apps.qualys.com",
    "US04": "https://qualysapi.qg4.apps.qualys.com",
    "EU01": "https://qualysapi.qualys.eu",
    "EU02": "https://qualysapi.qg2.apps.qualys.eu",
    "EU03": "https://qualysapi.qg3.apps.qualys.eu",
    "IN01": "https://qualysapi.qg1.apps.qualys.in",
    "CA01": "https://qualysapi.qg1.apps.qualys.ca",
    "AE01": "https://qualysapi.qg1.apps.qualys.ae",
    "UK01": "https://qualysapi.qg1.apps.qualys.co.uk",
    "AU01": "https://qualysapi.qg1.apps.qualys.com.au",
}

# Acciones FO de SOLO LECTURA. Todo lo que no esté acá se rechaza (no se ejecuta).
READ_FO_ACTIONS = {"list", "fetch", "count", "list_id_range", "export"}

_MAX_RETRY = 5


class QualysReadOnlyError(RuntimeError):
    """Se intentó una operación que NO es de lectura (bloqueada por diseño)."""


class QualysClient:
    """Cliente read-only. Exposición mínima: `fo_get` (FO XML) y `qps_search` (QPS REST)."""

    def __init__(self, pod: str, user: str, password: str,
                 timeout: int = 120, debug: bool = False) -> None:
        server = PODS.get((pod or "").upper())
        if not server:
            raise ValueError(f"POD desconocido: {pod!r}. Conocidos: {sorted(PODS)}")
        self.pod = (pod or "").upper()
        self.server = server
        self.timeout = timeout
        self.debug = debug
        self.call_count = 0
        self.sess = requests.Session()
        self.sess.auth = (user, password)
        self.sess.headers.update({"X-Requested-With": "qley21719 (read-only)"})

    # -- transporte con backoff reactivo ------------------------------------- #
    def _request(self, method: str, url: str, params=None, data=None,
                 headers=None, _retry: int = 0) -> requests.Response:
        resp = self.sess.request(method, url, params=params, data=data,
                                 headers=headers, timeout=self.timeout)
        self.call_count += 1
        if resp.status_code in (409, 429) and _retry < _MAX_RETRY:
            wait = 0
            for h in ("X-RateLimit-ToWait-Sec", "Retry-After"):
                try:
                    wait = max(wait, int(resp.headers.get(h, "0") or 0))
                except ValueError:
                    pass
            time.sleep(min(60, wait or 15 * (_retry + 1)))
            return self._request(method, url, params, data, headers, _retry + 1)
        return resp

    # -- FO XML API (read-only) ---------------------------------------------- #
    def fo_get(self, path: str, params: dict | None = None) -> tuple[int, str]:
        """GET a la FO API. `params['action']` debe ser de lectura o levanta QualysReadOnlyError."""
        params = dict(params or {})
        action = params.get("action", "")
        if action not in READ_FO_ACTIONS:
            raise QualysReadOnlyError(
                f"FO action {action!r} no es de lectura; permitidas: {sorted(READ_FO_ACTIONS)}")
        r = self._request("GET", self.server + path, params=params)
        return r.status_code, r.text

    # -- QPS REST (read-only: solo /search o /count) ------------------------- #
    @staticmethod
    def _qps_body(limit: int | None = None, criteria: list[dict] | None = None) -> str:
        """Arma el body XML de una búsqueda/conteo QPS (schema público ServiceRequest)."""
        parts = []
        if criteria:
            crits = "".join(
                f"<Criteria field={quoteattr(str(c['field']))} "
                f"operator={quoteattr(str(c['operator']))}>{escape(str(c['value']))}</Criteria>"
                for c in criteria if str(c.get("value", "")).strip() != ""
            )
            if crits:
                parts.append(f"<filters>{crits}</filters>")
        if limit:
            parts.append(f"<preferences><limitResults>{int(limit)}</limitResults></preferences>")
        return f"<ServiceRequest>{''.join(parts)}</ServiceRequest>" if parts else ""

    def qps_search(self, path: str, limit: int | None = None,
                   payload_xml: str | None = None,
                   criteria: list[dict] | None = None) -> tuple[int, str]:
        if "/search/" not in path and "/count/" not in path:
            raise QualysReadOnlyError("QPS read-only: solo se permiten paths con /search/ o /count/")
        if payload_xml is None:
            payload_xml = self._qps_body(limit=limit, criteria=criteria)
        r = self._request("POST", self.server + path, data=payload_xml,
                          headers={"Content-Type": "text/xml"})
        return r.status_code, r.text


# --------------------------------------------------------------------------- #
# Construcción desde el entorno
# --------------------------------------------------------------------------- #
def _read_dotenv(path: Path) -> dict:
    """Parser mínimo de .env (KEY=VALUE), sin dependencias. Ignora comentarios/blancos."""
    env = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def from_env() -> QualysClient:
    """Construye el cliente. Precedencia de credenciales: variables de entorno → `.env` (raíz del
    proyecto, gitignored) → `config.yaml` (raíz, gitignored). El password nunca se loguea."""
    root = Path(__file__).resolve().parent.parent
    dotenv = _read_dotenv(root / ".env")
    cfg = {}
    cfg_path = root / "config.yaml"
    if cfg_path.exists():
        try:
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            cfg = {}

    def pick(*keys):
        for k in keys:
            if os.environ.get(k):
                return os.environ[k]
        for k in keys:
            if dotenv.get(k):
                return dotenv[k]
        return None

    pod = pick("QUALYS_POD") or cfg.get("pod")
    user = pick("QUALYS_API_USER") or cfg.get("user")
    password = pick("QUALYS_API_PASSWORD") or cfg.get("password")
    if not (pod and user and password):
        raise RuntimeError(
            "Faltan credenciales. Definí QUALYS_POD / QUALYS_API_USER / QUALYS_API_PASSWORD "
            "por entorno, en un .env, o en config.yaml.")
    return QualysClient(pod, user, password)
