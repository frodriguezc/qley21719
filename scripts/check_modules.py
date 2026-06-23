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


def _probe_pol(fo: QualysClient) -> tuple[bool, str]:
    """POL disponible si compliance/policy list responde 200 sin error de suscripción."""
    try:
        code, text = fo.fo_get("/api/4.0/fo/compliance/policy/", {"action": "list"})
    except Exception as e:  # noqa: BLE001
        return False, f"error de cliente: {type(e).__name__}: {str(e)[:120]}"
    if code != 200:
        return False, f"HTTP {code}: {text[:160].strip()}"
    low = text.lower()
    if "not subscribed" in low or "no permission" in low or "<code>2007" in low:
        return False, "el usuario/suscripción no tiene Policy Compliance habilitado"
    n = text.count("<POLICY>")
    return True, f"HTTP 200 · {n} policies listadas"


def _probe_tc(cv: CloudViewClient) -> tuple[bool, str]:
    """TC disponible si controls/metadata/list (CSPM) responde 200."""
    try:
        code, text = cv.list_controls({"pageSize": 1})
    except Exception as e:  # noqa: BLE001
        return False, f"error de cliente: {type(e).__name__}: {str(e)[:120]}"
    if code != 200:
        return False, f"HTTP {code}: {text[:160].strip()}"
    return True, "HTTP 200 · controls/metadata accesible"


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
    pol_ok, pol_why = _probe_pol(fo)
    print(f"  POL (Policy Compliance): {'OK' if pol_ok else 'FALTA'} — {pol_why}", flush=True)
    tc_ok, tc_why = _probe_tc(cv)
    print(f"  TC  (TotalCloud/CSPM):   {'OK' if tc_ok else 'FALTA'} — {tc_why}", flush=True)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md = [
        "# Módulos Qualys disponibles en el tenant",
        "",
        f"POD `{fo.pod}` · {now} · verificación **read-only**.",
        "",
        "| Módulo | Motor | Estado | Detalle |",
        "|---|---|---|---|",
        f"| **Policy Compliance / Policy Audit** | host-based (`policy.xml`) | {'✅ disponible' if pol_ok else '❌ falta'} | {pol_why} |",
        f"| **TotalCloud / CloudView (CSPM)** | cloud-posture | {'✅ disponible' if tc_ok else '❌ falta'} | {tc_why} |",
        "",
        "## Qué significa",
        "- **POL** genera el `policy.xml` de la Ley 21.719 (controles CIS host-based) + `faltantes.txt`.",
        "- **TC** genera el mapeo de posture cloud (CSPM) por cuenta.",
    ]
    if not pol_ok:
        md.append("- ⚠️ **Sin POL:** no se genera `policy.xml` ni `faltantes.txt`. Habilitar Policy "
                  "Compliance/Audit en la suscripción (o usar un API user con ese módulo).")
    if not tc_ok:
        md.append("- ⚠️ **Sin TC:** no se genera el pack cloud-posture. Habilitar TotalCloud/CloudView "
                  "(o verificar el host CSPM / rol del API user).")
    if pol_ok and tc_ok:
        md.append("- ✅ Ambos motores disponibles: se generan todos los deliverables.")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md) + "\n", encoding="utf-8")

    # líneas machine-readable para el orquestador
    print(f"POL={'yes' if pol_ok else 'no'}")
    print(f"TC={'yes' if tc_ok else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
