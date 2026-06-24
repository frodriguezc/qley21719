#!/usr/bin/env python3
"""tenant_coverage_pack.py — arma un Compliance Pack de la Ley 21.719 a MEDIDA de un tenant.

Qué hace (todo **READ-ONLY** sobre el tenant; usa qualys_client, que bloquea estructuralmente
cualquier escritura):
  1. Barre el inventario (FO asset/host) -> consolida el **SO de la flota**.
  2. Best-effort: intenta inferir **DB/middleware** del software del inventario (CSAM); lo no
     detectable queda como "verificar manualmente".
  3. Lista las **policies ya importadas** (FO compliance/policy list) y las cruza con un catálogo
     curado (mapping/cis_catalog.yaml) -> qué benchmarks CIS YA están vs cuáles FALTAN.
  4. Genera el **policy.xml** de la ley cosechando los benchmarks CIS YA importados que aplican.
  5. Emite **faltantes.txt** (benchmarks a importar desde la librería para cobertura completa) y
     **subir.sh** (el comando de import, listo para que lo corra el CLIENTE — human-gate).

El **import NO lo hace este script** (read-only): "Import from Library" y "action=import" son
mutaciones que ejecuta el cliente. Ver subir.sh / faltantes.txt.

Uso:
  QUALYS_POD=US03 QUALYS_API_USER=... QUALYS_API_PASSWORD=... \
    .venv/bin/python scripts/tenant_coverage_pack.py --name "Ley 21.719 - <cliente>"
  # o con credenciales explícitas:
  ... --pod US03 --user U --password P
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from qualys_client import QualysClient, from_env  # noqa: E402
from compliance_pack import generate_pack  # noqa: E402
from scripts._runtime import (  # noqa: E402
    slugify, resolve_run_dir, preflight_writable, link_latest, setup_run_log)


# --------------------------------------------------------------------------- #
# Lectura del tenant (READ-ONLY)
# --------------------------------------------------------------------------- #
def _fleet_os(client: QualysClient, max_hosts: int, page: int = 1000) -> tuple[Counter, int, int]:
    """Pagina FO asset/host (action=list) y devuelve (Counter de OS, total_hosts, sin_os)."""
    os_counts: Counter = Counter()
    total = no_os = 0
    params = {"action": "list", "details": "All", "truncation_limit": str(min(page, max_hosts))}
    while total < max_hosts:
        http, text = client.fo_get("/api/5.0/fo/asset/host/", params)
        if http != 200:
            raise RuntimeError(f"asset/host list -> HTTP {http}")
        root = ET.fromstring(text)
        for h in root.findall(".//HOST"):
            total += 1
            os_txt = (h.findtext("OS") or "").strip()
            if os_txt:
                os_counts[os_txt] += 1
            else:
                no_os += 1
        print(f"      … {total} hosts barridos", flush=True)
        warn = root.find(".//WARNING/URL")
        if warn is None or not (warn.text or "").strip():
            break
        qs = parse_qs(urlparse(warn.text.strip()).query)
        id_min = (qs.get("id_min") or [None])[0]
        if not id_min:
            break
        params["id_min"] = id_min
    return os_counts, total, no_os


def _fleet_software(client: QualysClient, max_hosts: int, page: int = 500) -> set[str]:
    """Best-effort: nombres de software del inventario CSAM (QPS /search/am/hostasset). Si el tenant
    no tiene CSAM o el campo no viene, devuelve set() (-> db/middleware caen en 'verificar').

    Pagina por cursor (startFromId/lastId) igual que _fleet_os/_fetch_all_evaluations: en tenants con
    >page host assets, una sola página subreportaba el software (-> faltantes.txt sección [B]). Sigue
    siendo best-effort: cualquier HTTP!=200, error o schema viejo degrada al set acumulado."""
    found: set[str] = set()
    start_from_id: int | None = None
    scanned = 0
    try:
        while scanned < max_hosts:
            want = min(page, max_hosts - scanned)
            http, text = client.qps_search("/qps/rest/2.0/search/am/hostasset",
                                           limit=want, start_from_id=start_from_id)
            if http != 200:
                break
            root = ET.fromstring(text)
            page_hosts = 0
            # CSAM expone software bajo .../softwareList/HostAssetSoftware/name (varía por versión);
            # juntamos cualquier <name>/<fullName> que cuelgue de un nodo *software*, y contamos
            # los <HostAsset> de la página para avanzar el cursor sin re-leer.
            for el in root.iter():
                tag = el.tag.lower()
                if tag == "hostasset":
                    page_hosts += 1
                elif "software" in tag:
                    for child in el.iter():
                        if child.tag.lower() in ("name", "fullname") and (child.text or "").strip():
                            found.add(child.text.strip().lower())
            scanned += page_hosts
            has_more = (root.findtext(".//hasMoreRecords") or "").strip().lower() == "true"
            last_id = (root.findtext(".//lastId") or "").strip()
            if page_hosts == 0 or not has_more or not last_id.isdigit():
                break
            start_from_id = int(last_id) + 1
    except Exception:
        return found
    return found


def _imported_policies(client: QualysClient) -> list[tuple[str, str]]:
    """Devuelve [(policy_id, title)] de las policies del tenant (FO compliance/policy list)."""
    http, text = client.fo_get("/api/4.0/fo/compliance/policy/", {"action": "list"})
    if http != 200:
        raise RuntimeError(f"compliance/policy list -> HTTP {http}")
    root = ET.fromstring(text)
    out = []
    for p in root.findall(".//POLICY"):
        pid = (p.findtext("ID") or "").strip()
        title = (p.findtext("TITLE") or "").strip()
        if pid:
            out.append((pid, title))
    return out


# --------------------------------------------------------------------------- #
# Reconciliación flota <-> catálogo <-> policies importadas
# --------------------------------------------------------------------------- #
def _any(patterns, text) -> bool:
    return any(re.search(p, text, re.I) for p in (patterns or []))


def reconcile(catalog: dict, os_counts: Counter, software: set[str],
              policies: list[tuple[str, str]]) -> dict:
    """Cruza el catálogo curado con la flota (os/software) y las policies importadas (title)."""
    targets = catalog["targets"]
    titles = [t for _, t in policies]

    rows = []
    for tg in targets:
        # ¿presente en la flota?
        fleet_hosts = sum(n for osx, n in os_counts.items() if _any(tg.get("os_match"), osx))
        # match de software por nombre individual (no contra un blob unido -> evita cruces falsos)
        sw_hit = any(_any(tg.get("software_match"), s) for s in software)
        present_in_fleet = fleet_hosts > 0 or sw_hit
        # ¿ya importado? (match de title contra las policies del tenant)
        imported_ids = [pid for pid, ti in policies if _any(tg.get("title_match"), ti)]
        rows.append({
            "key": tg["key"], "group": tg["group"], "kind": tg["kind"],
            "benchmark": tg["benchmark"], "pillars": tg.get("pillars", []),
            "fleet_hosts": fleet_hosts, "sw_hit": sw_hit,
            "present_in_fleet": present_in_fleet,
            "imported_ids": imported_ids, "imported": bool(imported_ids),
        })
    return {"rows": rows}


# --------------------------------------------------------------------------- #
# Emisión de faltantes.txt + subir.sh
# --------------------------------------------------------------------------- #
def _write_faltantes(path: Path, catalog: dict, rec: dict, os_counts: Counter, total_hosts: int,
                     no_os: int, software_seen: bool, present_sources: list[dict],
                     pod: str, name: str, now: str) -> None:
    L = []
    L += [f"COBERTURA TÉCNICA — Ley 21.719 — POD {pod} — {now}",
          f"Policy: {name}", "=" * 72, ""]
    L += [f"Flota: {total_hosts} hosts barridos ({no_os} sin SO detectado / sin auth scan).", ""]
    L += ["SO detectado en la flota (top):"]
    for osx, n in os_counts.most_common(40):
        L.append(f"  {n:4d}  {osx}")
    if not os_counts:
        L.append("  (ninguno — la flota no tiene SO resuelto; correr auth scans)")
    L += ["", "-" * 72, "Benchmarks CIS YA importados que aplican (fuentes del policy.xml):"]
    if present_sources:
        for s in present_sources:
            L.append(f"  [{s['id']}] {s['title']}")
    else:
        L.append("  (ninguno — el policy.xml no se generó: importa al menos un benchmark CIS)")

    detected_missing = [r for r in rec["rows"]
                        if r["present_in_fleet"] and not r["imported"] and r["kind"] == "os"]
    # non-OS (db/middleware/container/infra) no importados: si se detectó por software -> "detectado";
    # si no -> "verificar manualmente" (Qualys no siempre fingerprintea el motor sin CSAM/auth scan).
    verify = [r for r in rec["rows"] if not r["imported"] and r["kind"] != "os"]
    detected_sw = [r for r in verify if r["sw_hit"]]
    verify_only = [r for r in verify if not r["sw_hit"]]

    L += ["", "=" * 72,
          "FALTAN IMPORTAR (Import from Library, human-gate) para cobertura completa:", ""]
    L += ["[A] Detectados en la flota por SO, sin benchmark importado:"]
    if detected_missing:
        for r in sorted(detected_missing, key=lambda x: -x["fleet_hosts"]):
            L.append(f"  - {r['benchmark']}   ({r['fleet_hosts']} hosts · {r['group']})")
    else:
        L.append("  (ninguno — el SO de la flota ya está cubierto por benchmarks importados)")

    L += ["", "[B] Detectados por software (best-effort), sin benchmark importado:"]
    if detected_sw:
        for r in detected_sw:
            L.append(f"  - {r['benchmark']}   ({r['group']})")
    else:
        L.append("  (ninguno detectado automáticamente)")

    L += ["", "[C] DB / middleware / infra — VERIFICAR manualmente si corren en la flota:",
          "    (Qualys no siempre fingerprintea el motor sin CSAM/auth scan -> confirmar con el DBA/infra)"]
    for r in verify_only:
        L.append(f"  - {r['benchmark']}   ({r['group']})")

    L += ["", "-" * 72, "Fuera del alcance de Policy Compliance (van por OTRO motor de Qualys):"]
    other = [o for o in catalog.get("additional_domains", []) if not o.get("pc_importable")]
    for o in other:
        L.append(f"  - {o.get('group', o.get('key'))}: {o.get('benchmark', '')}  ({o.get('qualys_app', '')})")
    if not other:
        L.append("  (ninguno)")
    L += ["", "Cómo importar un benchmark: PA > Policies > New > Policy > Import from Library > <nombre>.",
          "Tras importar: re-correr este script para sumarlo al policy.xml, o regenerar el pack.", ""]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def _write_subir_sh(path: Path, server: str, levels: dict, name: str) -> None:
    base = server.rstrip("/")
    lines = ["#!/usr/bin/env bash",
             "# Import de la policy generada (lo corre el CLIENTE — human-gate).",
             "# READ-ONLY de la herramienta: este paso NO lo ejecuta el generador.",
             "# Requiere QUALYS_API_USER / QUALYS_API_PASSWORD en el entorno.",
             "set -euo pipefail", ""]
    for lid, lv in levels.items():
        xml = Path(lv["out_dir"]) / "policy.xml"
        title = f"{name} ({lid})"
        lines += [f'# --- nivel {lid} ({lv.get("included","?")} controles) ---',
                  'curl -sS -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" \\',
                  '     -H "X-Requested-With: tenant-coverage-pack" \\',
                  '     -H "Content-Type: text/xml" \\',
                  f'     --data-binary @"{xml}" \\',
                  f'     "{base}/api/4.0/fo/compliance/policy/?action=import'
                  f'&title={_q(title)}&create_user_controls=0"', ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _q(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s)


def _match_ley_policies(policies: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    """Policies del tenant cuyo título matchea el pack (para merge in-place / drift). Normaliza y
    compara por contención del núcleo del nombre o por la referencia a la ley (21.719 / 21719)."""
    core = slugify(name).replace("-", "")
    out = []
    for pid, title in policies:
        t, ts = title.lower(), slugify(title).replace("-", "")
        if (core and core in ts) or "21.719" in t or "21719" in ts:
            out.append((pid, title))
    return out


def _write_subir_merge_sh(path: Path, server: str, levels: dict, name: str,
                          existing_id: str | None, candidates: list[tuple[str, str]]) -> None:
    """ALTERNATIVA a subir.sh: actualizar IN-PLACE una policy Ley YA importada y afinada, en vez de
    re-importar una nueva. Emite preview_merge=1 (PASO 1, no-committing) y el merge real comentado
    (PASO 2). Lo corre el CLIENTE; la herramienta es READ-ONLY y NO ejecuta esto."""
    base = server.rstrip("/")
    lines = [
        "#!/usr/bin/env bash",
        "# ALTERNATIVA a subir.sh: ACTUALIZAR IN-PLACE una policy Ley YA importada y afinada,",
        "# en vez de re-importar una nueva. Lo corre el CLIENTE (human-gate); la herramienta es",
        "# READ-ONLY y NO ejecuta esto. Requiere QUALYS_API_USER / QUALYS_API_PASSWORD en el entorno.",
        "#",
        "# OJO: action=merge&update_existing_controls=1 SOBREESCRIBE en la policy destino los controles",
        "# comunes (status/criticidad/valores). Tus EXCEPCIONES y valores ajustados de esos CIDs se",
        "# pierden. Por eso el PASO 1 es un preview_merge=1 (no guarda nada): revisa el diff primero.",
        "# Para SUMAR cobertura sin tocar tu tuning, prefiere re-importar como policy nueva (subir.sh).",
        "set -euo pipefail", ""]
    if existing_id:
        lines += [f'POLICY_ID="{existing_id}"   # policy Ley detectada en el tenant', ""]
    else:
        lines.append("# No se detectó UNA sola policy Ley en el tenant. Completa el id a mano:")
        for pid, title in candidates:
            lines.append(f"#   candidato id={pid}  title={title!r}")
        lines += ['POLICY_ID="<EXISTING_POLICY_ID>"   # <-- COMPLETAR', ""]
    for lid, lv in levels.items():
        xml = Path(lv["out_dir"]) / "policy.xml"
        url = f"{base}/api/4.0/fo/compliance/policy/?action=merge&id=$POLICY_ID&update_existing_controls=1"
        lines += [
            f'# --- nivel {lid} ({lv.get("included", "?")} controles) ---',
            "# PASO 1 — PREVIEW (no guarda nada): revisa qué cambiaría.",
            'curl -sS -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" \\',
            '     -H "X-Requested-With: tenant-coverage-pack" \\',
            '     -H "Content-Type: text/xml" \\',
            f'     --data-binary @"{xml}" \\',
            f'     "{url}&preview_merge=1"',
            "",
            "# PASO 2 — COMMIT (descomentar SOLO tras revisar el preview):",
            '# curl -sS -u "$QUALYS_API_USER:$QUALYS_API_PASSWORD" \\',
            '#      -H "X-Requested-With: tenant-coverage-pack" \\',
            '#      -H "Content-Type: text/xml" \\',
            f'#      --data-binary @"{xml}" \\',
            f'#      "{url}"', ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


# --------------------------------------------------------------------------- #
# Orquestador
# --------------------------------------------------------------------------- #
def run(args) -> int:
    if args.pod and args.user and args.password:
        client = QualysClient(args.pod, args.user, args.password)
    else:
        client = from_env()
    client.debug = getattr(args, "debug", False)  # surfacing del throttle/backoff (stderr, secret-safe)

    catalog = yaml.safe_load((ROOT / args.catalog).read_text())
    base = Path(args.out)
    preflight_writable(base)                      # falla rápido ANTES de tocar el tenant
    run_dir, run_id = resolve_run_dir(base, slugify(args.name))
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_dir
    log = setup_run_log(run_dir)
    # Persistir cada backoff al run.log (UTC, secret-safe) aunque no se pase --debug: el throttle
    # silencioso era invisible. El filtro de redacción del handler es defensa en profundidad.
    client.on_throttle = lambda note: log.warning(f"throttle {note}")
    log.info(f"start run_id={run_id} pod={client.pod} server={client.server} "
             f"out={out} max_hosts={args.max_hosts} level={args.level or 'all'} name={args.name!r}")

    print(f"[1/5] Barriendo inventario (POD {client.pod}, máx {args.max_hosts} hosts)…")
    os_counts, total, no_os = _fleet_os(client, args.max_hosts)
    print(f"      {total} hosts · {len(os_counts)} variantes de SO · {no_os} sin SO")
    log.info(f"fleet hosts={total} os_variants={len(os_counts)} no_os={no_os}")

    print("[2/5] Inferencia best-effort de software (CSAM)…")
    software = _fleet_software(client, args.max_hosts)
    print(f"      {len(software)} nombres de software ({'CSAM disponible' if software else 'sin software -> DB/middleware a verificar'})")
    log.info(f"software names={len(software)} csam={'yes' if software else 'no'}")

    print("[3/5] Listando policies importadas…")
    policies = _imported_policies(client)
    print(f"      {len(policies)} policies en el tenant")
    log.info(f"policies imported={len(policies)}")

    rec = reconcile(catalog, os_counts, software, policies)

    # fuentes = policies importadas que matchean un benchmark del catálogo (CIS) -> a cosechar
    src_ids, present_sources, seen = [], [], set()
    title_by_id = {pid: ti for pid, ti in policies}
    for r in rec["rows"]:
        for pid in r["imported_ids"]:
            if pid not in seen:
                seen.add(pid)
                src_ids.append(pid)
                present_sources.append({"id": pid, "title": title_by_id.get(pid, "")})

    now = _now()
    result = None
    if src_ids:
        print(f"[4/5] Generando policy.xml desde {len(src_ids)} benchmarks importados…")
        # spec relativo a ROOT (viaja con la herramienta), igual que el catálogo -> CWD-independiente.
        spec_path = args.spec if Path(args.spec).is_absolute() else str(ROOT / args.spec)

        def _pack_progress(msg: str) -> None:  # latido del harvest: a stdout (UX) y al run.log (post-mortem)
            print(f"      … {msg}", flush=True)
            log.info(f"pack {msg}")

        result = generate_pack(spec_path, str(out), client=client, source_ids=src_ids,
                               level=args.level or None, refresh=args.refresh, ui_safe=True,
                               progress=_pack_progress)
        print(f"      harvested={result['harvested']} classified={result['classified']} "
              f"unclassified={result['unclassified']} ok={result['ok']}")
        log.info(f"pack source={result['source']} harvested={result['harvested']} "
                 f"classified={result['classified']} unclassified={result['unclassified']} "
                 f"src_ids={','.join(src_ids)} ok={result['ok']}")
    else:
        print("[4/5] Sin benchmarks CIS importados -> se omite el policy.xml (solo faltantes).")
        log.info("pack skipped (sin benchmarks CIS importados)")

    print("[5/5] Emitiendo faltantes.txt + subir.sh…")
    _write_faltantes(out / "faltantes.txt", catalog, rec, os_counts, total, no_os,
                     bool(software), present_sources, client.pod, args.name, now)
    drift_done = None
    if result:
        _write_subir_sh(out / "subir.sh", client.server, result["levels"], args.name)
        # subir-merge.sh: alternativa de merge in-place (preview-first) para una policy ya afinada.
        cand = _match_ley_policies(policies, args.name)
        _write_subir_merge_sh(out / "subir-merge.sh", client.server, result["levels"], args.name,
                              cand[0][0] if len(cand) == 1 else None, cand)
        if args.drift:
            drift_done = _emit_drift(out, client, policies, title_by_id, result, args, log)

    link_latest(run_dir)
    log.info(f"done calls={client.call_count} out={out}")
    print(f"\nSalida: {out}/   (también: {run_dir.parent / 'latest'})")
    print(f"  - faltantes.txt   (qué importar para cobertura completa)")
    if result:
        for lid, lv in result["levels"].items():
            print(f"  - {lid}/policy.xml ({lv['included']} controles)")
        print(f"  - subir.sh        (import como policy NUEVA — lo corre el CLIENTE, human-gate)")
        print(f"  - subir-merge.sh  (merge IN-PLACE con preview — lo corre el CLIENTE, human-gate)")
        if drift_done is not None:
            print(f"  - drift.md        ({drift_done})")
    print(f"  - run.log         (traza de la corrida, sin credenciales)")
    return 0


def _emit_drift(out: Path, client, policies, title_by_id, result, args, log) -> str:
    """Emite drift.md: export READ-ONLY de la policy Ley viva vs el pack regenerado. Devuelve una
    etiqueta corta para el resumen en consola. Nunca muta nada."""
    from compliance_pack import drift as _drift
    from compliance_pack.generator import _export_policy
    lvls = result["levels"]
    gen_lid = "sensible" if "sensible" in lvls else next(iter(lvls))
    gen_path = Path(lvls[gen_lid]["out_dir"]) / "policy.xml"

    if args.drift_policy_id:
        cand = [(args.drift_policy_id, title_by_id.get(args.drift_policy_id, "(id forzado)"))]
    else:
        cand = _match_ley_policies(policies, args.name)

    if len(cand) != 1:
        msg = ("# Drift — policy Ley importada vs pack regenerado\n\n" + (
            "Sin policy Ley previa en el tenant — nada que diferenciar. "
            "(Tras importar `subir.sh` una primera vez, una próxima corrida con `--drift` la comparará.)\n"
            if not cand else
            "Varias policies candidatas; re-corre con `--drift-policy-id <id>`:\n\n"
            + "".join(f"- `{pid}`  {ti}\n" for pid, ti in cand)))
        (out / "drift.md").write_text(msg, encoding="utf-8")
        log.info(f"drift candidates={len(cand)} (no único) -> drift.md informativo")
        return "sin policy previa" if not cand else "varias candidatas (ver drift.md)"

    live_pid, live_title = cand[0]
    try:
        live = _drift.walk_controls(_export_policy(client, live_pid))
        gen = _drift.walk_controls(ET.fromstring(gen_path.read_text(encoding="utf-8")))
        d = _drift.diff_cids(live, gen, ui_safe=True)
        md = _drift.render_md(d, live_title, lvls[gen_lid]["title"], gen_lid, live_pid, ui_safe=True)
        (out / "drift.md").write_text(md, encoding="utf-8")
        log.info(f"drift live_pid={live_pid} missing={len(d['missing_from_live'])} "
                 f"extra={len(d['extra_in_live'])} changed={len(d['changed'])}")
        return (f"vs id {live_pid}: {len(d['missing_from_live'])} a sumar / "
                f"{len(d['extra_in_live'])} solo viva / {len(d['changed'])} cambiados")
    except Exception as e:  # noqa: BLE001 — best-effort: drift no debe abortar el pack
        (out / "drift.md").write_text(
            f"# Drift\n\nNo se pudo generar (export de la policy `{live_pid}`): {e}\n", encoding="utf-8")
        log.info(f"drift error {e}")
        return f"error (ver drift.md)"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser(description="Compliance Pack a medida de un tenant (read-only).")
    ap.add_argument("--name", default="Ley 21.719 - Medidas de Seguridad", help="Título de la policy.")
    ap.add_argument("--spec", default="mapping/ley21719.yaml", help="Spec YAML de la ley.")
    ap.add_argument("--catalog", default="mapping/cis_catalog.yaml", help="Catálogo tecnología->benchmark.")
    ap.add_argument("--out", default="artifacts/tenant-pack", help="Directorio de salida.")
    ap.add_argument("--level", default="", help="Nivel (base|sensible). Vacío = ambos.")
    ap.add_argument("--max-hosts", type=int, default=5000, help="Tope de hosts a barrer.")
    ap.add_argument("--refresh", action="store_true", help="Forzar re-cosecha live del harvest.")
    ap.add_argument("--drift", action="store_true",
                    help="Emitir drift.md: export READ-ONLY de la policy Ley ya importada vs el pack regenerado.")
    ap.add_argument("--drift-policy-id", default="",
                    help="Forzar el id de la policy viva para el drift (si hay varias candidatas).")
    ap.add_argument("--pod", default="", help="POD (si no, env/.env).")
    ap.add_argument("--user", default="", help="API user (si no, env/.env).")
    ap.add_argument("--password", default="", help="API password (si no, env/.env).")
    ap.add_argument("--debug", action="store_true",
                    help="Emitir a stderr el diagnóstico de throttle/backoff (concurrency vs rate, "
                         "headers de Qualys, segundos de espera). Sin credenciales. Default: off.")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
