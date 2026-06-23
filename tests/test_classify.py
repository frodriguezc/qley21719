#!/usr/bin/env python3
"""Unit tests (sin dependencias) de la clasificacion del compliance pack.

Corre standalone:  .venv/bin/python tests/test_classify.py
Cubre `_classify` (sub-first -> category -> fallback), las rutas de trazabilidad,
la retrocompat (sin bloque `classification`), la precedencia del exclude y `_cid_sort_key`.
No toca el tenant ni el cache: arma `cats`/`families` sinteticos en memoria.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compliance_pack.generator import _classify, _cid_sort_key  # noqa: E402

# --- fixtures sinteticos (no son el spec real) ---------------------------- #
FAMILIES = [
    {"id": "acceso",
     "match": {"categories": ["Access Control Requirements"], "sub_categories": ["DB Access Controls"]}},
    {"id": "cifrado",
     "match": {"categories": ["Encryption"], "sub_categories": ["DB Encryption"]}},
    {"id": "hardening",
     "match": {"categories": ["OS Security Settings"], "sub_categories": ["DB Specific Settings"]}},
]
CLS = {"default_family": "hardening", "exclude_sub_categories": ["DB Performance and Tuning"]}


def _cats(cat, sub):
    return {"X": {"category": cat, "sub_category": sub}}


def test_sub_first_wins_over_category_and_default():
    # 'DB Access Controls' es sub de acceso; el control esta en CATEGORY 'Database Settings'
    # (no mapeada) -> SUB-first manda: acceso, no hardening-por-default.
    cats = _cats("Database Settings", "DB Access Controls")
    assert _classify("X", cats, FAMILIES, CLS) == ("acceso", "sub")


def test_category_backbone():
    cats = _cats("OS Security Settings", "System Settings (OSI layers 6-7)")
    assert _classify("X", cats, FAMILIES, CLS) == ("hardening", "category")


def test_default_family_fallback_for_unmatched_category():
    # CATEGORY desconocida + SUB no mapeado + no excluido -> cae en default_family.
    cats = _cats("Some Future Category", "Some New Subcat")
    assert _classify("X", cats, FAMILIES, CLS) == ("hardening", "default")


def test_exclude_drops_intentionally():
    cats = _cats("Database Settings", "DB Performance and Tuning")
    assert _classify("X", cats, FAMILIES, CLS) == (None, "excluded")


def test_no_meta_returns_no_meta():
    # CID ausente del catalogo -> None aunque haya default_family (no lo agarra el default).
    assert _classify("MISSING", {}, FAMILIES, CLS) == (None, "no_meta")


def test_retrocompat_without_classification():
    # Sin bloque classification: lo que no matchea queda 'unmatched' (None), como antes.
    cats = _cats("Some Future Category", "Some New Subcat")
    assert _classify("X", cats, FAMILIES, None) == (None, "unmatched")
    # y un sub que antes se excluia ahora simplemente no matchea -> unmatched (no 'excluded').
    cats2 = _cats("Database Settings", "DB Performance and Tuning")
    assert _classify("X", cats2, FAMILIES, None) == (None, "unmatched")


def test_exclude_is_fallback_not_gate():
    # Un sub presente a la vez en una familia Y en exclude -> GANA la familia (precedencia SUB).
    fams = [{"id": "acceso",
             "match": {"categories": [], "sub_categories": ["DB Performance and Tuning"]}}]
    cls = {"default_family": "acceso", "exclude_sub_categories": ["DB Performance and Tuning"]}
    cats = _cats("Database Settings", "DB Performance and Tuning")
    assert _classify("X", cats, fams, cls) == ("acceso", "sub")


def test_empty_sub_uses_category():
    # SUB vacio no debe matchear ninguna sub-regla (el guard `if sub and ...`); usa CATEGORY.
    cats = _cats("OS Security Settings", "")
    assert _classify("X", cats, FAMILIES, CLS) == ("hardening", "category")


def test_cid_sort_key_numeric_order():
    assert sorted(["10", "2", "1", "100"], key=_cid_sort_key) == ["1", "2", "10", "100"]


def test_cid_sort_key_udc_after_numeric():
    # CIDs no numericos (UDCs) van detras de los numericos, sin crashear int().
    assert sorted(["10", "UDC_zz", "2", "udc_a"], key=_cid_sort_key) == ["2", "10", "UDC_zz", "udc_a"]


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e or 'assertion'}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
