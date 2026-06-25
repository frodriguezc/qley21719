"""reconcile — vista de reconciliación PC (host) + cloud (recurso) a nivel ARTÍCULO/FAMILIA.

Implementa la DECISIÓN F1 (DESIGN-cloud-posture.md §8): los dos motores read-only de este repo
—Policy Compliance (host-based: emite `policy.xml` + `mapping.csv`) y cloud-posture / CSPM (emite
`mapping.csv` + posture PASS/FAIL)— cubren las MISMAS 5 familias legales de la Ley 21.719 por
SUSTRATOS distintos (un control sobre un host != un control sobre un recurso cloud). Este módulo
**NO fusiona los packs**: joinea sus dos `mapping.csv` por `family` y produce una vista por-artículo
que reporta la cobertura como **UNIÓN etiquetada por sustrato**.

Reglas horneadas (no se confían al lector):
  - **NUNCA** suma ni promedia cobertura entre planos (poblaciones distintas) -> doble conteo
    evitado por construcción: cada sustrato lleva su propio conteo, nunca uno combinado.
  - Un 'gap' cloud solo cuenta si HAY assets cloud en esa área (se proveyó un pack cloud con
    controles para esa familia); si no, es 'no provisto / no evaluado', NO un gap -> falsos gaps
    evitados. Análogo para PC.
  - Declara su SCOPE (qué `mapping.csv` de PC, qué providers/cuentas cloud) y queda como foto
    read-only a la fecha de lectura.

100% determinista, sin red, sin tocar el tenant: solo lee los dos CSV ya emitidos.

Esquemas de entrada (verbatim de los generadores):
  - PC   (compliance_pack):  cid, family, family_heading, law_refs, category, sub_category,
                             criticality_value, criticality_label, n_technologies, source_policies, statement
  - cloud (cloud_pack):      cid, control_name, criticality, service, benchmark, family, route,
                             law_articles, posture, provider, account
"""
from __future__ import annotations

import csv
import os

from .generator import load_spec

# Buckets de posture del lado cloud (el PC mapping.csv es definición de política, sin posture live).
_PASS = "PASS"
_FAIL = "FAIL"


def read_mapping_csv(path: str | None) -> list[dict] | None:
    """Lee un mapping.csv (PC o cloud) como lista de dicts. Devuelve None si `path` es None
    (= ese plano NO se proveyó, distinto de [] = provisto pero vacío). Tolerante: si el archivo
    no existe, levanta FileNotFoundError (el caller decide); columnas ausentes -> claves faltantes."""
    if path is None:
        return None
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def legal_families(spec: dict) -> list[dict]:
    """Las 5 familias legales en orden (las que trazan a artículos). Excluye `no_aplica`
    (bucket de exclusión sin law_articles). Fuente única: el spec cloud."""
    return [f for f in spec.get("families", []) if f.get("law_articles")]


def _aggregate_pc(rows: list[dict] | None) -> dict:
    """PC mapping.csv -> {family_id: {'controls': n}}. None -> {} (plano no provisto)."""
    out: dict[str, dict] = {}
    for r in rows or []:
        fid = (r.get("family") or "").strip()
        if not fid:
            continue
        out.setdefault(fid, {"controls": 0})
        out[fid]["controls"] += 1
    return out


def _aggregate_cloud(rows: list[dict] | None) -> dict:
    """cloud mapping.csv -> {family_id: {controls, pass, fail, not_eval, providers, accounts}}."""
    out: dict[str, dict] = {}
    for r in rows or []:
        fid = (r.get("family") or "").strip()
        if not fid:
            continue
        d = out.setdefault(fid, {"controls": 0, "pass": 0, "fail": 0, "not_eval": 0,
                                 "providers": set(), "accounts": set()})
        d["controls"] += 1
        posture = (r.get("posture") or "").strip().upper()
        if posture == _PASS:
            d["pass"] += 1
        elif posture == _FAIL:
            d["fail"] += 1
        else:                       # NOT_EVALUATED / UNKNOWN / vacío -> no hay posture live
            d["not_eval"] += 1
        if r.get("provider"):
            d["providers"].add(r["provider"])
        if r.get("account"):
            d["accounts"].add(r["account"])
    return out


def reconcile(pc_rows: list[dict] | None, cloud_rows: list[dict] | None,
              spec: dict | None = None) -> dict:
    """Joinea PC + cloud por `family` y devuelve la vista por-familia (estructura, sin render).
    `*_rows = None` = ese plano NO se proveyó (la familia no se marca gap por ese plano)."""
    spec = spec or load_spec()
    pc = _aggregate_pc(pc_rows)
    cloud = _aggregate_cloud(cloud_rows)
    pc_provided = pc_rows is not None
    cloud_provided = cloud_rows is not None

    families = []
    for fam in legal_families(spec):
        fid = fam["id"]
        p = pc.get(fid)
        c = cloud.get(fid)
        pc_cov = bool(p and p["controls"])
        cloud_cov = bool(c and c["controls"])
        # Veredicto de UNIÓN (nunca suma): qué sustrato(s) tocan el artículo.
        if pc_cov and cloud_cov:
            verdict = "PC + cloud"
        elif pc_cov:
            verdict = "solo PC (host)"
        elif cloud_cov:
            verdict = "solo cloud (recurso)"
        else:
            # sin cobertura en ningún plano provisto. Si un plano NO se proveyó, NO es gap por él.
            missing = []
            if pc_provided:
                missing.append("PC")
            if cloud_provided:
                missing.append("cloud")
            verdict = ("sin cobertura en " + "+".join(missing)) if missing else "sin packs provistos"
        families.append({
            "family": fid,
            "cloud_area": fam.get("cloud_area", ""),
            "articles": list(fam.get("law_articles", [])),
            "pc": p, "cloud": c,
            "pc_covered": pc_cov, "cloud_covered": cloud_cov,
            "verdict": verdict,
        })

    providers = sorted({pr for c in cloud.values() for pr in c["providers"]})
    accounts = sorted({a for c in cloud.values() for a in c["accounts"]})
    return {
        "families": families,
        "scope": {
            "pc_provided": pc_provided, "cloud_provided": cloud_provided,
            "cloud_providers": providers, "cloud_accounts": accounts,
            "pc_controls": sum(v["controls"] for v in pc.values()),
            "cloud_controls": sum(v["controls"] for v in cloud.values()),
        },
    }


def _cloud_cell(c: dict | None, cloud_provided: bool) -> str:
    if not cloud_provided:
        return "— (sin pack cloud)"
    if not c or not c["controls"]:
        return "— (sin assets cloud en esta área)"
    bits = f"✔ {c['controls']} ctrl"
    posture = f"{c['pass']}✓ / {c['fail']}✗"
    if c["not_eval"]:
        posture += f" / {c['not_eval']} s/eval"
    return f"{bits} · {posture}"


def _pc_cell(p: dict | None, pc_provided: bool) -> str:
    if not pc_provided:
        return "— (sin pack PC)"
    if not p or not p["controls"]:
        return "— (sin cobertura PC)"
    return f"✔ {p['controls']} ctrl (host)"


def render_markdown(recon: dict, pc_path: str | None = None, cloud_path: str | None = None,
                    generated_at: str | None = None) -> str:
    """Renderiza `coverage-by-article.md`: la vista de UNIÓN por sustrato, con las reglas F1
    horneadas en el encabezado. `generated_at` se pasa para que el render sea determinista
    (los tests no dependen del reloj)."""
    sc = recon["scope"]
    lines = [
        "# Cobertura Ley 21.719 por artículo — reconciliación PC (host) + cloud (recurso)",
        "",
        "> **Vista de UNIÓN por sustrato — NO sumes ni promedies entre planos.** Un control sobre un",
        "> **host** (Policy Compliance) y un control sobre un **recurso cloud** (CSPM) son poblaciones",
        "> distintas: cada plano lleva su propio conteo, nunca uno combinado (evita el doble conteo).",
        "> Un **gap** cloud solo cuenta si hay assets cloud en esa área (connector activo); un gap PC,",
        "> solo si hay hosts de esa tecnología. Es una **foto read-only** con el scope de abajo.",
        "",
    ]
    # Scope / procedencia
    scope_bits = []
    if pc_path:
        scope_bits.append(f"PC: `{pc_path}` ({sc['pc_controls']} controles)")
    elif not sc["pc_provided"]:
        scope_bits.append("PC: _no provisto_")
    if cloud_path:
        prov = ", ".join(sc["cloud_providers"]) or "—"
        acct = ", ".join(sc["cloud_accounts"]) or "—"
        scope_bits.append(f"cloud: `{cloud_path}` ({sc['cloud_controls']} controles · "
                          f"providers: {prov} · cuentas: {acct})")
    elif not sc["cloud_provided"]:
        scope_bits.append("cloud: _no provisto_")
    lines.append("**Scope:** " + " · ".join(scope_bits))
    if generated_at:
        lines.append(f"**Generado:** {generated_at}")
    lines += ["", "| Familia | Artículos (Ley 21.719) | PC (host) | Cloud (recurso) | Unión |",
              "|---|---|---|---|---|"]
    for f in recon["families"]:
        arts = "<br>".join(f["articles"]) or "—"
        pc_cell = _pc_cell(f["pc"], sc["pc_provided"])
        cloud_cell = _cloud_cell(f["cloud"], sc["cloud_provided"])
        lines.append(f"| **{f['family']}** — {f['cloud_area']} | {arts} | {pc_cell} | "
                     f"{cloud_cell} | {f['verdict']} |")
    lines += [
        "",
        "**Leyenda:** `✓` PASS · `✗` FAIL · `s/eval` sin evaluación live (config mapeada, sin "
        "posture). El conteo PC es definición de política (host); la posture host llega tras el "
        "scan del cliente. NOTA de alcance: las familias `cifrado` y `disponibilidad` son "
        "config-only en cloud (el CSPM valida la CONFIGURACIÓN, no el cifrado/restore per se).",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_report(recon: dict, out_path: str, **render_kw) -> str:
    """Escribe el markdown a `out_path` (crea dirs). Devuelve el path."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    md = render_markdown(recon, **render_kw)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    return out_path
