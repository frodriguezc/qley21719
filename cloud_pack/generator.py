"""Generador del pack CLOUD POSTURE (CSPM) — read-only.

Pipeline (análogo conceptual al de PC, adaptado a CSPM; ver DESIGN-cloud-posture.md §4):
  harvest controls (GET controls/metadata/list)  -> universo de controles cloud por provider
  resolve posture (GET evaluations/{account})     -> control.result PASS/FAIL
  classify (control -> familia legal)              -> por keywords de mapping/ley21719-cloud.yaml
  emit                                             -> mapping.csv + gaps.md + apply-instructions.md + summary.json

NO hay policy.xml en cloud; NO se dispara ningún POST (creación de reportes/policies = mutación
con human-gate). Todo lo que toca el tenant es GET vía CloudViewClient (allow-list-only).

Los parsers son DEFENSIVOS: los shapes exactos del JSON CSPM están documentados (control.id,
control.name, control.criticality, control.result, benchmarkType) pero NO probados contra un
tenant live en este repo -> se toleran variantes ({data:[...]}, listas planas, camelCase).
"""
from __future__ import annotations

import csv
import json
import os
from collections import OrderedDict

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SPEC = os.path.join(os.path.dirname(HERE), "mapping", "ley21719-cloud.yaml")


# --------------------------------------------------------------------------- #
# Spec
# --------------------------------------------------------------------------- #
def load_spec(path: str | None = None) -> dict:
    with open(path or _DEFAULT_SPEC, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------- #
# Clasificación (control cloud -> familia legal) — data-driven por keywords
# --------------------------------------------------------------------------- #
def classify_control(name: str, spec: dict,
                     benchmark_type: str = "") -> tuple[str | None, str]:
    """control.name (+ benchmarkType) -> (family_id_o_None, route).
    route: 'priority' (frase de alta prioridad, gana antes del orden de familias), 'keyword'
    (matcheó un keyword por orden de familias), 'default' (cayó en default_family -> REVISAR en
    gaps), 'unmatched' (no matchea y no hay default). El primer family (en el orden de `families`)
    con un keyword substring gana (precedencia por orden, como el SUB-first de PC), salvo que una
    frase de `priority` matchee antes."""
    cls = spec.get("classification", {}) or {}
    hay = f"{name or ''} {benchmark_type or ''}".lower()
    # 0) overrides de ALTA prioridad: ganan ANTES del orden de familias. Para compuestos de
    # detección/registro ("log metric filter / alerts exists / X is monitored") que son auditoría
    # aunque mencionen IAM/role/mfa (que matchearían acceso primero por precedencia).
    for fid, toks in (cls.get("priority", {}) or {}).items():
        for t in toks:
            if str(t).lower() in hay:
                return fid, "priority"
    # 1) keyword por orden de familias (el primero que matchea gana)
    kw = cls.get("keywords", {}) or {}
    for fam in spec.get("families", []):
        fid = fam["id"]
        for token in kw.get(fid, []):
            if token.lower() in hay:
                return fid, "keyword"
    df = cls.get("default_family")
    return (df, "default") if df else (None, "unmatched")


# --------------------------------------------------------------------------- #
# Parsers defensivos del JSON CSPM
# --------------------------------------------------------------------------- #
def _as_list(blob):
    """Devuelve la lista de items tolerando {content:[...]}, {data:[...]}, {items:[...]} o lista plana."""
    if isinstance(blob, list):
        return blob
    if isinstance(blob, dict):
        # 'content' = paginación Spring (connectors/evaluations); 'control' (singular) =
        # controls/metadata/list. Verificado live vs el tenant CSPM (jun-2026).
        for k in ("content", "data", "items", "control", "controls", "evaluation", "evaluations", "results"):
            v = blob.get(k)
            if isinstance(v, list):
                return v
    return []


def _first(d: dict, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def parse_controls(text_or_obj) -> list[dict]:
    """JSON de controls/metadata/list O de evaluations -> [{cid, name, criticality, service,
    provider, benchmark}]. Shapes verificados live: metadata usa controlName/serviceType y la
    lista bajo 'control'; evaluations usa controlId/controlName/service y 'policyNames' (lista de
    policies; se usa como benchmark si no hay benchmarkType)."""
    blob = json.loads(text_or_obj) if isinstance(text_or_obj, str) else text_or_obj
    out = []
    for c in _as_list(blob):
        if not isinstance(c, dict):
            continue
        benchmark = _first(c, "benchmarkType", "benchmark", "control.benchmarkType")
        pol = c.get("policyNames")
        if not benchmark and isinstance(pol, list):
            benchmark = "; ".join(str(p) for p in pol)
        out.append({
            "cid": str(_first(c, "cid", "controlId", "id", "control.id")),
            "name": str(_first(c, "name", "controlName", "control.name")),
            "criticality": str(_first(c, "criticality", "control.criticality")),
            "service": str(_first(c, "serviceType", "service")),
            "provider": str(_first(c, "provider")),
            "benchmark": str(benchmark),
        })
    return out


def parse_evaluations(text_or_obj) -> dict:
    """JSON de evaluations/{account} -> {cid: 'PASS'|'FAIL'|'<raw>'}."""
    blob = json.loads(text_or_obj) if isinstance(text_or_obj, str) else text_or_obj
    posture = {}
    for e in _as_list(blob):
        if not isinstance(e, dict):
            continue
        cid = str(_first(e, "cid", "controlId", "id", "control.id"))
        if not cid:
            continue
        res = str(_first(e, "result", "controlResult", "control.result", "evaluationResult", default="")).upper()
        posture[cid] = res or "UNKNOWN"
    return posture


# --------------------------------------------------------------------------- #
# Emit
# --------------------------------------------------------------------------- #
def _articles_by_family(spec: dict) -> dict:
    return {f["id"]: list(f.get("law_articles", [])) for f in spec.get("families", [])}


def build_pack(controls: list[dict], posture: dict, spec: dict, out_dir: str,
               provider: str = "", account: str = "") -> dict:
    """Clasifica los controles, cruza con el posture (PASS/FAIL) y emite el pack read-only.
    Devuelve stats. NO muta nada; el cliente aplica por UI (human-gate)."""
    os.makedirs(out_dir, exist_ok=True)
    arts = _articles_by_family(spec)
    fam_order = [f["id"] for f in spec.get("families", [])]

    by_family: "OrderedDict[str, list]" = OrderedDict((f, []) for f in fam_order)
    gaps = []  # controles en default_family o sin clasificar -> REVISAR
    rows = []
    for c in controls:
        # el `service` (IAM/KMS/Logging/...) es señal fuerte de clasificación, junto al nombre.
        hay_extra = f"{c.get('service', '')} {c.get('benchmark', '')}".strip()
        fid, route = classify_control(c["name"], spec, hay_extra)
        result = posture.get(c["cid"], "NOT_EVALUATED")
        if fid:
            by_family.setdefault(fid, []).append(c["cid"])
        if route in ("default", "unmatched"):
            gaps.append({**c, "family": fid or "", "route": route})
        rows.append({
            "cid": c["cid"], "control_name": c["name"], "criticality": c.get("criticality", ""),
            "service": c.get("service", ""), "benchmark": c.get("benchmark", ""),
            "family": fid or "", "route": route,
            "law_articles": "; ".join(arts.get(fid, [])) if fid else "",
            "posture": result, "provider": provider, "account": account,
        })

    # mapping.csv
    cols = ["cid", "control_name", "criticality", "service", "benchmark", "family",
            "route", "law_articles", "posture", "provider", "account"]
    with open(os.path.join(out_dir, "mapping.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    fails = [r for r in rows if r["posture"] == "FAIL"]
    # control-set de FAILs (input a remediación), filtrado por los CIDs del mapping
    with open(os.path.join(out_dir, "fails.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(fails)

    _write_gaps(os.path.join(out_dir, "gaps.md"), gaps, spec)
    _write_apply_instructions(os.path.join(out_dir, "apply-instructions.md"), spec, provider, account)

    stats = {
        "provider": provider, "account": account,
        "controls": len(controls), "classified": sum(1 for r in rows if r["family"]),
        "by_family": {f: len(v) for f, v in by_family.items()},
        "gaps": len(gaps), "fails": len(fails),
        "evaluated": sum(1 for r in rows if r["posture"] not in ("NOT_EVALUATED", "UNKNOWN")),
        "no_policy_xml": True, "no_mutation": True,
    }
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2, ensure_ascii=False)
    return stats


def _write_gaps(path: str, gaps: list[dict], spec: dict) -> None:
    df = (spec.get("classification", {}) or {}).get("default_family", "")
    lines = ["# Gaps — controles cloud a REVISAR", "",
             f"Controles que cayeron en `default_family` ({df}) por keyword o que no matchearon "
             "ninguna familia. Revisar el mapeo en `mapping/ley21719-cloud.yaml` (`classification.keywords`).",
             ""]
    if not gaps:
        lines.append("(ninguno — todos los controles matchearon una familia explícita)")
    for g in gaps:
        lines.append(f"- `{g['cid']}` {g['name']}  → {g['route']} ({g.get('family') or 'sin familia'})")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_apply_instructions(path: str, spec: dict, provider: str, account: str) -> None:
    md = f"""# Cómo aplicar el pack cloud-posture (CSPM) — human-gate

> En cloud NO hay `policy.xml` importable. Este pack es READ-ONLY: la herramienta solo LEE
> posture (controls + evaluations) y emite trazabilidad. **No muta nada.** El cliente aplica.

Scope leído: provider=`{provider or '(varios)'}`  account/connector=`{account or '(según --account)'}`

1. **Revisar** `mapping.csv` (control CSPM → familia legal → artículos → PASS/FAIL) y `fails.csv`
   (los FAIL, input a remediación) y `gaps.md` (controles a revisar).
2. **Crear la Custom Policy en la UI** (no por API por defecto): `Policy > New`, elegir provider/
   executionType, asociar los controles del mapping, y asignar connectors/tags (el scope lo pone
   el cliente). Ref. DESIGN-cloud-posture.md §4.
3. (Opcional) Generar/descargar un Assessment/Mandate Report desde la consola — su **creación es
   POST (mutación)**, por eso **la herramienta no lo dispara**; lo hace el cliente.
4. Re-correr esta herramienta (read-only) para regenerar el mapping tras cualquier cambio.

NOTA de alcance: el CSPM valida la CONFIGURACIÓN (cifrado/backup/red); no cifra, no respalda, no
restaura ni clasifica el dato. Las familias `cifrado` y `disponibilidad` son config-only (gap honesto).
La Ley 21.719 NO es un mandate nativo CSPM: se puentea vía mandate afín (ISO 27001 / NIST 800-53 / GDPR).
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
