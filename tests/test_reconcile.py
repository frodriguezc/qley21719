#!/usr/bin/env python3
"""Unit tests (sin red, sin tenant) de la reconciliación PC + cloud (DECISIÓN F1).

Corre standalone:  .venv/bin/python tests/test_reconcile.py
Usa el spec REAL (mapping/ley21719-cloud.yaml) para las familias legales y prueba la agregación,
los veredictos de UNIÓN, el bucketing de posture, la semántica None-vs-[] y el render + la CLI.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud_pack.generator import load_spec  # noqa: E402
from cloud_pack.reconcile import (  # noqa: E402
    legal_families, read_mapping_csv, reconcile, render_markdown)

SPEC = load_spec()


def _pc_row(family):
    return {"cid": "1", "family": family, "law_refs": "Art. X", "statement": "s"}


def _cloud_row(family, posture="FAIL", provider="aws", account="111122223333"):
    return {"cid": "11", "control_name": "Ensure thing", "family": family,
            "posture": posture, "provider": provider, "account": account, "law_articles": "Art. X"}


def _by_family(recon):
    return {f["family"]: f for f in recon["families"]}


def test_legal_families_excludes_no_aplica():
    fams = legal_families(SPEC)
    ids = [f["id"] for f in fams]
    assert "no_aplica" not in ids, "no_aplica NO es una familia legal (sin law_articles)"
    assert set(ids) == {"acceso", "cifrado", "auditoria", "hardening", "disponibilidad"}


def test_verdict_pc_plus_cloud():
    recon = reconcile([_pc_row("acceso")], [_cloud_row("acceso")], SPEC)
    assert _by_family(recon)["acceso"]["verdict"] == "PC + cloud"


def test_verdict_solo_pc_when_cloud_provided_but_empty_for_family():
    # cloud provisto pero SIN controles de 'cifrado' -> 'solo PC' (no doble cuenta cloud).
    recon = reconcile([_pc_row("cifrado")], [_cloud_row("acceso")], SPEC)
    assert _by_family(recon)["cifrado"]["verdict"] == "solo PC (host)"


def test_verdict_solo_cloud():
    recon = reconcile([_pc_row("acceso")], [_cloud_row("auditoria")], SPEC)
    assert _by_family(recon)["auditoria"]["verdict"] == "solo cloud (recurso)"


def test_verdict_sin_cobertura_marks_only_provided_planes():
    # ambos packs provistos pero 'disponibilidad' no aparece en ninguno -> 'sin cobertura en PC+cloud'.
    recon = reconcile([_pc_row("acceso")], [_cloud_row("acceso")], SPEC)
    assert _by_family(recon)["disponibilidad"]["verdict"] == "sin cobertura en PC+cloud"


def test_none_plane_is_not_a_gap():
    # solo cloud provisto (pc_rows=None) -> 'disponibilidad' sin cloud NO se marca gap por PC.
    recon = reconcile(None, [_cloud_row("acceso")], SPEC)
    bf = _by_family(recon)
    assert recon["scope"]["pc_provided"] is False
    assert bf["acceso"]["verdict"] == "solo cloud (recurso)"
    # familia sin cobertura en el ÚNICO plano provisto (cloud) -> 'sin cobertura en cloud' (no 'PC+cloud')
    assert bf["disponibilidad"]["verdict"] == "sin cobertura en cloud"


def test_posture_bucketing_pass_fail_not_eval():
    rows = [_cloud_row("auditoria", "PASS"), _cloud_row("auditoria", "FAIL"),
            _cloud_row("auditoria", "NOT_EVALUATED"), _cloud_row("auditoria", "")]
    recon = reconcile(None, rows, SPEC)
    c = _by_family(recon)["auditoria"]["cloud"]
    assert (c["pass"], c["fail"], c["not_eval"]) == (1, 1, 2)
    assert c["controls"] == 4


def test_scope_aggregates_providers_and_accounts():
    rows = [_cloud_row("acceso", provider="aws", account="A1"),
            _cloud_row("hardening", provider="gcp", account="P9")]
    recon = reconcile(None, rows, SPEC)
    assert recon["scope"]["cloud_providers"] == ["aws", "gcp"]
    assert recon["scope"]["cloud_accounts"] == ["A1", "P9"]
    assert recon["scope"]["cloud_controls"] == 2


def test_render_has_f1_rules_and_no_summed_total():
    recon = reconcile([_pc_row("acceso")], [_cloud_row("acceso", "FAIL")], SPEC)
    md = render_markdown(recon, pc_path="pc.csv", cloud_path="cl.csv", generated_at="2026-06-25T00:00:00Z")
    assert "NO sumes ni promedies" in md                 # regla anti doble conteo, horneada
    assert "UNIÓN por sustrato" in md
    assert "PC (host)" in md and "Cloud (recurso)" in md  # columnas por sustrato separadas
    assert "Scope:" in md and "2026-06-25" in md
    # el conteo va por sustrato: aparece el de host y el de cloud, nunca un combinado "2 ctrl".
    assert "1 ctrl (host)" in md and "1 ctrl" in md


def test_read_mapping_csv_none_vs_file():
    assert read_mapping_csv(None) is None
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
        fh.write("cid,family,posture,provider,account\n9,acceso,PASS,aws,A1\n")
        p = fh.name
    rows = read_mapping_csv(p)
    assert isinstance(rows, list) and rows[0]["family"] == "acceso"


def test_cli_stdout_end_to_end():
    import scripts.reconcile as cli
    with tempfile.TemporaryDirectory() as d:
        pc = Path(d) / "pc.csv"
        cl = Path(d) / "cloud.csv"
        pc.write_text("cid,family,law_refs,statement\n1,acceso,Art. X,s\n", encoding="utf-8")
        cl.write_text("cid,control_name,family,posture,provider,account,law_articles\n"
                      "11,Ensure MFA,acceso,FAIL,aws,A1,Art. X\n", encoding="utf-8")
        rc = cli.main(["--pc", str(pc), "--cloud", str(cl), "--stdout"])
    assert rc == 0


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
