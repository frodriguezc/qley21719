"""Drift report (READ-ONLY): compara la policy Ley YA importada en el tenant (export live) contra
el pack recién generado. Es 100% de LECTURA: no hay import/merge; solo exporta ambas policies y
reconcilia sus CIDs.

Este módulo es PURO y testeable offline: opera sobre `xml.etree` y dicts. El caller hace el export
live (vía el cliente read-only) y la lectura del policy.xml generado; acá solo se camina y se difta.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET


def _cid_key(cid: str):
    s = str(cid)
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def walk_controls(root: ET.Element) -> dict:
    """{cid: {criticality:int, tech_ids:frozenset}} desde un POLICY_EXPORT_OUTPUT (export live) o un
    policy.xml generado (mismo esquema CONTROL/ID/CRITICALITY/TECHNOLOGIES)."""
    out: dict[str, dict] = {}
    for c in root.findall(".//CONTROL"):
        cid = (c.findtext("ID") or "").strip()
        if not cid:
            continue
        try:
            crit = int((c.findtext("CRITICALITY/VALUE") or "").strip())
        except ValueError:
            crit = 0
        tids = frozenset(
            (t.findtext("ID") or "").strip()
            for t in c.findall(".//TECHNOLOGY")
            if (t.findtext("ID") or "").strip()
        )
        out[cid] = {"criticality": crit, "tech_ids": tids}
    return out


def diff_cids(live: dict, generated: dict, ui_safe: bool = False) -> dict:
    """Set-math sobre dos {cid: {criticality, tech_ids}}:
      - missing_from_live: CIDs del pack nuevo que NO están en la policy viva (cobertura a sumar).
      - extra_in_live:     CIDs en la policy viva que el pack nuevo NO trae (tuning del cliente,
                           controles de una versión vieja del benchmark, o de otro alcance).
      - changed:           CIDs comunes con criticidad y/o tecnologías distintas.
    En ui_safe el policy.xml generado conserva ID/CRITICALITY/TECHNOLOGIES (suficiente para diftar);
    igualmente las diferencias de criticidad pueden reflejar el ajuste de riesgo del cliente."""
    live_ids, gen_ids = set(live), set(generated)
    missing = sorted(gen_ids - live_ids, key=_cid_key)
    extra = sorted(live_ids - gen_ids, key=_cid_key)
    changed = []
    for cid in sorted(live_ids & gen_ids, key=_cid_key):
        lv, gv = live[cid], generated[cid]
        diffs = []
        if lv["criticality"] != gv["criticality"]:
            diffs.append(f"criticidad {lv['criticality']}->{gv['criticality']}")
        if lv["tech_ids"] != gv["tech_ids"]:
            add = sorted(gv["tech_ids"] - lv["tech_ids"], key=_cid_key)
            rem = sorted(lv["tech_ids"] - gv["tech_ids"], key=_cid_key)
            parts = []
            if add:
                parts.append(f"+tech {add}")
            if rem:
                parts.append(f"-tech {rem}")
            diffs.append(" ".join(parts))
        if diffs:
            changed.append((cid, "; ".join(diffs)))
    return {"missing_from_live": missing, "extra_in_live": extra, "changed": changed}


def render_md(diff: dict, live_title: str, gen_title: str, gen_level: str,
              live_pid: str, ui_safe: bool = False) -> str:
    """Markdown del drift. No prescribe escritura: solo informa. La acción (re-importar o usar
    subir-merge.sh) la decide y la corre el cliente (human-gate)."""
    miss, extra, chg = diff["missing_from_live"], diff["extra_in_live"], diff["changed"]
    L = [
        "# Drift — policy Ley importada vs pack regenerado",
        "",
        f"- Policy viva (tenant): **{live_title}** (id `{live_pid}`)",
        f"- Pack regenerado (nivel `{gen_level}`): **{gen_title}**",
        "",
        "Comparación READ-ONLY de CIDs (la herramienta no muta nada). Decidí vos cómo conciliar:",
        "re-importar como policy nueva, o usar `subir-merge.sh` (merge in-place, con dry-run previo).",
        "",
        f"## Resumen: {len(miss)} a sumar · {len(extra)} solo en la viva · {len(chg)} cambiados",
        "",
        "## [+] En el pack nuevo, faltan en la policy viva (cobertura a sumar)",
    ]
    if miss:
        L += [f"- {c}" for c in miss[:300]]
        if len(miss) > 300:
            L.append(f"- … (+{len(miss) - 300} más)")
    else:
        L.append("- (ninguno — la policy viva ya cubre todos los CIDs del pack)")

    L += ["", "## [-] Solo en la policy viva (no en el pack nuevo)",
          "_Tuning del cliente, controles de una versión previa del benchmark, o de otro alcance. "
          "Un merge in-place con `update_existing_controls=1` NO los borra; un re-import como policy "
          "nueva no los arrastra._"]
    if extra:
        L += [f"- {c}" for c in extra[:300]]
        if len(extra) > 300:
            L.append(f"- … (+{len(extra) - 300} más)")
    else:
        L.append("- (ninguno)")

    L += ["", "## [~] Comunes con diferencias (criticidad / tecnologías)"]
    if ui_safe:
        L.append("_Nivel ui_safe: criticidad y tech IDs se conservan; una diferencia de criticidad "
                 "puede ser el ajuste de riesgo del cliente (Art. 14 septies), no un error._")
    if chg:
        L += [f"- {cid} — {d}" for cid, d in chg[:300]]
        if len(chg) > 300:
            L.append(f"- … (+{len(chg) - 300} más)")
    else:
        L.append("- (ninguno)")
    L.append("")
    return "\n".join(L)
