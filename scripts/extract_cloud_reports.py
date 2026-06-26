#!/usr/bin/env python3
"""extract_cloud_reports.py — extracción AUTOMATIZADA de reportes CSPM (Assessment + Mandate).

Es la contraparte HUMAN-GATE del paso 3b de apply-instructions: automatiza, secuencialmente y
respetando los rate limits, el flujo `create -> poll -> download` de los reportes de TotalCloud/
CloudView, multi-provider (AWS/Azure/GCP/OCI).

⚠️ MUTACIÓN: crear/correr un reporte es un POST que toca el tenant. Por eso este script vive FUERA
del motor read-only y NO hace ningún POST salvo `--run` explícito. Sin `--run` es DRY-RUN: solo GETs
de descubrimiento (connectors/policies/mandates) + imprime el PLAN (qué crearía). Auth = API Gateway
+ JWT (igual que el motor; NO Basic). Rate limits: el cliente respeta `X-RateLimit-ToWait-Sec`/
`Retry-After` (429/409) con backoff; entre creates se espacia y el poll es cortés (intervalo + tope).

Uso:
    # DRY-RUN (default, read-only): muestra el plan sin crear nada
    python scripts/extract_cloud_reports.py --provider oci
    # EJECUTAR (mutación, human-gate): crea, pollea y descarga
    TENANT=sinacofi ... python scripts/extract_cloud_reports.py --provider oci --run
    python scripts/extract_cloud_reports.py --provider all --reports assessment --run

Salida: artifacts/cloud-reports/<run_id>/<provider>/<account>/  (gitignored): los CSV/PDF + run.log.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.cloud_posture_pack import _ACCOUNT_KEYS  # noqa: E402  (reusa el mapeo de account keys)
from scripts._runtime import (link_latest, preflight_writable, resolve_run_dir,  # noqa: E402
                              setup_run_log, slugify)

_CONNECTOR_ID_KEYS = ("connectorId", "id", "connectorUuid", "uuid", "connectorRef")
_REPORT_ID_KEYS = ("reportId", "id", "reportUuid")
_POLICY_ID_KEYS = ("policyId", "id")
_MANDATE_ID_KEYS = ("mandateId", "id")
_PROVIDERS = ("aws", "azure", "gcp", "oci")
_CLOUD_TYPE = {"aws": "AWS", "azure": "AZURE", "gcp": "GCP", "oci": "OCI"}
# mandate afín preferido (Ley 21.719 no es mandate nativo); orden de preferencia + substrings.
_MANDATE_HINTS = {
    "iso27001": ("iso 27001", "iso/iec 27001", "iso27001", "27001"),
    "nist": ("nist 800-53", "nist 800 53", "800-53", "sp 800-53"),
    "gdpr": ("gdpr", "general data protection"),
}
_MANDATE_PREF = ("iso27001", "nist", "gdpr")
# estados terminales del Assessment Report (async): create -> Accepted/Processing/Generated -> ...
_DONE_OK = {"completed", "generated", "finished", "success"}
_DONE_BAD = {"failed", "error", "cancelled", "canceled"}


# --------------------------------------------------------------------------------------
# Helpers PUROS (testeables sin red)
# --------------------------------------------------------------------------------------

def _first(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return None


def _items(text: str) -> list:
    """Extrae la lista de items de una respuesta JSON Spring (content[]) o lista plana."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(obj, dict):
        c = obj.get("content")
        return c if isinstance(c, list) else []
    return obj if isinstance(obj, list) else []


def _extract_connectors(provider: str, text: str) -> list[dict]:
    """[{account, connector_id, name}] desde la respuesta de connectors (defensivo con los nombres
    de campo: account por _ACCOUNT_KEYS del provider, connectorId por _CONNECTOR_ID_KEYS)."""
    out = []
    for it in _items(text):
        if not isinstance(it, dict):
            continue
        acct = _first(it, _ACCOUNT_KEYS.get(provider, ()))
        cid = _first(it, _CONNECTOR_ID_KEYS)
        if acct and cid:
            out.append({"account": acct, "connector_id": cid,
                        "name": it.get("name") or it.get("connectorName") or ""})
    return out


def _extract_policies(text: str) -> list[dict]:
    """[{policy_id, name}] desde /reports/policies."""
    out = []
    for it in _items(text):
        if not isinstance(it, dict):
            continue
        pid = _first(it, _POLICY_ID_KEYS)
        if pid:
            out.append({"policy_id": pid, "name": it.get("name") or it.get("policyName") or ""})
    return out


def _pick_mandate(text: str, hint: str | None) -> dict | None:
    """Elige el mandate afín por substring del nombre. Con hint usa ese; sin hint, el 1º que matchee
    el orden de preferencia (ISO 27001 -> NIST 800-53 -> GDPR). None si no hay match."""
    mandates = []
    for it in _items(text):
        if not isinstance(it, dict):
            continue
        mid = _first(it, _MANDATE_ID_KEYS)
        name = (it.get("name") or it.get("mandateName") or "")
        if mid:
            mandates.append({"mandate_id": mid, "name": name})
    order = (hint,) if hint else _MANDATE_PREF
    for key in order:
        for subs in (_MANDATE_HINTS.get(key, (key,)),):
            for m in mandates:
                low = m["name"].lower()
                if any(s in low for s in subs):
                    return m
    return None


def _assessment_body(name: str, cloud_type: str, policy_ids: list[str],
                     connector_ids: list[str], results: list[str], fmt: str) -> dict:
    return {
        "reportName": name,
        "format": fmt.upper(),
        "cloudType": cloud_type,
        "executionType": "RUN_TIME",
        "policyIds": policy_ids,
        "connectorIds": connector_ids,
        "resourceResults": [r.upper() for r in results],
        "resourceSummaryInclude": True,
    }


def _mandate_body(name: str, cloud_type: str, mandate_id: str, policy_ids: list[str],
                  connector_ids: list[str], fmt: str = "PDF") -> dict:
    return {
        "cloudType": cloud_type,
        "type": "MANDATE_BASED",
        "mandateId": mandate_id,
        "policies": [{"cloudType": cloud_type, "policyId": pid} for pid in policy_ids],
        "connectorIds": connector_ids,
        "format": fmt.upper(),
        "title": name,
    }


def _status_of(text: str) -> str:
    """status (lowercase) de una respuesta de assessment/list."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return ""
    if isinstance(obj, dict):
        for it in (obj.get("content") or [obj]):
            if isinstance(it, dict) and it.get("status"):
                return str(it["status"]).strip().lower()
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return str(obj[0].get("status", "")).strip().lower()
    return ""


def _discover(client, provider: str) -> tuple[list[dict], str]:
    """Descubre connectors (read-only). Devuelve (connectors, state) con state ok|empty|auth|error.
    OCI por Connector Management API; el resto por cloudview-api/<provider>/connectors."""
    try:
        if provider == "oci":
            code, text = client.list_cloud_connectors("OCI")
        else:
            code, text = client.get(f"/{provider}/connectors")
    except Exception as e:  # noqa: BLE001
        return [], f"error:{type(e).__name__}"
    if code in (401, 403):
        return [], "auth"
    if code != 200:
        return [], f"error:{code}"
    conns = _extract_connectors(provider, text)
    return conns, ("ok" if conns else "empty")


# --------------------------------------------------------------------------------------
# Ejecución de un reporte (poll + download) — solo bajo --run
# --------------------------------------------------------------------------------------

def _run_assessment(client, body: dict, out_dir: Path, label: str, fmt: str,
                    poll_interval: int, poll_timeout: int, log, sleep=time.sleep) -> dict:
    code, text = client.post("/report/assessment/create", body)
    if not (200 <= code < 300):
        log.info(f"{label} assessment create FAILED http={code} :: {text[:200]}")
        return {"ok": False, "stage": "create", "http": code, "detail": text[:300]}
    rid = _first(json.loads(text) if text.strip().startswith("{") else {}, _REPORT_ID_KEYS)
    if not rid:
        return {"ok": False, "stage": "create", "http": code, "detail": "sin reportId en la respuesta"}
    log.info(f"{label} assessment reportId={rid} -> polling")
    waited = 0
    while waited < poll_timeout:
        sleep(poll_interval)
        waited += poll_interval
        sc, st = client.get("/report/assessment/list", {"reportId": rid})
        status = _status_of(st)
        if status in _DONE_OK:
            break
        if status in _DONE_BAD:
            return {"ok": False, "stage": "poll", "report_id": rid, "status": status}
    else:
        return {"ok": False, "stage": "poll-timeout", "report_id": rid, "waited": waited}
    out_path = out_dir / f"assessment-{slugify(label)}.{fmt.lower()}"
    dc, n = client.download(f"/report/assessment/{rid}/download",
                            str(out_path), {"reportFormat": fmt.lower()})
    if not (200 <= dc < 300):
        return {"ok": False, "stage": "download", "http": dc, "report_id": rid}
    log.info(f"{label} assessment OK -> {out_path} ({n} bytes)")
    return {"ok": True, "report_id": rid, "path": str(out_path), "bytes": n}


def _run_mandate(client, body: dict, label: str, log) -> dict:
    code, text = client.post("/reports", body)
    if not (200 <= code < 300):
        log.info(f"{label} mandate create FAILED http={code} :: {text[:200]}")
        return {"ok": False, "stage": "create", "http": code, "detail": text[:300]}
    rid = _first(json.loads(text) if text.strip().startswith("{") else {}, _REPORT_ID_KEYS)
    log.info(f"{label} mandate creado reportId={rid or '(s/id)'} — descargá el PDF desde consola "
             f"(Reports) si el tenant no expone download por API")
    return {"ok": True, "report_id": rid, "note": "download del mandate vía consola"}


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------

def main(argv=None, client=None) -> int:
    ap = argparse.ArgumentParser(description="Extracción de reportes CSPM (Assessment + Mandate). "
                                             "DRY-RUN por defecto; --run para ejecutar (mutación).")
    ap.add_argument("--provider", default="all", choices=["all", *_PROVIDERS])
    ap.add_argument("--account", default="", help="account/subscription/project/tenant id (opcional).")
    ap.add_argument("--reports", default="assessment,mandate",
                    help="coma: assessment,mandate (default ambos).")
    ap.add_argument("--mandate", default="", choices=["", "iso27001", "nist", "gdpr"],
                    help="mandate afín a usar (default: auto ISO27001->NIST->GDPR).")
    ap.add_argument("--name", default="Ley 21.719", help="prefijo del nombre/título del reporte.")
    ap.add_argument("--assessment-format", default="csv", choices=["csv", "pdf"])
    ap.add_argument("--results", default="FAIL", help="coma: FAIL,PASS,PASSE (assessment).")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent
                                         / "artifacts" / "cloud-reports"))
    ap.add_argument("--poll-interval", type=int, default=15)
    ap.add_argument("--poll-timeout", type=int, default=600)
    ap.add_argument("--create-gap", type=int, default=3, help="segundos entre creates (cortesía).")
    ap.add_argument("--run", action="store_true",
                    help="EJECUTAR (mutación: crea reportes). Sin esto = DRY-RUN (solo plan).")
    ap.add_argument("--pod", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    if args.account and args.provider == "all":
        raise SystemExit("[error] --account requiere un --provider específico (no 'all').")
    want = {r.strip().lower() for r in args.reports.split(",") if r.strip()}
    results = [r.strip() for r in args.results.split(",") if r.strip()]

    base = Path(args.out)
    preflight_writable(base)
    run_dir, run_id = resolve_run_dir(base)
    run_dir.mkdir(parents=True, exist_ok=True)
    log = setup_run_log(run_dir)
    mode = "RUN (mutación)" if args.run else "DRY-RUN (plan, read-only)"
    log.info(f"start run_id={run_id} mode={'run' if args.run else 'dry-run'} "
             f"provider={args.provider} reports={sorted(want)} account={args.account or 'auto'}")

    if client is None:                               # DI: los tests inyectan un cliente fake
        from qualys_client.cloud_reports import CloudReportClient
        from qualys_client.cloud_reports import from_env as rep_from_env
        if args.pod and args.user and args.password:
            client = CloudReportClient(args.pod, args.user, args.password,
                                       server=args.server, debug=args.debug)
        else:
            client = rep_from_env(server=args.server)

    banner = "🟢 DRY-RUN — no se crea nada (solo descubrimiento + plan)" if not args.run else \
        "🔴 RUN — se CREARÁN reportes (mutación del tenant)"
    print(f"== extract_cloud_reports — {mode} ==\n{banner}\nhost={client.server}\n", flush=True)

    providers = list(_PROVIDERS) if args.provider == "all" else [args.provider]
    summary = []
    for prov in providers:
        ct = _CLOUD_TYPE[prov]
        conns, state = _discover(client, prov)
        if args.account:
            conns = [c for c in conns if c["account"] == args.account] or \
                    [{"account": args.account, "connector_id": "", "name": "(--account)"}]
            state = "ok"
        if state != "ok":
            print(f"  [{prov}] {('🔐 auth' if state == 'auth' else '∅/⚠️ ' + state)} — skip",
                  flush=True)
            log.info(f"{prov} discover={state}")
            continue

        # policies del cloudType (para assessment y mandate)
        pc, pt = client.get("/reports/policies", {"cloudType": ct})
        policies = _extract_policies(pt) if 200 <= pc < 300 else []
        policy_ids = [p["policy_id"] for p in policies]
        if not policy_ids:
            print(f"  [{prov}] sin policies para cloudType={ct} (http={pc}) — "
                  f"{'no se puede armar el reporte' if state == 'ok' else state}", flush=True)
            log.info(f"{prov} policies http={pc} count=0")

        mandate = None
        if "mandate" in want:
            mc, mt = client.get("/reports/mandates")
            mandate = _pick_mandate(mt, args.mandate or None) if 200 <= mc < 300 else None

        for c in conns:
            acct = c["account"]
            cids = [c["connector_id"]] if c["connector_id"] else []
            label = f"{prov}/{acct}"
            out_dir = run_dir / prov / slugify(acct)
            jobs = []
            if "assessment" in want and policy_ids:
                jobs.append(("assessment",
                             _assessment_body(f"{args.name} - {prov}", ct, policy_ids, cids,
                                              results, args.assessment_format)))
            if "mandate" in want and policy_ids and mandate:
                jobs.append(("mandate",
                             _mandate_body(f"{args.name} (mandate afín) - {prov}", ct,
                                           mandate["mandate_id"], policy_ids, cids)))

            for kind, body in jobs:
                if not args.run:
                    print(f"  [PLAN] {label} {kind}: connectors={cids or '∅'} "
                          f"policies={len(policy_ids)} "
                          f"{'mandate=' + mandate['name'] if kind == 'mandate' else ''}".rstrip(),
                          flush=True)
                    log.info(f"PLAN {label} {kind} body={json.dumps(body)[:400]}")
                    summary.append({"label": label, "kind": kind, "planned": True})
                    continue
                out_dir.mkdir(parents=True, exist_ok=True)
                if kind == "assessment":
                    r = _run_assessment(client, body, out_dir, f"{label}", args.assessment_format,
                                        args.poll_interval, args.poll_timeout, log)
                else:
                    r = _run_mandate(client, body, label, log)
                ok = r.get("ok")
                tag = "✓" if ok else "✗"
                extra = ""
                if not ok and prov == "oci":
                    extra = "  (⚠️ OCI en reportes no ejercitado en el smoke → revisá; fallback: consola)"
                print(f"  [{tag}] {label} {kind}{(' -> ' + r.get('path')) if r.get('path') else ''}"
                      f"{extra}", flush=True)
                summary.append({"label": label, "kind": kind, **r})
                time.sleep(args.create_gap)

    link_latest(run_dir)
    okc = sum(1 for s in summary if s.get("ok"))
    plan = sum(1 for s in summary if s.get("planned"))
    log.info(f"done jobs={len(summary)} ok={okc} planned={plan} "
             f"gets={client.call_count} posts={client.mutations} out={run_dir}")
    if not args.run:
        print(f"\n[DRY-RUN] {plan} reporte(s) en el plan. Re-corré con --run para crearlos. "
              f"({client.call_count} GETs, 0 POSTs)", flush=True)
    else:
        print(f"\n[RUN] {okc}/{len(summary)} OK. GETs={client.call_count} POSTs={client.mutations}. "
              f"Salida: {run_dir}/  (los reportes se autoborran a los 7 días en Qualys)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
