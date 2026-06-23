#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_coverage_matrix.py — genera `ley21719_coverage_matrix.csv` a partir del catálogo
curado (`cis_catalog.yaml`) + el spec de la ley (`ley21719.yaml`).

Es la vista PLANA (una fila por módulo/tecnología) del mapeo Ley 21.719 → controles
técnicos de Qualys, para cargarse en el flujo que arma la política y como matriz de
cobertura reviewable/extensible. Es 100% DERIVADO de los dos YAML (fuente única) → para
ampliar cobertura se editan los YAML, no este script ni el CSV.

Cada fila une un módulo con los ARTÍCULOS reales de la ley (no solo las familias `pillars`):
el join `pillars → families[].law_refs` de `ley21719.yaml`.

Cobertura en DOS planos (ambos salen de cis_catalog.yaml):
  - `targets` (PC): host OS, DB, web/middleware, contenedores (Docker/K8s), virtualización,
    red → SÍ entran en un `policy.xml` de Policy Compliance (pc_importable=yes).
  - `additional_domains`: cloud posture (AWS/Azure/GCP/OCI) e identidad (Entra cloud / AD) →
    cloud = OTRO motor (TotalCloud/CloudView CSPM), NO entra en el policy.xml de PC
    (pc_importable=no). AD sí es PC (vía el perfil DC del CIS Windows Server).

Determinista, solo `pyyaml` + `csv` (mismas deps que el producto). NO toca el tenant.
Run:  python mapping/build_coverage_matrix.py   (desde la raíz del repo o desde mapping/)
"""
import csv
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(HERE, "cis_catalog.yaml")
LAW = os.path.join(HERE, "ley21719.yaml")
OUT = os.path.join(HERE, "ley21719_coverage_matrix.csv")

COLUMNS = [
    "key", "group", "kind", "qualys_app", "control_system", "pc_importable",
    "benchmark", "cis_version", "pillars", "law_articles", "status", "notes",
]


def _article(law_ref: str) -> str:
    """De 'Art. 14 quinquies b) — confidencialidad…' devuelve solo 'Art. 14 quinquies b)'."""
    return re.split(r"\s+[—-]\s+", law_ref.strip(), maxsplit=1)[0].strip()


def _law_articles_index(law: dict) -> dict:
    """family_id -> lista de artículos cortos."""
    return {f["id"]: [_article(r) for r in f.get("law_refs", [])]
            for f in law.get("families", [])}


def _articles_for(pillars, idx) -> str:
    arts = []
    for p in pillars:
        for a in idx.get(p, []):
            if a not in arts:
                arts.append(a)
    return "; ".join(sorted(arts))


def main():
    with open(CATALOG, encoding="utf-8") as fh:
        catalog = yaml.safe_load(fh)
    with open(LAW, encoding="utf-8") as fh:
        law = yaml.safe_load(fh)
    idx = _law_articles_index(law)

    rows = []
    # 1) Targets PC del catálogo (host OS / DB / middleware / contenedores / virt / red).
    for t in catalog.get("targets", []):
        pillars = t.get("pillars", [])
        rows.append({
            "key": t["key"], "group": t.get("group", ""), "kind": t.get("kind", ""),
            "qualys_app": "Policy Compliance", "control_system": "pc-library",
            "pc_importable": "yes", "benchmark": t.get("benchmark", ""),
            "cis_version": t.get("cis_version", ""),
            "pillars": "; ".join(pillars), "law_articles": _articles_for(pillars, idx),
            "status": "active", "notes": "",
        })
    # 2) Dominios adicionales (cloud posture / identidad) — del mismo catálogo.
    for o in catalog.get("additional_domains", []):
        pillars = o.get("pillars", [])
        rows.append({
            "key": o["key"], "group": o.get("group", ""), "kind": o.get("kind", ""),
            "qualys_app": o.get("qualys_app", ""),
            "control_system": o.get("control_system", ""),
            "pc_importable": "yes" if o.get("pc_importable") else "no",
            "benchmark": o.get("benchmark", ""),
            "cis_version": o.get("cis_version", ""),
            "pillars": "; ".join(pillars), "law_articles": _articles_for(pillars, idx),
            "status": o.get("status", "planned"), "notes": o.get("notes", ""),
        })

    with open(OUT, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    pc = sum(1 for r in rows if r["pc_importable"] == "yes")
    print(f"OK → {OUT}")
    print(f"   {len(rows)} filas · {pc} PC-importables · {len(rows) - pc} otro motor (cloud/identidad)")


if __name__ == "__main__":
    main()
