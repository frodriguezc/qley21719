#!/usr/bin/env python3
"""cloud_posture_pack.py — pack de cobertura CLOUD POSTURE (CSPM) de la Ley 21.719 (read-only).

Motor SEPARADO del de Policy Compliance. Lee posture CSPM (controls + evaluations) vía
qualys_client.CloudViewClient (solo GET, allow-list-only) y emite un mapping report
control-cloud -> familia legal -> artículo (+ PASS/FAIL), control-set de FAILs, gaps y
apply-instructions. NO emite policy.xml y NO muta el tenant (el cliente aplica por UI).

Uso (live, auto-descubre connectors de todos los providers):
    export QUALYS_POD=US03 QUALYS_API_USER=... QUALYS_API_PASSWORD=...
    python scripts/cloud_posture_pack.py                      # --provider all (default)
    python scripts/cloud_posture_pack.py --provider aws       # solo AWS (auto-cuentas)
    python scripts/cloud_posture_pack.py --provider aws --account 111122223333

Uso (sin tenant, sobre una muestra JSON {controls:[...], evaluations:[...]}):
    python scripts/cloud_posture_pack.py --fixture sample.json --provider aws --account demo

Salida en artifacts/cloud-pack/<provider>/<account>/ (gitignored): mapping.csv, fails.csv,
gaps.md, apply-instructions.md, summary.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cloud_pack.generator import (  # noqa: E402
    _html_to_text, build_pack, load_spec, parse_controls, parse_evaluations)
from scripts._runtime import (  # noqa: E402
    resolve_run_dir, preflight_writable, link_latest, setup_run_log)

# Campos del connector con el id de cuenta, por proveedor (verificado live AWS/GCP).
_ACCOUNT_KEYS = {
    "aws": ("awsAccountId", "accountId", "baseAccountId"),
    "azure": ("subscriptionId", "accountId"),
    "gcp": ("projectId",),
    "oci": ("tenancyId", "tenantId", "accountId"),
}


def _http_ok(code: int, text: str, what: str) -> None:
    if code != 200:
        raise SystemExit(f"[error] {what}: HTTP {code}\n{text[:500]}")


# Diagnóstico de POSTURE por cuenta (NO colapsar "no encontró nada" en un solo estado):
#   ok            -> 200 + evaluations con al menos un control evaluado (PASS/FAIL/…).
#   not_evaluated -> 200 + evaluations pero TODAS NOT_EVALUATED -> CSPM activo, el connector aún
#                    no corrió la evaluación -> "Run Connector". (caso C)
#   empty         -> 200 pero sin evaluations (content: []) -> CSPM no activo en el connector
#                    (solo inventario) o sin evaluations para esa cuenta. (caso B)
#   auth          -> HTTP 401/403 en la API CSPM -> el API user (gateway+JWT) no tiene scope del
#                    módulo CloudView / CSPM no habilitado. NO es "vacío". (caso A)
#   error         -> otro fallo HTTP (5xx/red).
def _fetch_all_evaluations(client, provider, account, page_size=500, max_pages=200):
    """Lee TODAS las páginas de evaluations/{account} (paginación Spring: content/last/number).
    Solo GETs read-only. Devuelve (items, http_status): el status de la 1ª página, para que el
    caller distinga 401/403 (auth, caso A) de 200-vacío (caso B). 401/403 NO se traga: corta y
    devuelve ([], code). Otro non-200 (5xx/red) sí aborta vía _http_ok."""
    items, page, first_status = [], 0, 0
    while page < max_pages:
        code, text = client.list_evaluations(provider, account,
                                              params={"pageSize": page_size, "pageNo": page})
        if page == 0:
            first_status = code
        if code in (401, 403):                       # caso A: auth/permiso CSPM -> NO silenciar
            return [], code
        _http_ok(code, text, f"{provider}/evaluations/{account} (page {page})")
        obj = json.loads(text)
        content = obj.get("content") if isinstance(obj, dict) else obj
        if not content:
            break
        items.extend(content)
        if not isinstance(obj, dict) or obj.get("last") is True or len(content) < page_size:
            break
        page += 1
    return items, first_status


def _discover_accounts(client, provider):
    """Lista los connectors del provider (GET read-only) y extrae los account/subscription/project ids.
    OCI va por la Connector Management API (`/connectors/v1.0/OCI/list`): cloudview-api/oci/connectors
    NO existe (404). El resto (aws/azure/gcp) por cloudview-api `/<prov>/connectors`.

    Devuelve (accounts, status): `status` ∈ ok|empty|auth|error para que el caller NO confunda
    "401/403 al listar connectors" (caso A: auth/permiso CSPM) con "200 sin connectors" (vacío
    legítimo). Antes ambos caían en `[]` -> indistinguibles (síntoma sinacofi: _probe_tc daba 401)."""
    try:
        if provider == "oci":
            code, text = client.list_cloud_connectors("OCI", {"pageSize": 100})
        else:
            code, text = client.list_connectors(provider)
    except Exception as e:  # noqa: BLE001
        return [], ("error", f"error de cliente: {type(e).__name__}: {str(e)[:120]}")
    if code in (401, 403):
        snip = (text or "")[:120].strip().replace("\n", " ")
        return [], ("auth", f"HTTP {code} (auth/permiso CSPM, NO ausencia de cuentas): {snip}")
    if code != 200:
        return [], ("error", f"HTTP {code}: {(text or '')[:140].strip()}")
    obj = json.loads(text)
    items = obj.get("content") if isinstance(obj, dict) else (obj if isinstance(obj, list) else [])
    seen, out = set(), []
    for c in items or []:
        if not isinstance(c, dict):
            continue
        for k in _ACCOUNT_KEYS.get(provider, ()):
            v = c.get(k)
            if v and str(v) not in seen:
                seen.add(str(v))
                out.append(str(v))
                break
    return out, ("ok" if out else "empty", f"{len(out)} cuenta(s)")


def _diagnose_posture(http_status: int, controls: list, posture: dict) -> tuple[str, str]:
    """Clasifica el resultado de leer posture en uno de los 3 casos distinguibles (+auth), para
    que el próximo diagnóstico sea inmediato y NO se colapsen estados muy distintos en "vacío":
      auth          (A) -> 401/403 en la API CSPM: el API user no tiene scope CloudView / CSPM no
                           habilitado en la suscripción. NO es "sin datos".
      empty         (B) -> 200 pero evaluations vacío: CSPM no activo en el connector (solo
                           inventario) o sin evaluations para esa cuenta.
      not_evaluated (C) -> 200 con evaluations pero TODAS NOT_EVALUATED: CSPM activo, el connector
                           aún no corrió la evaluación -> hay que "Run Connector".
      ok                -> 200 con al menos un control evaluado (PASS/FAIL/…).
    Devuelve (estado, detalle-humano)."""
    if http_status in (401, 403):
        return "auth", (f"HTTP {http_status}: la API CSPM rechazó auth/permiso — el API user "
                        "(gateway+JWT) no tiene scope del módulo CloudView o CSPM no está "
                        "habilitado en la suscripción. NO es ausencia de datos.")
    if not controls:
        return "empty", ("HTTP 200 pero sin evaluations (content: []) — CSPM no activo en el "
                         "connector (solo inventario) o sin evaluations para esta cuenta.")
    evaluated = sum(1 for v in posture.values() if v not in ("NOT_EVALUATED", "UNKNOWN", ""))
    if evaluated == 0:
        return "not_evaluated", (f"HTTP 200 con {len(controls)} control(es) pero TODOS "
                                 "NOT_EVALUATED — CSPM activo, el connector aún no corrió la "
                                 'evaluación. Acción: "Run Connector" (ver detalle abajo).')
    return "ok", f"HTTP 200 · {evaluated}/{len(controls)} control(es) evaluado(s)"


def _run_connector_hint(provider: str, account: str) -> str:
    """Acción provider-aware para 'Run Connector' (audit jun-2026). AWS/Azure/GCP tienen API de run
    (`GET /qps/rest/3.0/run/am/<prov>assetdataconnector/<connectorId>`, Basic); **OCI no la tiene**
    (límite Qualys) → solo consola. Es una MUTACIÓN → la herramienta NO la dispara; el operador la
    corre. `account` es la cuenta/tenant (no el connectorId del run) → se deja como placeholder."""
    p = (provider or "").lower().strip()
    if p in ("aws", "azure", "gcp"):
        return (f"    → Run Connector POR API: GET <platform>/qps/rest/3.0/run/am/{p}"
                f"assetdataconnector/<connectorId> (Basic auth, qualysapi.<pod>). O consola: "
                f"TotalCloud → Connectors → hover → Actions → Run Connector. Esperar FINISHED_SUCCESS "
                f"y re-correr esta herramienta.")
    if p == "oci":
        return ("    → OCI NO tiene API de Run Connector (límite Qualys) → consola: TotalCloud → "
                "Connectors → hover sobre el connector OCI → Actions → Run Connector (sincroniza "
                "inventario y dispara la evaluación de postura). Esperar FINISHED_SUCCESS y re-correr.")
    return "    → Run Connector: consola → TotalCloud → Connectors → hover → Actions → Run Connector."


def _fetch_control_metadata(client, page_size=500, max_pages=20) -> dict:
    """Best-effort: {cid: remediation(texto)} desde controls/metadata/list (campo manualRemediation,
    HTML->texto). Es la librería GLOBAL de controles CSPM (~1.4k, todos los providers) -> se baja una
    vez por corrida y se joinea por cid == controlId de las evaluations. Read-only (GET). 401/403/
    error/schema viejo -> {} (la columna `remediation` queda vacía; la metadata de control es un
    permiso aparte que no todo tenant tiene)."""
    out: dict = {}
    try:
        for page in range(max_pages):
            code, text = client.list_controls(params={"pageSize": page_size, "pageNo": page})
            if code != 200:
                break
            ctrls = (json.loads(text) or {}).get("control") or []
            if not ctrls:
                break
            for it in ctrls:
                cid = str(it.get("cid", "")).strip()
                if cid:
                    out[cid] = _html_to_text(it.get("manualRemediation") or "")
            if len(ctrls) < page_size:
                break
    except Exception:
        return out
    return out


def _run_one(client, spec, provider, account, out_base, log=None, remediation=None):
    """Corre el pipeline para una cuenta: fetch evaluations -> classify -> emit. Read-only.
    Loguea de forma DISTINGUIBLE cuál de los 3 casos de posture ocurrió (auth/empty/not_evaluated/
    ok), por provider/cuenta, tanto a stdout como al run.log."""
    items, http_status = _fetch_all_evaluations(client, provider, account)
    controls = parse_controls(items)
    posture = parse_evaluations(items)
    state, why = _diagnose_posture(http_status, controls, posture)
    DIAG = {"auth": "🔐 AUTH/PERMISO", "empty": "∅ SIN EVALUATIONS",
            "not_evaluated": "⏳ NOT_EVALUATED (Run Connector)", "ok": "✓ OK"}
    diag_line = f"  [{provider}/{account}] posture {DIAG[state]} — {why}"
    print(diag_line, flush=True)
    if log is not None:
        log.info(f"{provider}/{account} posture_state={state} http={http_status} "
                 f"controls={len(controls)} :: {why}")
    if state == "not_evaluated":                      # acción provider-aware (API vs consola; audit jun-2026)
        hint = _run_connector_hint(provider, account)
        print(hint, flush=True)
        if log is not None:
            log.info(f"{provider}/{account} run_connector_hint :: {hint.strip()}")
    out_dir = str(Path(out_base) / provider / (account or "default"))
    stats = build_pack(controls, posture, spec, out_dir, provider=provider, account=account,
                       remediation=remediation)
    print(f"  [{provider}/{account}] {stats['controls']} ctrl · {stats['evaluated']} eval · "
          f"{stats['fails']} FAIL · {stats['gaps']} a revisar · {stats['by_family']}", flush=True)
    return {"provider": provider, "account": account, "out_dir": out_dir,
            "posture_state": state, **stats}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pack de cobertura cloud-posture (CSPM), read-only.")
    ap.add_argument("--provider", default="all", choices=["all", "aws", "azure", "gcp", "oci"],
                    help="Proveedor cloud, o 'all' para auto-descubrir todos (default).")
    ap.add_argument("--account", default="",
                    help="Account/subscription/project/tenant id. Vacío = auto-descubrir vía connectors.")
    ap.add_argument("--spec", default=None, help="Spec YAML cloud (default: mapping/ley21719-cloud.yaml).")
    ap.add_argument("--out", default="artifacts/cloud-pack", help="Directorio de salida.")
    ap.add_argument("--fixture", default=None, help="JSON local {controls,evaluations} para correr SIN tenant.")
    ap.add_argument("--pod", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None, help="Override del gateway CSPM si difiere del host FO.")
    args = ap.parse_args(argv)

    spec = load_spec(args.spec)

    base = Path(args.out)
    preflight_writable(base)                       # falla rápido ANTES de tocar el tenant
    run_dir, run_id = resolve_run_dir(base)        # artifacts/cloud-pack/<run_id>/<provider>/<account>
    run_dir.mkdir(parents=True, exist_ok=True)
    out_base = str(run_dir)
    log = setup_run_log(run_dir)
    log.info(f"start run_id={run_id} mode={'fixture' if args.fixture else 'live'} "
             f"provider={args.provider} account={args.account or 'auto'}")

    if args.fixture:
        blob = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        evals = blob.get("evaluations", [])
        # como en live: si no hay 'controls' explícitos, se derivan de las evaluations.
        controls = parse_controls(blob.get("controls") or evals)
        posture = parse_evaluations(evals)
        prov = args.provider if args.provider != "all" else "aws"
        acct = args.account or "fixture"
        out_dir = str(Path(out_base) / prov / acct)
        stats = build_pack(controls, posture, spec, out_dir, provider=prov, account=acct)
        link_latest(run_dir)
        log.info(f"fixture done controls={stats['controls']} fails={stats['fails']} out={out_dir}")
        print(f"[fixture] {stats['controls']} ctrl · {stats['fails']} FAIL -> {out_dir}/")
        return 0

    if args.account and args.provider == "all":
        raise SystemExit("[error] --account requiere un --provider específico (no 'all').")

    from qualys_client import CloudViewClient
    from qualys_client.cloudview import from_env as cv_from_env
    if args.pod and args.user and args.password:
        client = CloudViewClient(args.pod, args.user, args.password, server=args.server)
    else:
        client = cv_from_env(server=args.server)
    print(f"[cloud] host={client.server}", flush=True)

    # Metadata global de controles (remediación) — una vez por corrida; best-effort (puede 401).
    remediation = _fetch_control_metadata(client)
    print(f"[cloud] metadata: {len(remediation)} control(es) con remediación"
          if remediation else "[cloud] metadata de control no accesible — columna 'remediation' vacía",
          flush=True)
    log.info(f"control_metadata remediation_controls={len(remediation)}")

    providers = ["aws", "azure", "gcp", "oci"] if args.provider == "all" else [args.provider]
    ran = 0
    for prov in providers:
        if args.account:
            accounts, disc = [args.account], ("ok", "cuenta provista por --account")
        else:
            accounts, disc = _discover_accounts(client, prov)
        disc_state, disc_why = disc
        if not accounts:
            # NO colapsar "401/403 al descubrir connectors" (auth, caso A) con "200 sin connectors"
            # (vacío legítimo): logueá el motivo distinguible.
            if disc_state == "auth":
                print(f"  [{prov}] 🔐 AUTH/PERMISO al listar connectors — {disc_why}", flush=True)
                log.info(f"{prov} discover_state=auth :: {disc_why}")
            elif disc_state == "error":
                print(f"  [{prov}] ⚠️ ERROR al listar connectors — {disc_why}", flush=True)
                log.info(f"{prov} discover_state=error :: {disc_why}")
            else:
                print(f"  [{prov}] ∅ sin connectors/cuentas — skip", flush=True)
                log.info(f"{prov} discover_state=empty (sin connectors/cuentas)")
            continue
        for acct in accounts:
            try:
                r = _run_one(client, spec, prov, acct, out_base, log=log, remediation=remediation)
                log.info(f"{prov}/{acct} posture={r['posture_state']} controls={r['controls']} "
                         f"evaluated={r['evaluated']} fails={r['fails']} gaps={r['gaps']}")
                ran += 1
            except SystemExit as e:
                print(f"  [{prov}/{acct}] ⚠️ ERROR: {e}", flush=True)
                log.info(f"{prov}/{acct} ERROR {e}")
    link_latest(run_dir)
    log.info(f"done accounts={ran} calls={client.call_count} out={out_base}")
    print(f"[cloud] {ran} cuenta(s) procesada(s) · {client.call_count} GETs -> {out_base}/"
          f" (también: {run_dir.parent / 'latest'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
