#!/usr/bin/env python3
"""reconcile.py — reconciliación PC (host) + cloud (recurso) de la cobertura Ley 21.719 (read-only).

Implementa la DECISIÓN F1 (DESIGN-cloud-posture.md §8): joinea los dos `mapping.csv` que ya emiten
los dos motores read-only del repo —Policy Compliance (host) y cloud-posture/CSPM (recurso)— por
`family`, y produce una vista por-artículo como UNIÓN etiquetada por sustrato. **No fusiona los
packs, no suma entre planos, no toca el tenant** (solo lee dos CSV). Ver cloud_pack/reconcile.py.

Uso:
    # ambos planos (lo normal):
    python scripts/reconcile.py \
        --pc    artifacts/tenant-pack/<cliente>/latest/sensible/mapping.csv \
        --cloud artifacts/cloud-pack/latest/aws/<account>/mapping.csv

    # un solo plano (el otro se reporta 'no provisto', NO como gap):
    python scripts/reconcile.py --cloud .../mapping.csv --stdout

Salida: artifacts/reconcile/<run_id>/coverage-by-article.md (gitignored) + symlink `latest`,
o a stdout con --stdout.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud_pack.reconcile import (  # noqa: E402
    read_mapping_csv, reconcile, render_markdown, write_report)
from cloud_pack.generator import load_spec  # noqa: E402
from scripts._runtime import (  # noqa: E402
    preflight_writable, resolve_run_dir, link_latest, utc_run_id)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reconciliación PC + cloud por artículo (read-only).")
    ap.add_argument("--pc", default=None, help="mapping.csv del pack PC (compliance_pack).")
    ap.add_argument("--cloud", default=None, help="mapping.csv del pack cloud (cloud_pack).")
    ap.add_argument("--spec", default=None, help="Spec YAML cloud (default: mapping/ley21719-cloud.yaml).")
    ap.add_argument("--out", default="artifacts/reconcile", help="Directorio base de salida.")
    ap.add_argument("--stdout", action="store_true", help="Imprimir el markdown en vez de escribir archivo.")
    args = ap.parse_args(argv)

    if not args.pc and not args.cloud:
        ap.error("se requiere al menos --pc o --cloud (uno de los dos planos).")

    for label, p in (("--pc", args.pc), ("--cloud", args.cloud)):
        if p and not Path(p).is_file():
            raise SystemExit(f"[error] {label}: no existe el archivo {p!r}")

    spec = load_spec(args.spec)
    pc_rows = read_mapping_csv(args.pc)        # None si no se pasó --pc (plano no provisto)
    cloud_rows = read_mapping_csv(args.cloud)
    recon = reconcile(pc_rows, cloud_rows, spec)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.stdout:
        sys.stdout.write(render_markdown(recon, pc_path=args.pc, cloud_path=args.cloud,
                                         generated_at=generated_at))
        return 0

    base = Path(args.out)
    preflight_writable(base)
    run_dir, _ = resolve_run_dir(base, run_id=utc_run_id())
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(run_dir / "coverage-by-article.md")
    write_report(recon, out_path, pc_path=args.pc, cloud_path=args.cloud, generated_at=generated_at)
    link_latest(run_dir)

    sc = recon["scope"]
    covered = sum(1 for f in recon["families"] if f["pc_covered"] or f["cloud_covered"])
    print(f"[reconcile] {covered}/{len(recon['families'])} familias con cobertura "
          f"(PC: {sc['pc_controls']} ctrl · cloud: {sc['cloud_controls']} ctrl) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
