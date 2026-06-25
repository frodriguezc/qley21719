#!/usr/bin/env python3
"""check_modules.py — verifica (READ-ONLY) qué módulos de Qualys tiene habilitados el tenant:
**POL** (Policy Compliance / Policy Audit, motor host-based) y **TC** (TotalCloud / CloudView,
motor cloud-posture CSPM). Si falta alguno, lo indica.

Solo hace GETs de lectura (compliance/policy list para POL; controls/metadata para TC).
Emite un reporte Markdown y, a stdout, líneas `POL=yes|no` / `TC=yes|no` para que el
orquestador (run.sh / run.ps1) decida qué generar.

Uso:
    python scripts/check_modules.py --out deliverables/0-modulos.md
    (credenciales por entorno/.env, o --pod/--user/--password)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qualys_client import QualysClient, from_env  # noqa: E402
from qualys_client.cloudview import CloudViewClient  # noqa: E402
from qualys_client.cloudview import from_env as cv_from_env  # noqa: E402


# Estados de una probe (NO confundir "auth" con "absent"):
#   ok     -> el módulo responde 200 y está habilitado.
#   absent -> autenticó OK pero la suscripción/usuario NO tiene el módulo (200 + "not subscribed").
#   auth   -> HTTP 401/403: credenciales/POD/permiso rechazados. NO dice nada sobre el módulo.
#   error  -> otro fallo (HTTP raro, red, etc.).
def _probe_pol(fo: QualysClient) -> tuple[str, str]:
    """Clasifica POL probando compliance/policy list."""
    try:
        code, text = fo.fo_get("/api/4.0/fo/compliance/policy/", {"action": "list"})
    except Exception as e:  # noqa: BLE001
        return "error", f"error de cliente: {type(e).__name__}: {str(e)[:120]}"
    if code in (401, 403):
        return "auth", f"HTTP {code}: autenticación/permiso rechazado (NO es ausencia de módulo)"
    if code != 200:
        return "error", f"HTTP {code}: {text[:160].strip()}"
    low = text.lower()
    if "not subscribed" in low or "no permission" in low or "<code>2007" in low:
        return "absent", "el usuario/suscripción no tiene Policy Compliance habilitado"
    n = text.count("<POLICY>")
    return "ok", f"HTTP 200 · {n} policies listadas"


def _probe_tc(cv: CloudViewClient) -> tuple[str, str]:
    """Clasifica TC probando `<prov>/connectors` (CSPM, vía gateway+JWT). connectors responde 200
    cuando hay acceso a CloudView aunque no haya cuentas conectadas (a diferencia de
    controls/metadata/list, que puede dar 401 por un permiso de control-library aparte)."""
    last = ("error", "sin respuesta del gateway CSPM")
    for prov in ("aws", "azure", "gcp"):
        try:
            code, text = cv.list_connectors(prov, {"pageNo": 0, "pageSize": 1})
        except Exception as e:  # noqa: BLE001
            return "error", f"error de cliente: {type(e).__name__}: {str(e)[:120]}"
        if code == 200:
            return "ok", f"HTTP 200 · CloudView accesible ({prov}/connectors)"
        if code in (401, 403):
            snip = text[:120].strip().replace("\n", " ")
            last = ("auth", f"HTTP {code} (auth/permiso, NO ausencia de módulo): {snip}")
        else:
            last = ("error", f"HTTP {code}: {text[:140].strip()}")
    return last


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verifica módulos POL/TC del tenant (read-only).")
    ap.add_argument("--out", default="deliverables/0-modulos.md", help="Reporte Markdown de salida.")
    ap.add_argument("--pod", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None, help="Override del gateway CSPM.")
    args = ap.parse_args(argv)

    if args.pod and args.user and args.password:
        fo = QualysClient(args.pod, args.user, args.password)
        cv = CloudViewClient(args.pod, args.user, args.password, server=args.server)
    else:
        fo = from_env()
        cv = cv_from_env(server=args.server)

    print(f"[modules] POD {fo.pod} · FO {fo.server} · CSPM {cv.server}", flush=True)
    pol_state, pol_why = _probe_pol(fo)
    tc_state, tc_why = _probe_tc(cv)
    LABEL = {"ok": "OK", "absent": "FALTA", "auth": "AUTH✗", "error": "ERROR"}
    print(f"  POL (Policy Compliance): {LABEL[pol_state]} — {pol_why}", flush=True)
    print(f"  TC  (TotalCloud/CSPM):   {LABEL[tc_state]} — {tc_why}", flush=True)

    pol_ok = pol_state == "ok"
    tc_ok = tc_state == "ok"
    # 401/403 = autenticación rechazada → NO es "módulo faltante". Si NADA autenticó, abortar fuerte:
    # producir un pack vacío con "✓ LISTO" engaña (el síntoma original que mandó por la pista equivocada).
    auth_fatal = (pol_state == "auth" or tc_state == "auth") and not (pol_ok or tc_ok)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ICON = {"ok": "✅ disponible", "absent": "❌ falta", "auth": "🔐 auth/permiso rechazado", "error": "⚠️ error"}
    md = [
        "# Módulos Qualys disponibles en el tenant",
        "",
        f"POD `{fo.pod}` · {now} · verificación **read-only**.",
        "",
        "| Módulo | Motor | Estado | Detalle |",
        "|---|---|---|---|",
        f"| **Policy Compliance / Policy Audit** | host-based (`policy.xml`) | {ICON[pol_state]} | {pol_why} |",
        f"| **TotalCloud / CloudView (CSPM)** | cloud-posture | {ICON[tc_state]} | {tc_why} |",
        "",
        "## Qué significa",
        "- **POL** genera el `policy.xml` de la Ley 21.719 (controles CIS host-based) + `faltantes.txt`.",
        "- **TC** genera el mapeo de posture cloud (CSPM) por cuenta.",
    ]
    if auth_fatal:
        md += [
            "",
            "## 🔐 Autenticación rechazada (HTTP 401/403) — NO es ausencia de módulos",
            "Las credenciales no autenticaron contra este POD. Esto **no** dice si el tenant tiene",
            "POL/TC: hay que arreglar la conexión primero. Revisá, en orden:",
            f"1. **POD**: `{fo.pod}` ¿es el del cliente? (el default de `.env.example` es `US03`). "
            "Confirmalo en la URL de la consola del cliente (`qualysguard.qgN…` → `US0N`).",
            "2. **Usuario/clave** del API user correctos para ese POD.",
            "3. El API user tiene **API Access** habilitado (no es un usuario solo-UI/SSO).",
        ]
    else:
        if pol_state == "absent":
            md.append("- ⚠️ **Sin POL:** no se genera `policy.xml` ni `faltantes.txt`. Habilitar Policy "
                      "Compliance/Audit en la suscripción (o usar un API user con ese módulo).")
        elif pol_state in ("auth", "error"):
            md.append(f"- ⚠️ **POL no verificable** ({pol_why}). No es ausencia de módulo confirmada.")
        if tc_state == "absent":
            md.append("- ⚠️ **Sin TC:** no se genera el pack cloud-posture. Habilitar TotalCloud/CloudView "
                      "(o verificar el host CSPM / rol del API user).")
        elif tc_state in ("auth", "error"):
            md.append(f"- ⚠️ **TC no verificable** ({tc_why}). No es ausencia de módulo confirmada.")
        if pol_ok and tc_ok:
            md.append("- ✅ Ambos motores disponibles: se generan todos los deliverables.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md) + "\n", encoding="utf-8")

    if auth_fatal:
        print("\n✗ Autenticación rechazada (HTTP 401/403) — esto NO es 'módulo faltante'.\n"
              f"   Revisá: 1) QUALYS_POD ('{fo.pod}' es el default si copiaste .env.example) coincide con "
              "el POD del cliente · 2) usuario/clave correctos · 3) el API user tiene 'API Access'.",
              file=sys.stderr)
        return 2

    # líneas machine-readable para el orquestador
    print(f"POL={'yes' if pol_ok else 'no'}")
    print(f"TC={'yes' if tc_ok else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
