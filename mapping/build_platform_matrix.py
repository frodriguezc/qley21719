#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_platform_matrix.py — genera `ley21719_platform_coverage_matrix.csv` a partir de
`platform_coverage.yaml` (mapeo Ley 21.719 -> TODAS las capacidades de la plataforma
Qualys, no solo Policy Compliance).

Es la vista PLANA (long format): una fila por par (obligación × módulo). Las obligaciones
sin pata Qualys (out-of-scope / zero) emiten una sola fila con módulo "—". 100% DERIVADO
del YAML (fuente única): para ampliar/corregir cobertura se edita el YAML, no este CSV.

Complementa build_coverage_matrix.py (que cubre el plano OPERATIVO: benchmarks PC + cloud
importables). Este cubre el plano de POSICIONAMIENTO: qué módulo aporta qué a cada obligación,
con tipo de cobertura y grounding. Técnico-comercial, NO contrato.

Determinista, solo `pyyaml` + `csv`. NO toca el tenant.
Run:  python mapping/build_platform_matrix.py   (desde la raíz del repo o desde mapping/)
"""
import csv
import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, "platform_coverage.yaml")
OUT = os.path.join(HERE, "ley21719_platform_coverage_matrix.csv")

COLUMNS = [
    "obligation_id", "article", "obligation", "coverage_type",
    "module_key", "module_name", "module_domain", "module_role",
    "grounding", "scope_note",
]


def main():
    with open(SPEC, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)

    modules = {m["key"]: m for m in spec.get("modules", [])}
    rows = []
    for ob in spec.get("obligations", []):
        mod_keys = ob.get("modules", []) or []
        if not mod_keys:
            # Obligación sin pata Qualys: una fila marcador.
            rows.append({
                "obligation_id": ob["id"], "article": ob.get("article", ""),
                "obligation": ob.get("title", ""), "coverage_type": ob.get("coverage_type", ""),
                "module_key": "—", "module_name": "—", "module_domain": "",
                "module_role": "", "grounding": "",
                "scope_note": ob.get("scope_note", ""),
            })
            continue
        for mk in mod_keys:
            m = modules.get(mk, {})
            rows.append({
                "obligation_id": ob["id"], "article": ob.get("article", ""),
                "obligation": ob.get("title", ""), "coverage_type": ob.get("coverage_type", ""),
                "module_key": mk, "module_name": m.get("name", mk),
                "module_domain": m.get("domain", ""), "module_role": m.get("primary_role", ""),
                "grounding": m.get("grounding", ""), "scope_note": ob.get("scope_note", ""),
            })

    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    obs = spec.get("obligations", [])
    by_type = {}
    for ob in obs:
        by_type[ob.get("coverage_type", "?")] = by_type.get(ob.get("coverage_type", "?"), 0) + 1
    pending = sum(1 for a in spec.get("assumptions", []) if a.get("status") == "pending")
    print(f"OK → {OUT}")
    print(f"   {len(rows)} filas (obligación×módulo) · {len(obs)} obligaciones · {len(modules)} módulos")
    print("   por tipo de cobertura: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    print(f"   ASSUMPTIONs pendientes de re-anclar: {pending}")


if __name__ == "__main__":
    main()
