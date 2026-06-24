#!/usr/bin/env python3
"""cloud_posture_pack.py — pack de cobertura CLOUD POSTURE (CSPM) de la Ley 21.719 (read-only).

Motor SEPARADO del de Policy Compliance. Lee posture CSPM (controls + evaluations) vía
qualys_client.CloudViewClient (solo GET, allow-list-only) y emite un mapping report
control-cloud -> familia legal -> artículo (+ PASS/FAIL), control-set de FAILs, gaps y
apply-instructions. NO emite policy.xml y NO muta el tenant (el cliente aplica por UI).

Uso (live, auto-descubre connectors de todos los providers):
    export QUALYS_POD=US03 QUALYS_API_USER=... QUALYS_API_PASSWORD=...
    python scripts/cloud_posture_pack.py                      # --provider all (default)
    python scripts/cloud_posture_pack.py --provider aws       # solo AWS (auto-cuentas)
    python scripts/cloud_posture_pack.py --provider aws --account 111122223333

Uso (sin tenant, sobre una muestra JSON {controls:[...], evaluations:[...]}):
    python scripts/cloud_posture_pack.py --fixture sample.json --provider aws --account demo

Salida en artifacts/cloud-pack/<provider>/<account>/ (gitignored): mapping.csv, fails.csv,
gaps.md, apply-instructions.md, summary.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud_pack.generator import build_pack, load_spec, parse_controls, parse_evaluations  # noqa: E402
from scripts._runtime import (  # noqa: E402
    resolve_run_dir, preflight_writable, link_latest, setup_run_log)

# Campos del connector con el id de cuenta, por proveedor (verificado live AWS/GCP).
_ACCOUNT_KEYS = {
    "aws": ("awsAccountId", "accountId", "baseAccountId"),
    "azure": ("subscriptionId", "accountId"),
    "gcp": ("projectId",),
    "oci": ("tenancyId", "tenantId", "accountId"),
}


def _http_ok(code: int, text: str, what: str) -> None:
    if code != 200:
        raise SystemExit(f"[error] {what}: HTTP {code}\n{text[:500]}")


def _fetch_all_evaluations(client, provider, account, page_size=500, max_pages=200):
    """Lee TODAS las páginas de evaluations/{account} (paginación Spring: content/last/number).
    Solo GETs read-only. Devuelve la lista plana de items."""
    items, page = [], 0
    while page < max_pages:
        code, text = client.list_evaluations(provider, account,
                                              params={"pageSize": page_size, "pageNo": page})
        _http_ok(code, text, f"{provider}/evaluations/{account} (page {page})")
        obj = json.loads(text)
        content = obj.get("content") if isinstance(obj, dict) else obj
        if not content:
            break
        items.extend(content)
        if not isinstance(obj, dict) or obj.get("last") is True or len(content) < page_size:
            break
        page += 1
    return items


def _discover_accounts(client, provider):
    """Lista los connectors del provider (GET read-only) y extrae los account/subscription/project ids."""
    try:
        code, text = client.list_connectors(provider)
    except Exception:
        return []
    if code != 200:
        return []
    obj = json.loads(text)
    items = obj.get("content") if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
    seen, out = set(), []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        for k in _ACCOUNT_KEYS.get(provider, ()):
            v = c.get(k)
            if v and str(v) not in seen:
                seen.add(str(v))
                out.append(str(v))
                break
    return out


def _run_one(client, spec, provider, account, out_base):
    """Corre el pipeline para una cuenta: fetch evaluations -> classify -> emit. Read-only."""
    items = _fetch_all_evaluations(client, provider, account)
    controls = parse_controls(items)
    posture = parse_evaluations(items)
    out_dir = str(Path(out_base) / provider / (account or "default"))
    stats = build_pack(controls, posture, spec, out_dir, provider=provider, account=account)
    print(f"  [{provider}/{account}] {stats['controls']} ctrl · {stats['fails']} FAIL · "
          f"{stats['gaps']} a revisar · {stats['by_family']}", flush=True)
    return {"provider": provider, "account": account, "out_dir": out_dir, **stats}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pack de cobertura cloud-posture (CSPM), read-only.")
    ap.add_argument("--provider", default="all", choices=["all", "aws", "azure", "gcp", "oci"],
                    help="Proveedor cloud, o 'all' para auto-descubrir todos (default).")
    ap.add_argument("--account", default="",
                    help="Account/subscription/project/tenant id. Vacío = auto-descubrir vía connectors.")
    ap.add_argument("--spec", default=None, help="Spec YAML cloud (default: mapping/ley21719-cloud.yaml).")
    ap.add_argument("--out", default="artifacts/cloud-pack", help="Directorio de salida.")
    ap.add_argument("--fixture", default=None, help="JSON local {controls,evaluations} para correr SIN tenant.")
    ap.add_argument("--pod", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None, help="Override del gateway CSPM si difiere del host FO.")
    args = ap.parse_args(argv)

    spec = load_spec(args.spec)

    base = Path(args.out)
    preflight_writable(base)                       # falla rápido ANTES de tocar el tenant
    run_dir, run_id = resolve_run_dir(base)        # artifacts/cloud-pack/<run_id>/<provider>/<account>
    run_dir.mkdir(parents=True, exist_ok=True)
    out_base = str(run_dir)
    log = setup_run_log(run_dir)
    log.info(f"start run_id={run_id} mode={'fixture' if args.fixture else 'live'} "
             f"provider={args.provider} account={args.account or 'auto'}")

    if args.fixture:
        blob = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        evals = blob.get("evaluations", [])
        # como en live: si no hay 'controls' explícitos, se derivan de las evaluations.
        controls = parse_controls(blob.get("controls") or evals)
        posture = parse_evaluations(evals)
        prov = args.provider if args.provider != "all" else "aws"
        acct = args.account or "fixture"
        out_dir = str(Path(out_base) / prov / acct)
        stats = build_pack(controls, posture, spec, out_dir, provider=prov, account=acct)
        link_latest(run_dir)
        log.info(f"fixture done controls={stats['controls']} fails={stats['fails']} out={out_dir}")
        print(f"[fixture] {stats['controls']} ctrl · {stats['fails']} FAIL -> {out_dir}/")
        return 0

    if args.account and args.provider == "all":
        raise SystemExit("[error] --account requiere un --provider específico (no 'all').")

    from qualys_client import CloudViewClient
    from qualys_client.cloudview import from_env as cv_from_env
    if args.pod and args.user and args.password:
        client = CloudViewClient(args.pod, args.user, args.password, server=args.server)
    else:
        client = cv_from_env(server=args.server)
    print(f"[cloud] host={client.server}", flush=True)

    providers = ["aws", "azure", "gcp", "oci"] if args.provider == "all" else [args.provider]
    ran = 0
    for prov in providers:
        accounts = [args.account] if args.account else _discover_accounts(client, prov)
        if not accounts:
            print(f"  [{prov}] sin connectors/cuentas — skip", flush=True)
            log.info(f"{prov} skip (sin connectors/cuentas)")
            continue
        for acct in accounts:
            try:
                r = _run_one(client, spec, prov, acct, out_base)
                log.info(f"{prov}/{acct} controls={r['controls']} fails={r['fails']} gaps={r['gaps']}")
                ran += 1
            except SystemExit as e:
                print(f"  [{prov}/{acct}] ERROR: {e}", flush=True)
                log.info(f"{prov}/{acct} ERROR {e}")
    link_latest(run_dir)
    log.info(f"done accounts={ran} calls={client.call_count} out={out_base}")
    print(f"[cloud] {ran} cuenta(s) procesada(s) · {client.call_count} GETs -> {out_base}/"
          f" (también: {run_dir.parent / 'latest'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
