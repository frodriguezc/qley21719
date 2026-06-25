#!/usr/bin/env python3
"""verify_tenant.py — sonda READ-ONLY para cerrar los confirmables-live del motor CSPM.

Cierra (con una credencial del tenant) los ítems que quedaron "confirmar live" en
DESIGN-cloud-posture.md §7 — SIN disparar ninguna mutación:

  1. ROL READER (§7 #8 / C1): ¿una credencial de SOLO LECTURA puede GETear controls, evaluations
     y el LISTADO de reportes? Corre ESTE script con un API user de rol **Reader** (no Manager):
     si los GET dan 200, el invariante "el cliente usa credenciales read-only" se sostiene.
  2. OCI live (§7 #5 / B2): ¿responden los connectors y evaluations de OCI? (lado lectura).
  3. CIDs a nivel tenant (B3): vuelca control.name + CID reales por proveedor (sobre todo OCI)
     para cotejar los ejemplos del mapping con los controles del benchmark vigente en el tenant.

Lo que este script **NO** hace (y por qué): NO crea/corre reportes (`report/.../create` = POST =
MUTACIÓN). El `CloudViewClient` es allow-list-only y NO expone ningún POST -> es estructuralmente
incapaz de mutar el tenant. Confirmar `cloudType=OCI` EN reportes requiere el POST de create -> lo
corre el cliente a mano (ver apply-instructions §3b); acá no.

Uso:
    export QUALYS_POD=US03 QUALYS_API_USER=<reader> QUALYS_API_PASSWORD=...
    python scripts/verify_tenant.py                 # todos los providers
    python scripts/verify_tenant.py --provider oci  # solo OCI
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.cloud_posture_pack import _ACCOUNT_KEYS, _discover_accounts  # noqa: E402

_OK, _WARN, _FAIL = "PASS", "WARN", "FAIL"


def _classify(code: int) -> str:
    if code == 200:
        return _OK
    if code in (401, 403):
        return _WARN          # alcanzó la API pero el rol no tiene permiso (dato útil, no error de shape)
    return _FAIL


def _probe(label: str, fn) -> tuple[str, int, object]:
    """Corre un GET read-only, devuelve (verdict, code, parsed-or-text). Nunca lanza."""
    try:
        code, text = fn()
    except Exception as e:  # noqa: BLE001
        return _FAIL, 0, f"{type(e).__name__}: {e}"
    verdict = _classify(code)
    try:
        body = json.loads(text)
    except Exception:  # noqa: BLE001
        body = text
    print(f"  [{verdict}] {label}: HTTP {code}")
    return verdict, code, body


def _count(body) -> int:
    if isinstance(body, dict):
        for k in ("content", "data", "items", "control", "controls"):
            if isinstance(body.get(k), list):
                return len(body[k])
        return 1
    return len(body) if isinstance(body, list) else 0


def _sample_controls(body, n=5) -> list[tuple[str, str]]:
    items = body.get("content") if isinstance(body, dict) else body
    if isinstance(body, dict) and not isinstance(items, list):
        items = body.get("control") or []
    out = []
    for it in (items or [])[:n]:
        if isinstance(it, dict):
            cid = it.get("controlId") or it.get("cid") or it.get("id") or "?"
            name = it.get("controlName") or it.get("name") or it.get("control.name") or "?"
            out.append((str(cid), str(name)))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sonda read-only del tenant CSPM (cierra §7 #8/#5/B3).")
    ap.add_argument("--provider", default="all", choices=["all", "aws", "azure", "gcp", "oci"])
    ap.add_argument("--account", default="", help="Account/subscription/project/tenant id (vacío = auto).")
    ap.add_argument("--pod", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None)
    args = ap.parse_args(argv)

    from qualys_client import CloudViewClient
    from qualys_client.cloudview import from_env as cv_from_env
    if args.pod and args.user and args.password:
        client = CloudViewClient(args.pod, args.user, args.password, server=args.server)
    else:
        client = cv_from_env(server=args.server)

    print(f"[verify] host={client.server}  (solo GETs allow-list; NINGUNA mutación)\n")
    results: dict[str, str] = {}

    # --- Check 1a: controls metadata (lectura base que un Reader debe poder) -------------------
    print("# 1. Rol read-only (§7 #8) — ¿un Reader puede GETear controls/evaluations/reportes?")
    v, _, body = _probe("controls/metadata/list", lambda: client.list_controls({"pageSize": 5}))
    results["controls_metadata"] = v
    if v == _OK:
        print(f"      -> {_count(body)} controles en la muestra")

    # --- Check 1b: report assessment LIST (read; no create) ------------------------------------
    v, code, _ = _probe("report/assessment/list", lambda: client.list_assessment_reports({"pageSize": 1}))
    results["report_list"] = v
    if v == _WARN:
        print("      -> 401/403: el listado de reportes pide Reporting Permission "
              "(rol '- only Reports'); el create igual lo corre el cliente (human-gate).")

    # --- Checks 2 + 3: por proveedor (OCI incluido) — evaluations + sample de CIDs --------------
    providers = ["aws", "azure", "gcp", "oci"] if args.provider == "all" else [args.provider]
    print("\n# 2. OCI live (§7 #5) + # 3. CIDs a nivel tenant (B3) — connectors + evaluations")
    any_oci = False
    for prov in providers:
        accounts = [args.account] if args.account else _discover_accounts(client, prov)
        if not accounts:
            print(f"  [skip] {prov}: sin connectors/cuentas")
            continue
        if prov == "oci":
            any_oci = True
        for acct in accounts[:1]:   # una cuenta por proveedor basta para la sonda
            v, _, body = _probe(f"{prov}/evaluations/{acct}",
                                lambda p=prov, a=acct: client.list_evaluations(p, a, {"pageSize": 5}))
            results[f"evaluations_{prov}"] = v
            if v == _OK:
                sample = _sample_controls(body)
                print(f"      -> {_count(body)} evaluaciones · muestra de CIDs/control.name:")
                for cid, name in sample:
                    print(f"         · {cid}: {name}")
    if "oci" in providers and not any_oci:
        print("  [info] OCI: sin connectors en el tenant -> no se pudo confirmar OCI live "
              "(no es un fallo; el tenant no tiene OCI onboardeado).")

    # --- Veredicto -----------------------------------------------------------------------------
    print("\n# Resumen")
    oks = [k for k, v in results.items() if v == _OK]
    warns = [k for k, v in results.items() if v == _WARN]
    fails = [k for k, v in results.items() if v == _FAIL]
    print(f"  PASS={len(oks)}  WARN={len(warns)}  FAIL={len(fails)}  ({client.call_count} GETs)")
    print(f"  GETs OK: {', '.join(oks) or '—'}")
    if warns:
        print(f"  WARN (permiso/rol, no shape): {', '.join(warns)}")
    if fails:
        print(f"  FAIL: {', '.join(fails)}")
    print("\n  Lectura: si corriste esto con una credencial **Reader** y los GETs base dan PASS,")
    print("  el invariante read-only se sostiene con mínimo privilegio. `cloudType=OCI` EN reportes")
    print("  no se prueba acá (es POST/mutación) -> lo valida el cliente al generar el reporte.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
