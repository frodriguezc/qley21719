#!/usr/bin/env python3
"""Tests (sin red) del drift report y los helpers de merge in-place (subir-merge.sh).

Corre standalone:  .venv/bin/python tests/test_drift.py
Cubre:
  - compliance_pack.drift: walk_controls / diff_cids (missing/extra/changed) / render_md.
  - tenant_coverage_pack: _match_ley_policies (match por nombre/ley) y _write_subir_merge_sh
    (preview_merge=1 presente, commit comentado, id o placeholder).
"""
from __future__ import annotations

import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from compliance_pack import drift  # noqa: E402
import scripts.tenant_coverage_pack as tcp  # noqa: E402


def _policy_xml(controls):
    """controls = [(cid, criticality, [tech_ids])] -> POLICY_EXPORT_OUTPUT minimo."""
    parts = ['<POLICY_EXPORT_OUTPUT><RESPONSE><POLICY><SECTIONS><SECTION><CONTROLS>']
    for cid, crit, techs in controls:
        techs_xml = "".join(f"<TECHNOLOGY><ID>{t}</ID></TECHNOLOGY>" for t in techs)
        parts.append(
            f"<CONTROL><ID>{cid}</ID><CRITICALITY><VALUE>{crit}</VALUE></CRITICALITY>"
            f"<TECHNOLOGIES>{techs_xml}</TECHNOLOGIES></CONTROL>")
    parts.append("</CONTROLS></SECTION></SECTIONS></POLICY></RESPONSE></POLICY_EXPORT_OUTPUT>")
    return ET.fromstring("".join(parts))


# --- drift.walk_controls --------------------------------------------------- #
def test_walk_controls_reads_cid_criticality_tech():
    root = _policy_xml([("100", 4, ["1", "2"]), ("200", 2, [])])
    w = drift.walk_controls(root)
    assert set(w) == {"100", "200"}
    assert w["100"]["criticality"] == 4
    assert w["100"]["tech_ids"] == frozenset({"1", "2"})
    assert w["200"]["tech_ids"] == frozenset()


def test_walk_controls_bad_criticality_defaults_zero():
    root = ET.fromstring(
        "<P><CONTROL><ID>9</ID><CRITICALITY><VALUE>x</VALUE></CRITICALITY>"
        "<TECHNOLOGIES/></CONTROL></P>")
    assert drift.walk_controls(root)["9"]["criticality"] == 0


# --- drift.diff_cids ------------------------------------------------------- #
def test_diff_missing_extra_changed():
    live = drift.walk_controls(_policy_xml([("1", 3, ["10"]), ("2", 2, ["10"]), ("3", 4, ["10"])]))
    gen = drift.walk_controls(_policy_xml([("2", 5, ["10", "11"]), ("3", 4, ["10"]), ("4", 1, ["12"])]))
    d = drift.diff_cids(live, gen)
    assert d["missing_from_live"] == ["4"]          # en gen, no en live
    assert d["extra_in_live"] == ["1"]              # en live, no en gen
    chg = dict(d["changed"])
    assert "2" in chg and "criticidad 2->5" in chg["2"] and "+tech ['11']" in chg["2"]
    assert "3" not in chg                            # identico -> no aparece


def test_diff_cid_numeric_sort():
    live = drift.walk_controls(_policy_xml([]))
    gen = drift.walk_controls(_policy_xml([("10", 1, []), ("2", 1, []), ("100", 1, [])]))
    assert drift.diff_cids(live, gen)["missing_from_live"] == ["2", "10", "100"]


def test_render_md_smoke():
    live = drift.walk_controls(_policy_xml([("1", 3, [])]))
    gen = drift.walk_controls(_policy_xml([("2", 3, [])]))
    md = drift.render_md(drift.diff_cids(live, gen), "Policy viva", "Pack nuevo", "sensible", "777")
    assert "# Drift" in md and "id `777`" in md
    assert "1 a sumar" in md and "1 solo en la viva" in md


# --- tenant_coverage_pack._match_ley_policies ------------------------------ #
def test_match_ley_policies_by_name_and_law_ref():
    pols = [("1", "Ley 21.719 - ACME (sensible)"),
            ("2", "CIS Ubuntu 22.04"),
            ("3", "Politica 21719 base"),
            ("4", "Otra cosa")]
    hit_ids = {pid for pid, _ in tcp._match_ley_policies(pols, "Ley 21.719 - ACME")}
    assert "1" in hit_ids            # match por nucleo del nombre
    assert "3" in hit_ids            # match por referencia a la ley (21719)
    assert "2" not in hit_ids and "4" not in hit_ids


# --- tenant_coverage_pack._write_subir_merge_sh ---------------------------- #
def _levels(tmp):
    d = Path(tmp) / "sensible"
    d.mkdir(parents=True, exist_ok=True)
    (d / "policy.xml").write_text("<x/>", encoding="utf-8")
    return {"sensible": {"out_dir": str(d), "included": 42}}


def test_subir_merge_has_preview_and_commented_commit():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "subir-merge.sh"
        tcp._write_subir_merge_sh(p, "https://qualysapi.qg3.apps.qualys.com",
                                  _levels(tmp), "Ley 21.719 - ACME", "555", [("555", "x")])
        s = p.read_text()
        assert 'POLICY_ID="555"' in s
        assert "action=merge&id=$POLICY_ID&update_existing_controls=1&preview_merge=1" in s
        assert "PASO 1 — PREVIEW" in s and "PASO 2 — COMMIT" in s
        # el commit real va comentado (no se ejecuta sin que el cliente lo descomente)
        assert "# curl -sS" in s


def test_subir_merge_placeholder_when_no_single_match():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "subir-merge.sh"
        tcp._write_subir_merge_sh(p, "https://qualysapi.qg3.apps.qualys.com",
                                  _levels(tmp), "Ley", None, [("1", "a"), ("2", "b")])
        s = p.read_text()
        assert 'POLICY_ID="<EXISTING_POLICY_ID>"' in s
        assert "candidato id=1" in s and "candidato id=2" in s


def test_subir_sh_has_4mib_guard_and_import():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "subir.sh"
        tcp._write_subir_sh(p, "https://qualysapi.qg3.apps.qualys.com",
                            _levels(tmp), "Ley 21.719 - ACME")
        s = p.read_text()
        # guard del cap de 4 MiB de la API + ruta GUI (Import from XML)
        assert "API_MAX_BYTES=4194304" in s and "_too_big" in s
        assert "Import from XML" in s
        # el import por API sigue presente (para policies <= 4 MiB)
        assert "action=import" in s and "create_user_controls=0" in s
        # estructura: si es grande NO llama a la API (el curl va en el else)
        assert "if _too_big" in s and "else" in s


def test_subir_merge_has_4mib_guard():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "subir-merge.sh"
        tcp._write_subir_merge_sh(p, "https://qualysapi.qg3.apps.qualys.com",
                                  _levels(tmp), "Ley 21.719 - ACME", "555", [("555", "x")])
        s = p.read_text()
        assert "API_MAX_BYTES=4194304" in s and "_too_big" in s
        # el preview sigue emitiéndose (bajo el else) y el commit comentado
        assert "preview_merge=1" in s and "# curl -sS" in s


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
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERR   {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
