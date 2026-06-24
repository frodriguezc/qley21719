"""Generador del Compliance Pack (read-only).

Pipeline:  harvest (export de policies fuente)  ->  resolve categories (catalogo)
        -> classify (CID -> familia legal)      ->  assemble (<POLICY> import-XML)
        -> validate                              ->  emit (policy.xml + mapping.csv
                                                          + gaps.md + import-instructions.md)

Todo lo que toca el tenant es LECTURA (compliance/policy action=export, compliance/control
action=list). El cliente (qualys_client) bloquea estructuralmente cualquier escritura.
"""
from __future__ import annotations

import copy
import csv
import json
import os
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Tags cuyo contenido Qualys emite como CDATA en el export (para fidelidad del import).
_CDATA_TAGS = {
    "TITLE", "COVER_PAGE", "STATUS", "HEADING", "LABEL",
    "REFERENCE_TEXT", "IS_CONTROL_DISABLE", "EXPORTED", "V",
}


# --------------------------------------------------------------------------- #
# Serializacion XML con CDATA (ElementTree no soporta CDATA nativamente)
# --------------------------------------------------------------------------- #
def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _attrs(elem: ET.Element) -> str:
    if not elem.attrib:
        return ""
    return "".join(f' {k}="{_escape(str(v))}"' for k, v in elem.attrib.items())


def _serialize(elem: ET.Element, depth: int = 0) -> str:
    ind = "  " * depth
    kids = list(elem)
    if kids:
        out = f"{ind}<{elem.tag}{_attrs(elem)}>\n"
        for k in kids:
            out += _serialize(k, depth + 1)
        out += f"{ind}</{elem.tag}>\n"
        return out
    text = elem.text if elem.text is not None else ""
    if text == "":
        # leaf vacio -> self-closing (p.ej. <REMEDIATION/>), salvo CDATA explicito
        if elem.tag in _CDATA_TAGS:
            return f"{ind}<{elem.tag}{_attrs(elem)}><![CDATA[]]></{elem.tag}>\n"
        return f"{ind}<{elem.tag}{_attrs(elem)}/>\n"
    body = f"<![CDATA[{text}]]>" if elem.tag in _CDATA_TAGS else _escape(text)
    return f"{ind}<{elem.tag}{_attrs(elem)}>{body}</{elem.tag}>\n"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cid_sort_key(cid: str):
    """Orden de CIDs robusto: numericos por valor, no-numericos (UDCs) detras, alfabetico.
    Evita el crash de `int(cid)` si alguna vez entra un CID no numerico."""
    s = str(cid)
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


# CRITICALITY/VALUE (Qualys) -> label. Se usa para los niveles (Art. 14 septies).
_CRIT_LABELS = {0: "UNDEFINED", 1: "MINIMAL", 2: "MEDIUM",
                3: "SERIOUS", 4: "CRITICAL", 5: "URGENT"}


def _criticality(ctl: ET.Element) -> int:
    """Lee CRITICALITY/VALUE de un CONTROL. Si falta/no parsea -> 0 (UNDEFINED)."""
    try:
        return int((ctl.findtext("CRITICALITY/VALUE") or "").strip())
    except ValueError:
        return 0


# --------------------------------------------------------------------------- #
# Lectura del tenant (READ-ONLY)
# --------------------------------------------------------------------------- #
def _export_policy(client, pid: str) -> ET.Element:
    http, text = client.fo_get("/api/4.0/fo/compliance/policy/",
                               {"action": "export", "id": str(pid)})
    if http != 200:
        raise RuntimeError(f"export policy {pid} -> HTTP {http}")
    return ET.fromstring(text)


def _resolve_categories(get_client, cids: list[str], cache_path: Path) -> dict:
    """CID -> {category, sub_category, statement}. Cachea en disco. `get_client` es un callable
    lazy: SOLO se invoca (y se pega al tenant) si hay CIDs no cacheados → si el cache cubre todo,
    no hace ninguna llamada (modo offline)."""
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    missing = [c for c in cids if c not in cache]
    if missing:
        client = get_client()
        for i in range(0, len(missing), 50):
            chunk = missing[i:i + 50]
            http, text = client.fo_get("/api/4.0/fo/compliance/control/",
                                       {"action": "list", "ids": ",".join(chunk)})
            if http != 200:
                raise RuntimeError(f"list controls -> HTTP {http}")
            root = ET.fromstring(text)
            for ctl in root.findall(".//CONTROL"):
                cid = (ctl.findtext("ID") or "").strip()
                cache[cid] = {
                    "category": (ctl.findtext("CATEGORY") or "").strip(),
                    "sub_category": (ctl.findtext("SUB_CATEGORY") or "").strip(),
                    "statement": (ctl.findtext("STATEMENT") or "").strip(),
                }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))
    return cache


# --------------------------------------------------------------------------- #
# Harvest + clasificacion
# --------------------------------------------------------------------------- #
def _harvest(client, sources: list[dict], progress=None) -> tuple[OrderedDict, dict]:
    """Devuelve (cid -> CONTROL element merged, cid -> set(source labels)).
    `progress(msg)` opcional: se invoca una vez por benchmark ANTES de exportarlo, para que el
    caller emita un latido (el export live de cada benchmark es la parte lenta del pack)."""
    controls: OrderedDict[str, ET.Element] = OrderedDict()
    provenance: dict[str, set] = defaultdict(set)
    total = len(sources)
    for i, src in enumerate(sources, 1):
        pid, label = str(src["id"]), src["label"]
        if progress:
            progress(f"cosechando benchmark {i}/{total}: {label}")
        root = _export_policy(client, pid)
        for ctl in root.findall(".//CONTROL"):
            cid = (ctl.findtext("ID") or "").strip()
            if not cid:
                continue
            provenance[cid].add(label)
            if cid not in controls:
                controls[cid] = copy.deepcopy(ctl)
            else:
                _merge_technologies(controls[cid], ctl)
    return controls, provenance


def _merge_technologies(dst_ctl: ET.Element, src_ctl: ET.Element) -> None:
    """Agrega al control dst las TECHNOLOGY de src que falten (por tech ID)."""
    dst_techs = dst_ctl.find("TECHNOLOGIES")
    if dst_techs is None:
        return
    have = {(t.findtext("ID") or "").strip() for t in dst_techs.findall("TECHNOLOGY")}
    for t in src_ctl.findall(".//TECHNOLOGY"):
        tid = (t.findtext("ID") or "").strip()
        if tid and tid not in have:
            dst_techs.append(copy.deepcopy(t))
            have.add(tid)
    dst_techs.set("total", str(len(dst_techs.findall("TECHNOLOGY"))))


# --------------------------------------------------------------------------- #
# Cache local del harvest (Opcion A: generacion offline / tenant-agnostica).
# Tras una corrida online, los CONTROL+EVALUATE cosechados quedan en disco
# (gitignored, junto al cache de categorias) -> regenerar NO necesita re-exportar
# las policies ni que esten importadas en el tenant. El cache guarda contenido CIS
# licenciado del tenant del cliente -> NO se commitea (vive en artifacts/cache/).
# --------------------------------------------------------------------------- #
def _harvest_cached(cache_path: Path, source_ids: list[str], sources: list[dict],
                    get_client, refresh: bool, offline: bool,
                    progress=None) -> tuple[OrderedDict, dict, dict]:
    """Devuelve (controls, provenance, meta). Usa el cache si cubre EXACTAMENTE el mismo set de
    fuentes (y no se pidio refresh); si no, cosecha live (salvo offline=True -> error). meta trae
    {server, pod, harvested_at, source_ids}."""
    want = sorted(str(s) for s in source_ids)
    if not refresh and cache_path.exists():
        blob = json.loads(cache_path.read_text())
        if sorted(blob.get("source_ids", [])) == want:
            controls = OrderedDict((cid, ET.fromstring(xml))
                                   for cid, xml in blob["controls"].items())
            provenance = {cid: set(labels) for cid, labels in blob["provenance"].items()}
            meta = {k: blob.get(k) for k in ("server", "pod", "harvested_at", "source_ids")}
            meta["from_cache"] = True
            return controls, provenance, meta
    if offline:
        raise RuntimeError(
            "offline=True pero el cache de harvest no cubre estas fuentes "
            f"({cache_path}). Corre una vez ONLINE (con las policies importadas) para poblarlo.")
    client = get_client()
    controls, provenance = _harvest(client, sources, progress=progress)
    meta = {"server": client.server, "pod": client.pod,
            "harvested_at": _now(), "source_ids": want}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        **meta,
        "controls": {cid: ET.tostring(el, encoding="unicode") for cid, el in controls.items()},
        "provenance": {cid: sorted(labels) for cid, labels in provenance.items()},
    }))
    return controls, provenance, {**meta, "from_cache": False}


def _classify(cid: str, cats: dict, families: list[dict],
              classification: dict | None = None) -> tuple[str | None, str]:
    """CID -> (family_id_o_None, route). `route` explica POR QUE se decidio, para trazabilidad:
      'sub'       -> matcheo explicito por SUB_CATEGORY (sub-first)
      'category'  -> matcheo explicito por CATEGORY (backbone)
      'default'   -> cayo en classification.default_family (fallback agnostico -> REVISAR)
      'excluded'  -> SUB en exclude_sub_categories -> descarte intencional (no-seguridad)
      'unmatched' -> ninguna regla ni default -> sin clasificar (fuera del pack)
      'no_meta'   -> el CID no resolvio CATEGORY/SUB en el catalogo -> sin clasificar

    Orden: (1) SUB_CATEGORY (parte 'Database Settings' en su pilar; sub-first deja que los subs DB
    de seguridad ganen al catch-all de categoria/default), (2) CATEGORY (cada familia captura su rama
    top-level global), (3) fallback `classification` (opcional). OJO: el exclude es un FALLBACK, no un
    gate absoluto — un control cuyo SUB/CATEGORY matchea una familia explicita gana ANTES del exclude."""
    meta = cats.get(cid)
    if not meta:
        return None, "no_meta"
    cat, sub = meta["category"], meta["sub_category"]
    for fam in families:
        if sub and sub in fam["match"].get("sub_categories", []):
            return fam["id"], "sub"
    for fam in families:
        if cat in fam["match"].get("categories", []):
            return fam["id"], "category"
    cls = classification or {}
    if sub in cls.get("exclude_sub_categories", []):
        return None, "excluded"
    df = cls.get("default_family")
    return (df, "default") if df else (None, "unmatched")


# --------------------------------------------------------------------------- #
# Ensamblado del <POLICY>
# --------------------------------------------------------------------------- #
def _assemble(spec: dict, controls: OrderedDict, by_family: dict,
              cats: dict, provenance: dict, title: str | None = None,
              cover_extra: str = "", ui_safe: bool = False) -> ET.Element:
    families = spec["families"]
    pol_cfg = spec["policy"]

    # union de tecnologias usadas por los controles incluidos
    techs: "OrderedDict[str,str]" = OrderedDict()
    for cid in controls:
        for t in controls[cid].findall(".//TECHNOLOGY"):
            tid = (t.findtext("ID") or "").strip()
            tname = (t.findtext("NAME") or "").strip()
            if tid and tid not in techs:
                techs[tid] = tname

    root = ET.Element("POLICY_EXPORT_OUTPUT")
    resp = ET.SubElement(root, "RESPONSE")
    ET.SubElement(resp, "DATETIME").text = _now()
    pol = ET.SubElement(resp, "POLICY")
    ET.SubElement(pol, "TITLE").text = title or pol_cfg["title"]
    ET.SubElement(pol, "EXPORTED").text = _now()

    total_ctrls = sum(len(v) for v in by_family.values())
    src_labels = "; ".join(s["label"] for s in spec["sources"])
    cover = (pol_cfg["cover_page"].strip()
             + cover_extra
             + f"\n\n--- Provenance (qley21719) ---\n"
             + f"Generado: {_now()}\n"
             + f"Controles: {total_ctrls} (CIDs de libreria Qualys)\n"
             + f"Fuentes cosechadas: {src_labels}\n"
             + f"Tecnologias cubiertas: {len(techs)}\n"
             + "ADVERTENCIA: cubre solo el pilar tecnico de seguridad de la Ley 21.719; "
             + "ver mapping.csv (trazabilidad) y gaps.md (lo no cubierto).")
    ET.SubElement(pol, "COVER_PAGE").text = cover
    ET.SubElement(pol, "STATUS").text = pol_cfg.get("status", "active")

    techs_el = ET.SubElement(pol, "TECHNOLOGIES")
    techs_el.set("total", str(len(techs)))
    for tid, tname in techs.items():
        te = ET.SubElement(techs_el, "TECHNOLOGY")
        ET.SubElement(te, "ID").text = tid
        ET.SubElement(te, "NAME").text = tname

    sections_el = ET.SubElement(pol, "SECTIONS")
    n_sections = 0
    for fam in families:
        cids = by_family.get(fam["id"], [])
        if not cids:
            continue
        n_sections += 1
        sec = ET.SubElement(sections_el, "SECTION")
        ET.SubElement(sec, "NUMBER").text = str(fam["number"])
        ET.SubElement(sec, "HEADING").text = fam["heading"]
        ctrls_el = ET.SubElement(sec, "CONTROLS")
        ctrls_el.set("total", str(len(cids)))
        ref_prefix = "Ley 21.719 — " + "; ".join(fam["law_refs"])
        for cid in sorted(cids, key=_cid_sort_key):
            # deepcopy: el mismo control puede ensamblarse en >1 nivel; mutar el
            # original lo arrastraria entre niveles (double-stamp / doble-strip).
            ctl = copy.deepcopy(controls[cid])
            if ui_safe:
                _strip_for_import(ctl)        # CONTROL=(ID,CRITICALITY?,TECHNOLOGIES) -> import UI+API
            else:
                _stamp_reference(ctl, ref_prefix)  # cita legal en REFERENCE_TEXT (solo import por API)
            ctrls_el.append(ctl)
    sections_el.set("total", str(n_sections))
    return root


def _stamp_reference(ctl: ET.Element, ref_prefix: str) -> None:
    """Reescribe REFERENCE_TEXT del control con la cita legal (+ ref original detras) y lo deja en la
    posicion que exige el export DTD: CONTROL=(ID, CRITICALITY?, REFERENCE_TEXT?, IS_CONTROL_DISABLE?,
    TECHNOLOGIES) -> REFERENCE_TEXT va ANTES de IS_CONTROL_DISABLE/TECHNOLOGIES. (Si el control no traia
    REFERENCE_TEXT, `SubElement` lo dejaba AL FINAL -> despues de TECHNOLOGIES -> DTD-invalido. Por eso
    se re-inserta explicitamente.) Nota: esta forma rica solo importa por API (`action=import`); la UI
    'Import from XML' la rechaza -> para la UI usar ui_safe=True (ver `_strip_for_import`)."""
    ref = ctl.find("REFERENCE_TEXT")
    orig = (ref.text or "").strip() if ref is not None else ""
    new = ref_prefix + (f"  ||  orig: {orig}" if orig else "")
    if ref is not None:
        ctl.remove(ref)
    ref = ET.Element("REFERENCE_TEXT")
    ref.text = new
    insert_at = len(ctl)
    for i, k in enumerate(list(ctl)):
        if k.tag in ("IS_CONTROL_DISABLE", "TECHNOLOGIES"):
            insert_at = i
            break
    ctl.insert(insert_at, ref)


# Hijos opcionales (y sin datos en el pack) que se podan del CONTROL y de cada TECHNOLOGY en ui_safe:
# el XSD del 'Import from XML' de la UI es mas estricto que el export DTD (ya rechazo REFERENCE_TEXT,
# que el DTD permite) -> cada elemento opcional sin datos es superficie de rechazo. La cita legal y la
# remediacion viven en mapping.csv; EVALUATE (logica de chequeo, verbatim del export) se conserva.
_UI_STRIP_CONTROL = ("REFERENCE_TEXT", "IS_CONTROL_DISABLE")
_UI_STRIP_TECH = ("RATIONALE", "REMEDIATION", "DATAPOINT", "USE_SCAN_VALUE", "DB_QUERY", "DESCRIPTION")


def _strip_for_import(ctl: ET.Element) -> None:
    """UI-safe: reduce el CONTROL a (ID, CRITICALITY?, TECHNOLOGIES) y cada TECHNOLOGY a (ID, NAME?,
    EVALUATE?). Quita REFERENCE_TEXT/IS_CONTROL_DISABLE del CONTROL y RATIONALE/REMEDIATION/DATAPOINT/
    USE_SCAN_VALUE/DB_QUERY/DESCRIPTION de cada TECHNOLOGY (todos opcionales por DTD; en el pack van
    vacios o sin datos -> p.ej. `<REMEDIATION/>` vacio, mismo riesgo que el REFERENCE_TEXT rechazado).
    Quitar IS_CONTROL_DISABLE deja el control habilitado por default (lo deseado)."""
    for tag in _UI_STRIP_CONTROL:
        for el in ctl.findall(tag):
            ctl.remove(el)
    techs = ctl.find("TECHNOLOGIES")
    if techs is not None:
        for tech in techs.findall("TECHNOLOGY"):
            for tag in _UI_STRIP_TECH:
                for el in tech.findall(tag):
                    tech.remove(el)


# --------------------------------------------------------------------------- #
# Validacion
# --------------------------------------------------------------------------- #
def _validate(xml_text: str, expected_cids: set) -> dict:
    issues = []
    root = ET.fromstring(xml_text)  # well-formed (levanta si no)
    out_cids = [(c.findtext("ID") or "").strip() for c in root.findall(".//CONTROL")]
    n_sec = len(root.findall(".//SECTION"))
    n_ctrl = len(out_cids)
    n_tech = len(root.findall("./RESPONSE/POLICY/TECHNOLOGIES/TECHNOLOGY"))
    if len(set(out_cids)) != len(out_cids):
        issues.append("CIDs duplicados en la salida")
    if set(out_cids) - expected_cids:
        issues.append("CIDs en salida no presentes en el set cosechado")
    # cada control debe tener al menos una TECHNOLOGY con EVALUATE
    for c in root.findall(".//CONTROL"):
        if not c.findall(".//TECHNOLOGY/EVALUATE"):
            issues.append(f"control {(c.findtext('ID') or '').strip()} sin EVALUATE")
            break
    # estructura POLICY esperada
    pol = root.find("./RESPONSE/POLICY")
    expect = ["TITLE", "EXPORTED", "COVER_PAGE", "STATUS", "TECHNOLOGIES", "SECTIONS"]
    if pol is None or [e.tag for e in pol] != expect:
        issues.append("estructura POLICY no coincide con el schema de export")
    return {"sections": n_sec, "controls": n_ctrl, "technologies": n_tech,
            "issues": issues, "ok": not issues}


# --------------------------------------------------------------------------- #
# Emision de artefactos
# --------------------------------------------------------------------------- #
def _write_mapping_csv(path: Path, controls, by_family, cats, provenance, spec):
    fam_refs = {f["id"]: "; ".join(f["law_refs"]) for f in spec["families"]}
    fam_head = {f["id"]: f["heading"] for f in spec["families"]}
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["cid", "family", "family_heading", "law_refs", "category",
                    "sub_category", "criticality_value", "criticality_label",
                    "n_technologies", "source_policies", "statement"])
        for fam_id, cids in by_family.items():
            for cid in sorted(cids, key=_cid_sort_key):
                meta = cats.get(cid, {})
                cv = _criticality(controls[cid])
                n_t = len(controls[cid].findall(".//TECHNOLOGY"))
                w.writerow([cid, fam_id, fam_head.get(fam_id, ""), fam_refs.get(fam_id, ""),
                            meta.get("category", ""), meta.get("sub_category", ""),
                            cv, _CRIT_LABELS.get(cv, str(cv)),
                            n_t, "; ".join(sorted(provenance.get(cid, []))),
                            meta.get("statement", "")])


def _write_gaps_md(path: Path, spec, unclassified, cats,
                   excluded_by_level=None, lv=None, excluded_by_rule=None, defaulted=None):
    g = spec.get("gaps", {})
    lines = ["# Gaps — Ley 21.719 vs Qualys Policy Compliance", "",
             "Lo que este pack **NO** cubre. Qualys PC audita controles tecnicos de",
             "configuracion; la mayor parte de la Ley 21.719 es organizacional/legal.", ""]
    if lv and lv.get("id"):
        lab = _CRIT_LABELS.get(lv["min_criticality"], str(lv["min_criticality"]))
        lines += [f"> **Nivel `{lv['id']}`** {lv.get('title_suffix','')} — piso de criticidad "
                  f"**>= {lv['min_criticality']} ({lab})** (Art. 14 septies: estandar diferenciado).", ""]
    if excluded_by_level:
        lines += ["## Controles cosechados excluidos por el nivel (criticidad bajo el piso)", "",
                  f"{len(excluded_by_level)} controles quedaron fuera de **este** nivel por tener "
                  "criticidad menor al piso; estan incluidos en el nivel `sensible` (sin piso). "
                  "No es una falta de cobertura de la herramienta, es la diferenciacion del Art. 14 septies.", ""]
    lines += ["## Obligaciones tecnicas con cobertura parcial / nula", ""]
    for it in g.get("technical_partial", []):
        lines.append(f"- **{it['ref']}** — {it['text']}")
    lines += ["", "## Obligaciones organizacionales (fuera de alcance de PC)", ""]
    for it in g.get("organizational", []):
        lines.append(f"- {it}")
    if defaulted:
        lines += ["", "## Controles asignados por la familia por defecto (REVISAR)", "",
                  f"{len(defaulted)} control(es) NO matchearon ninguna regla `match` SUB/CATEGORY "
                  "explicita y se asignaron a la familia `classification.default_family`. ESTAN en el "
                  "pack, pero conviene REVISAR que la familia sea la correcta: un benchmark nuevo podria "
                  "traer un control de acceso/cifrado/auditoria bajo una CATEGORY no listada y caer aca. "
                  "Si la familia es incorrecta, agregar una regla `match` a la familia que corresponda:", ""]
        for cid in sorted(defaulted, key=_cid_sort_key)[:200]:
            meta = cats.get(cid, {})
            lines.append(f"- {cid} — {meta.get('category','?')} :: {meta.get('sub_category','?')}")
        if len(defaulted) > 200:
            lines.append(f"- ... (+{len(defaulted) - 200} mas)")
    if excluded_by_rule:
        lines += ["", "## Controles excluidos a proposito (no-seguridad)", "",
                  f"{len(excluded_by_rule)} control(es) cosechado(s) se DESCARTARON por regla "
                  "(`classification.exclude_sub_categories`) por NO ser controles de seguridad "
                  "(p.ej. tuning/performance). Es una exclusion intencional y auditable, no una "
                  "falta de cobertura:", ""]
        for cid in sorted(excluded_by_rule, key=_cid_sort_key)[:200]:
            meta = cats.get(cid, {})
            lines.append(f"- {cid} — {meta.get('category','?')} :: {meta.get('sub_category','?')}")
        if len(excluded_by_rule) > 200:
            lines.append(f"- ... (+{len(excluded_by_rule) - 200} mas)")
    if unclassified:
        lines += ["", "## Controles cosechados sin familia (no asignados)", "",
                  "CIDs cuya CATEGORY/SUB_CATEGORY no matchea ninguna familia NI cae en "
                  "`classification.default_family` (quedaron FUERA del pack; revisar el mapping "
                  "si corresponde):", ""]
        for cid in sorted(unclassified, key=_cid_sort_key)[:200]:
            meta = cats.get(cid, {})
            lines.append(f"- {cid} — {meta.get('category','?')} :: {meta.get('sub_category','?')}")
        if len(unclassified) > 200:
            lines.append(f"- ... (+{len(unclassified) - 200} mas)")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_import_instructions(path: Path, spec, server: str, stats: dict,
                               title: str | None = None):
    title = title or spec["policy"]["title"]
    base = server.rstrip("/")
    md = f"""# Importar el pack en una suscripcion Qualys

Pack: **{title}**
Controles: {stats['controls']} · Secciones: {stats['sections']} · Tecnologias: {stats['technologies']}

El archivo `policy.xml` referencia **controles de libreria de Qualys** (mismos CID en
cualquier tenant), asi que es portable. La importacion la corre el cliente; qley21719
NO la ejecuta (read-only).

## Opcion A — UI (recomendada)
1. PA (Policy Audit) o PC > **Policies** > **New** > **Import from XML**.
2. Subir `policy.xml`. Confirmar el titulo.
3. Asignar **alcance** (asset tags / asset groups): el XML NO trae scope.
4. Revisar/ajustar **valores esperados** y excepciones al nivel de riesgo (Art. 14 septies).

## Opcion B — API (XML import, v4.0)
> POST `/api/4.0/fo/compliance/policy/?action=import`  ·  `Content-Type: text/xml`
> (verificado LIVE: las versiones 2.0 y 3.0 estan EOS; usar 4.0)

```bash
curl -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" \\
     -H "X-Requested-With: qley21719" \\
     -H "Content-Type: text/xml" \\
     --data-binary @policy.xml \\
     "{base}/api/4.0/fo/compliance/policy/?action=import&title={_url(title)}&create_user_controls=0"
```

- `create_user_controls=0`: el pack usa solo CIDs de libreria (no UDCs). Si una version
  futura agrega UDCs, cambiar a `1`.
- Respuesta: `SIMPLE_RETURN` con el `ID` de la policy creada (verificado: importa OK, 1196 controles).
- `compliance/policy` NO tiene `action=delete`. Para borrar una policy: REST
  `DELETE /pcas/v3/policy?policyId=<id>` (gateway, Bearer JWT) o por UI (PA > Policies).
- Tras importar: asignar scope (asset tags/groups) y (opcional) lanzar evaluacion.

## Importante
- Ajustar el `--base url` al POD del cliente (este pack se genero contra `{base}`).
- El pack cubre SOLO el pilar tecnico de seguridad de la Ley 21.719. Ver `gaps.md`.
"""
    path.write_text(md, encoding="utf-8")


def _url(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s)


# --------------------------------------------------------------------------- #
# Niveles (Art. 14 septies: estandares diferenciados)
# --------------------------------------------------------------------------- #
def _levels(spec: dict, only: str | None = None) -> list[dict]:
    """Lee la seccion `levels:` del spec. Sin `levels` -> un nivel implicito sin piso ni
    sufijo, emitido en out_dir directo (retrocompat). Ordena base->sensible (piso desc)."""
    cfg = spec.get("levels")
    if not cfg:
        return [{"id": None, "title_suffix": "", "min_criticality": 0, "subdir": ""}]
    items = [{"id": lid, "title_suffix": lc.get("title_suffix", ""),
              "min_criticality": int(lc.get("min_criticality", 0)), "subdir": lid}
             for lid, lc in cfg.items()]
    items.sort(key=lambda x: -x["min_criticality"])  # piso mas alto (base) primero
    if only:
        items = [x for x in items if x["id"] == only]
        if not items:
            raise ValueError(f"nivel '{only}' no existe (levels: {list(cfg)})")
    return items


def _level_cover(lv: dict, n_keep: int, n_total: int) -> str:
    if not lv["id"]:
        return ""
    lab = _CRIT_LABELS.get(lv["min_criticality"], str(lv["min_criticality"]))
    return ("\n\n--- Nivel del pack (Art. 14 septies) ---\n"
            f"NIVEL: {lv['id']} {lv['title_suffix']}\n"
            "La Ley 21.719 (Art. 14 septies) habilita ESTANDARES DE SEGURIDAD DIFERENCIADOS "
            "segun el tipo de datos (sensibles, de ninos/adolescentes, geolocalizacion, "
            "situacion socioeconomica, financieros) y el tamano/actividad del responsable.\n"
            f"Este nivel aplica un piso de criticidad >= {lv['min_criticality']} ({lab}): "
            f"incluye {n_keep} de {n_total} controles tecnicos cosechados "
            f"(base ⊆ sensible). El responsable ajusta el estandar a su nivel de riesgo.")


def _emit_level(spec, out: Path, lv: dict, controls: OrderedDict, by_family_all: dict,
                cats: dict, provenance: dict, unclassified: list, server: str,
                ui_safe: bool = False, excluded_by_rule: list | None = None,
                defaulted: list | None = None) -> dict:
    """Filtra por criticidad, ensambla, valida y emite UN nivel en su subcarpeta."""
    min_c = lv["min_criticality"]
    keep = {cid for cid in controls if _criticality(controls[cid]) >= min_c}
    by_family = {fam: [c for c in cids if c in keep] for fam, cids in by_family_all.items()}
    by_family = {f: cs for f, cs in by_family.items() if cs}
    lv_controls = OrderedDict((c, controls[c]) for c in controls if c in keep)
    excluded_by_level = [c for c in controls if c not in keep]

    title = spec["policy"]["title"] + (f" {lv['title_suffix']}" if lv["title_suffix"] else "")
    cover_extra = _level_cover(lv, len(keep), len(controls))

    root = _assemble(spec, lv_controls, by_family, cats, provenance,
                     title=title, cover_extra=cover_extra, ui_safe=ui_safe)
    # ui_safe: SIN DOCTYPE remoto — el importer de la UI valida contra su XSD interno (no el DTD), y un
    # DOCTYPE apuntando a un POD fijo (qg3) es solo superficie (fetch de red / entidad externa). El path
    # rico (API) SI lleva el DOCTYPE del export (action=import valida contra ese DTD).
    doctype = ("" if ui_safe else
               '<!DOCTYPE POLICY_EXPORT_OUTPUT SYSTEM '
               f'"{server.rstrip("/")}/api/4.0/fo/compliance/policy/policy_export_output.dtd">\n')
    xml_text = '<?xml version="1.0" encoding="UTF-8" ?>\n' + doctype + _serialize(root)
    stats = _validate(xml_text, set(lv_controls.keys()))

    dest = out / lv["subdir"] if lv["subdir"] else out
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "policy.xml").write_text(xml_text, encoding="utf-8")
    _write_mapping_csv(dest / "mapping.csv", lv_controls, by_family, cats, provenance, spec)
    _write_gaps_md(dest / "gaps.md", spec, unclassified, cats, excluded_by_level, lv,
                   excluded_by_rule=excluded_by_rule, defaulted=defaulted)
    _write_import_instructions(dest / "import-instructions.md", spec, server, stats, title)

    stats.update({
        "level": lv["id"], "min_criticality": min_c, "title": title,
        "included": sum(len(v) for v in by_family.values()),
        "excluded_by_level": len(excluded_by_level),
        "by_family": {k: len(v) for k, v in by_family.items()},
        "out_dir": str(dest),
    })
    return stats


# --------------------------------------------------------------------------- #
# Orquestador
# --------------------------------------------------------------------------- #
def generate_pack(spec_path: str, out_dir: str, client=None,
                  source_ids: list[str] | None = None, level: str | None = None,
                  offline: bool = False, refresh: bool = False,
                  ui_safe: bool = False, progress=None) -> dict:
    """Genera el pack. Cosecha+clasifica UNA vez (independiente del nivel) y emite cada nivel
    (`levels:` del spec) en su subcarpeta. `level` genera uno solo.

    Cache (Opcion A — generacion offline/agnostica): tras una corrida ONLINE, el harvest
    (CONTROL+EVALUATE) y las categorias quedan cacheados en `<out>/../cache/` (gitignored). Las
    regeneraciones leen del cache y NO necesitan el tenant ni las policies importadas:
      - `offline=True`  -> nunca toca el tenant; exige cache que cubra todo (si no, error).
      - `refresh=True`  -> ignora el cache y re-cosecha live (repuebla el cache).
      - default         -> cache-first; construye el cliente SOLO si falta algo (lazy).
    `client` = QualysClient read-only; si None se construye con from_env() *solo si hace falta*."""
    spec = yaml.safe_load(Path(spec_path).read_text())
    if source_ids:  # override de --sources (mantiene labels si coinciden)
        known = {str(s["id"]): s for s in spec["sources"]}
        spec["sources"] = [known.get(str(i), {"id": str(i), "label": f"policy {i}", "tech": "?"})
                           for i in source_ids]
    src_ids = [str(s["id"]) for s in spec["sources"]]

    # cliente lazy: solo se construye si una llamada live es realmente necesaria (cache miss).
    _holder = {"c": client}

    def get_client():
        if _holder["c"] is None:
            if offline:
                raise RuntimeError("offline=True pero se necesita el tenant (cache incompleto).")
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from qualys_client import from_env
            _holder["c"] = from_env()
        return _holder["c"]

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache_dir = out.parent / "cache"
    cat_cache = cache_dir / "cid_category.json"
    harvest_cache = cache_dir / "harvest.json"

    # 1. harvest (cache-first) — los controles se conservan intactos (el filtro por nivel es no-destructivo)
    controls, provenance, meta = _harvest_cached(
        harvest_cache, src_ids, spec["sources"], get_client, refresh, offline, progress=progress)
    all_cids = list(controls.keys())
    server = meta.get("server") or (_holder["c"].server if _holder["c"] else "")
    pod = meta.get("pod") or (_holder["c"].pod if _holder["c"] else None)

    # 2. categories (cache-first; solo pega al tenant si faltan CIDs)
    if progress:
        progress(f"resolviendo categorías de {len(all_cids)} controles…")
    cats = _resolve_categories(get_client, all_cids, cat_cache)

    # 3. classify (una vez; independiente del nivel)
    classification = dict(spec.get("classification") or {})
    fam_ids = {f["id"] for f in spec["families"]}
    default_family = classification.get("default_family")
    if default_family and default_family not in fam_ids:
        raise ValueError(
            f"classification.default_family '{default_family}' no es una familia "
            f"existente ({sorted(fam_ids)})")
    exclude_subs = set(classification.get("exclude_sub_categories", []))

    # Robustez de config (no fatal): superficies de footgun para edicion futura del spec -> al summary.
    config_warnings = []
    declared_subs = {s for f in spec["families"] for s in f["match"].get("sub_categories", [])}
    overlap = exclude_subs & declared_subs
    if overlap:  # el exclude es fallback: una familia que liste ese sub GANA -> el exclude no aplica
        config_warnings.append(
            "exclude_sub_categories tambien declarados en families.sub_categories "
            f"(gana la familia, el exclude se ignora): {sorted(overlap)}")
    harvested_subs = {(cats.get(c) or {}).get("sub_category", "") for c in all_cids}
    unused_excl = sorted(s for s in exclude_subs if s not in harvested_subs)
    if unused_excl:  # typo o entrada inerte: el descarte no recae sobre ningun control cosechado
        config_warnings.append(
            f"exclude_sub_categories sin match en el harvest (typo / inerte): {unused_excl}")

    by_family_all: dict[str, list] = defaultdict(list)
    unclassified = []      # 'unmatched'/'no_meta': sin regla NI default -> fuera del pack
    excluded_by_rule = []  # 'excluded': descartados a proposito (exclude_sub_categories, no-seguridad)
    defaulted = []         # 'default': asignados por default_family (fallback agnostico) -> REVISAR
    for cid in all_cids:
        fam, route = _classify(cid, cats, spec["families"], classification)
        if fam:
            by_family_all[fam].append(cid)
            if route == "default":
                defaulted.append(cid)
        elif route == "excluded":
            excluded_by_rule.append(cid)
        else:  # 'unmatched' / 'no_meta'
            unclassified.append(cid)
    for cid in unclassified + excluded_by_rule:  # ninguno va al POLICY (no se inventan)
        controls.pop(cid, None)

    # 4. emit por nivel (filtra por criticidad sobre el mismo harvest)
    result = {"spec": spec["law"]["id"] if "law" in spec else None,
              "out_dir": str(out), "pod": pod, "harvested": len(all_cids),
              "classified": len(controls), "unclassified": len(unclassified),
              "excluded_by_rule": len(excluded_by_rule),
              "defaulted": len(defaulted),
              "default_family": default_family,
              "config_warnings": config_warnings,
              "source": "cache" if meta.get("from_cache") else "live",
              "harvested_at": meta.get("harvested_at"),
              "ui_safe": ui_safe,
              "levels": {}, "ok": True}
    for lv in _levels(spec, only=level):
        lv_stats = _emit_level(spec, out, lv, controls, by_family_all,
                               cats, provenance, unclassified, server, ui_safe=ui_safe,
                               excluded_by_rule=excluded_by_rule, defaulted=defaulted)
        result["levels"][lv["id"] or "default"] = lv_stats
        result["ok"] = result["ok"] and lv_stats["ok"]

    # 5. invariante base ⊆ sensible (si ambos presentes)
    lv_map = result["levels"]
    if "base" in lv_map and "sensible" in lv_map:
        if lv_map["base"]["included"] > lv_map["sensible"]["included"]:
            result["ok"] = False
            lv_map["base"].setdefault("issues", []).append(
                "VIOLACION base ⊄ sensible (base tiene mas controles que sensible)")

    (out / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result
