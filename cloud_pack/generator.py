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


# Parámetros de reporte CSPM por provider: (cloudType del body, etiqueta del identificador de
# scope). VERIFICADO (jun-2026) contra la guía TotalCloud/CloudView API vigente
# (docs.qualys.com/en/tc/api/reports/create_report.htm): `cloudType` admite AWS, Azure, GCP y OCI.
# La colección Postman pineada v1.23.0.0 (2022) enumeraba solo AWS/Azure/GCP; OCI se agregó después
# (connectors 3.0 + evaluations v1 `/oci/evaluations/?tenantId=`). El smoke US03 no ejercitó OCI en
# reportes -> se deja un callout de "confirma contra tu tenant / usa consola" para OCI.
_CLOUD_REPORT_PARAMS = {
    "aws":   ("AWS",   "account id"),
    "azure": ("AZURE", "subscription id"),
    "gcp":   ("GCP",   "project id"),
    "oci":   ("OCI",   "tenant id (OCID)"),
}

# Subsección '3b' de apply-instructions. Plantilla con sentinelas __X__ (NO f-string: el cuerpo
# lleva llaves JSON y `$VARS` de shell). Sin credenciales: el password va por $QUALYS_API_PASSWORD
# (entorno), nunca en claro. Los `\\` son continuaciones de línea de shell en el .md emitido.
_CLOUD_REPORT_TEMPLATE = r"""---

## 3b — Generar el reporte vía API (opcional; lo corre el cliente)

> **Crear/correr un reporte es una MUTACIÓN (POST) → human-gate.** Por eso **la herramienta NO lo
> dispara** (invariante read-only: el `CloudViewClient` solo emite GETs allow-list). Estos `curl`
> los corre **el cliente**, igual que el `subir.sh` del motor PC.
>
> **Prerrequisito:** el API user necesita *Reporting Permission* en CloudView (un Reader puro puede
> no poder POSTear el `create`).

__OCI_WARN__Namespace `cloudview-api/rest/v1` · Host **`qualysguard.<seg>.apps.qualys.com`** (NO el
FO `qualysapi.*` → 404; es el mismo host que esta herramienta ya resolvió — ver `run.log`) · Auth
HTTP Basic + header `X-Requested-With`.

**Parámetros de este scope:**

| Variable | Valor |
|---|---|
| `PROVIDER` (path) | `__PROVIDER__` |
| `CLOUD_TYPE` (body) | `__CLOUD_TYPE__` |
| `SCOPE_ID` (__SCOPE_LABEL__) | `__SCOPE_ID__` |
| Reportes | __REPORTS__ |

Referencia multi-provider — AWS→`AWS`/account · Azure→`AZURE`/subscription · GCP→`GCP`/project ·
OCI→`OCI`/tenant (⚠️ verificar).

**Variables** (definir en el shell; el password va por entorno, **nunca** en claro):

```bash
PLATFORM_URL="https://qualysguard.<seg>.apps.qualys.com"   # mismo host que usa la herramienta
export QUALYS_API_USER QUALYS_API_PASSWORD                 # ya en el entorno; NO hardcodear
PROVIDER=__PROVIDER__ CLOUD_TYPE=__CLOUD_TYPE__ SCOPE_ID=__SCOPE_ID__
```

### A) Assessment Report (snapshot multi-policy; CSV/PDF; asíncrono)

```bash
# 0) (read-only — ya lo hace la herramienta) resolver el connectorId del scope
curl -s -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" -H "X-Requested-With: qley21719" \
  -H "Accept: application/json" \
  "$PLATFORM_URL/cloudview-api/rest/v1/$PROVIDER/connectors"   # match $SCOPE_ID -> <CONNECTOR_ID>

# 1) CREAR + correr el Assessment Report (POST = mutación -> lo corre el cliente) -> <REPORT_ID>
curl -s -X POST -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" \
  -H "X-Requested-With: qley21719" -H "Content-Type: application/json" \
  "$PLATFORM_URL/cloudview-api/rest/v1/report/assessment/create" \
  -d '{
    "reportName": "Ley 21.719 - <cliente>",
    "format": "CSV",
    "cloudType": "'"$CLOUD_TYPE"'",
    "executionType": "RUN_TIME",
    "policyIds": ["<POLICY_ID>"],
    "connectorIds": ["<CONNECTOR_ID>"],
    "resourceResults": ["FAIL"],
    "resourceSummaryInclude": true
  }'

# 2) POLL estado hasta "status":"Completed" (Accepted->Processing->Generated->Completed|Failed)
curl -s -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" -H "X-Requested-With: qley21719" \
  -H "Accept: application/json" \
  "$PLATFORM_URL/cloudview-api/rest/v1/report/assessment/list?reportId=<REPORT_ID>"

# 3) DESCARGAR (CSV o PDF)
curl -s -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" -H "X-Requested-With: qley21719" \
  "$PLATFORM_URL/cloudview-api/rest/v1/report/assessment/<REPORT_ID>/download?reportFormat=csv" \
  -o "ley21719-assessment-<cliente>-$CLOUD_TYPE.csv"

# (opcional) re-correr el mismo reporte sin recrearlo:
#   curl -s -X POST ... "$PLATFORM_URL/cloudview-api/rest/v1/report/assessment/<REPORT_ID>/rerun"
```

Body de `create`: `reportName, format(CSV|PDF), cloudType, executionType(RUN_TIME|BUILD_TIME),
policyIds[], connectorIds[], tagIds[], resourceResults[](PASS · PASSE=pass-with-exception · FAIL),
resourceSummaryInclude, query(QQL opc), startDate, endDate`. **Asíncrono**: `create`→`reportId`→
`list?reportId=` (estado **Processing** → **Completed**, recién ahí descargable) → `download`. **El
reporte se autoborra a los 7 días de creado.**

### B) Mandate Report (posture vs. mandate regulatorio) — AWS/Azure/GCP/OCI

La Ley 21.719 **no es un mandate nativo CSPM** → se puentea con un mandate **afín** (ISO 27001 /
NIST 800-53 / GDPR) como evidencia.

```bash
# a) descubrir el mandateId afín y las policies soportadas por cloudType
curl -s -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" -H "X-Requested-With: qley21719" \
  -H "Accept: application/json" "$PLATFORM_URL/cloudview-api/rest/v1/reports/mandates"
curl -s -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" -H "X-Requested-With: qley21719" \
  -H "Accept: application/json" \
  "$PLATFORM_URL/cloudview-api/rest/v1/reports/policies?cloudType=$CLOUD_TYPE"

# b) crear el mandate report (POST)
curl -s -X POST -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" \
  -H "X-Requested-With: qley21719" -H "Content-Type: application/json" \
  "$PLATFORM_URL/cloudview-api/rest/v1/reports" \
  -d '{ "cloudType":"'"$CLOUD_TYPE"'", "type":"<MANDATE_BASED>", "mandateId":"<MANDATE_ID>",
        "policies":[{"cloudType":"'"$CLOUD_TYPE"'","policyId":"<POLICY_ID>"}],
        "connectorIds":["<CONNECTOR_ID>"], "format":"PDF",
        "title":"Ley 21.719 (mandate afín) - <cliente>" }'
```

> ✅ **Verbo confirmado** (guía TotalCloud/CloudView API vigente, jun-2026 —
> `docs.qualys.com/en/tc/api/reports/create_report.htm` / `.../reports/update_report.htm`):
> **crear** el reporte es **`POST /cloudview-api/rest/v1/reports`** y **actualizar**
> **`PATCH …/reports/{reportId}`**. La colección Postman v1.23.0.0 los marcaba como `GET` con body
> (error de autoría). Los de `/report/assessment/*` son **POST** (create/rerun) y **GET**
> (list/download).
>
> **OCI:** la guía vigente admite `cloudType` **OCI** en reportes (se agregó tras v1.23.0.0); el
> smoke no lo ejercitó → confirma contra tu tenant OCI; si no responde, usa la **consola** (paso 3).

**Docs (Qualys):** [Reports](https://docs.qualys.com/en/cloudview/latest/reports/reports.htm) ·
[Assessment Report](https://docs.qualys.com/en/cloudview/latest/reports/assessment_report.htm) ·
[Mandate Report](https://docs.qualys.com/en/cloudview/latest/reports/mandate_report.htm) ·
[API · Create a Report](https://docs.qualys.com/en/tc/api/reports/create_report.htm) ·
[API · Create Assessment Report](https://docs.qualys.com/en/tc/api/assessment_reports/create_assessment_report.htm)

"""


def _cloud_report_section(provider: str, account: str) -> str:
    """Renderiza la subsección '3b' provider-aware: sustituye PROVIDER/CLOUD_TYPE/SCOPE_ID según el
    scope leído. Es un HUMAN-GATE: documenta los `curl` (POST=mutación) que corre el CLIENTE; la
    herramienta nunca los dispara. Sin credenciales reales (placeholders por entorno)."""
    p = (provider or "").lower().strip()
    if p in _CLOUD_REPORT_PARAMS:
        cloud_type, scope_label = _CLOUD_REPORT_PARAMS[p]
        provider_path = p
        scope_id = account or f"<{scope_label}>"
        reports = ("Assessment ✅ · Mandate ✅ (OCI soportado en la guía API vigente; "
                   "no ejercitado en el smoke — confirma contra tu tenant)"
                   if p == "oci" else "Assessment ✅ · Mandate ✅")
    else:
        provider_path = "<aws|azure|gcp|oci>"
        cloud_type = "<AWS|AZURE|GCP|OCI>"
        scope_label = "id del scope"
        scope_id = account or "<SCOPE_ID>"
        reports = "según provider (AWS/Azure/GCP ✅; OCI ⚠️ verificar)"

    oci_warn = ""
    if p == "oci":
        oci_warn = (
            "> ⚠️ **OCI:** la guía TotalCloud/CloudView API **vigente** admite `cloudType` **OCI** en "
            "reportes y expone connectors/evaluations OCI (`/oci/evaluations/?tenantId=`, que esta "
            "herramienta ya lee); OCI **no estaba** en la colección Postman v1.23.0.0 pineada y **el "
            "smoke no lo ejercitó en reportes** → confirma contra tu tenant OCI; si no responde, usa "
            "el flujo de **consola** (paso 3).\n\n")

    return (_CLOUD_REPORT_TEMPLATE
            .replace("__OCI_WARN__", oci_warn)
            .replace("__PROVIDER__", provider_path)
            .replace("__CLOUD_TYPE__", cloud_type)
            .replace("__SCOPE_LABEL__", scope_label)
            .replace("__SCOPE_ID__", scope_id)
            .replace("__REPORTS__", reports))


def _write_apply_instructions(path: str, spec: dict, provider: str, account: str) -> None:
    head = f"""# Cómo aplicar el pack cloud-posture (CSPM) — human-gate

> En cloud NO hay `policy.xml` importable. Este pack es READ-ONLY: la herramienta solo LEE
> posture (controls + evaluations) y emite trazabilidad. **No muta nada.** El cliente aplica.

Scope leído: provider=`{provider or '(varios)'}`  account/connector=`{account or '(según --account)'}`

1. **Revisar** `mapping.csv` (control CSPM → familia legal → artículos → PASS/FAIL) y `fails.csv`
   (los FAIL, input a remediación) y `gaps.md` (controles a revisar).
2. **Crear la Custom Policy en la UI** (no por API por defecto): `Policy > New`, elegir provider/
   executionType, asociar los controles del mapping, y asignar connectors/tags (el scope lo pone
   el cliente). Ref. DESIGN-cloud-posture.md §4.
3. (Opcional) Generar/descargar un Assessment/Mandate Report **desde la consola** — o por **API**
   (ver **§3b** abajo). Su **creación es POST (mutación)**, por eso **la herramienta no lo dispara**;
   lo hace el cliente.
4. Re-correr esta herramienta (read-only) para regenerar el mapping tras cualquier cambio.

"""
    tail = (
        "NOTA de alcance: el CSPM valida la CONFIGURACIÓN (cifrado/backup/red); no cifra, no respalda, no\n"
        "restaura ni clasifica el dato. Las familias `cifrado` y `disponibilidad` son config-only (gap honesto).\n"
        "La Ley 21.719 NO es un mandate nativo CSPM: se puentea vía mandate afín (ISO 27001 / NIST 800-53 / GDPR).\n"
    )
    md = head + _cloud_report_section(provider, account) + tail
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
