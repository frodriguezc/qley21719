#!/usr/bin/env bash
# run.sh — orquestador END-TO-END del pack Ley 21.719 (Qualys), READ-ONLY.
#
# Crea/usa el venv, instala requirements, verifica módulos (POL/TC), corre ambos motores
# y arma el folder `deliverables/`. NO muta el tenant (los scripts son read-only; el import
# lo corre el cliente con subir.sh). Monitoreo: banners con timestamp + heartbeat (segundos)
# en pasos largos/silenciosos + salida en vivo de los motores -> nunca parece "stalled".
#
# Uso (un tenant):
#   cp .env.example .env   # completar QUALYS_POD/QUALYS_API_USER/QUALYS_API_PASSWORD (read-only)
#   ./run.sh
#
# Uso (varios tenants, reportes SEPARADOS):
#   cp .env.example .env.clienteA   # credenciales del cliente A (gitignored, igual que .env)
#   cp .env.example .env.clienteB   # credenciales del cliente B
#   TENANT=clienteA ./run.sh        # -> deliverables/clienteA/  + artifacts/clienteA/
#   TENANT=clienteB ./run.sh        # -> deliverables/clienteB/  + artifacts/clienteB/
#   Cada tenant aísla credenciales, reportes y caché de harvest; los demás NO se tocan.
#   (Si no existe .env.<tenant>, cae a las credenciales del entorno/.env.)
#
# Variables opcionales:  MAX_HOSTS=3000  PACK_NAME="Ley 21.719 - <cliente>"  TENANT=<slug>  ./run.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
VENV="$HERE/.venv"; PY="$VENV/bin/python"

# --------------------------------------------------------------------------- #
# Helpers (sin estado; se pueden testear sourceando con QLEY_RUNSH_LIB=1).
# --------------------------------------------------------------------------- #
ts(){ printf '\033[2K\r[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die(){ ts "✗ ERROR: $*"; exit 1; }

# lee una clave de un env-file (KEY=VALUE; ignora comillas, espacios y comentarios inline). Vacío si no está.
dotenv_get_from(){ [ -f "$1" ] || return 0
  grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" 2>/dev/null | tail -1 \
    | sed -E "s/^[^=]*=[[:space:]]*//; s/[[:space:]]+#.*$//; s/[[:space:]]*$//; s/^[\"']//; s/[\"']$//"; }

# slug seguro para nombre de carpeta (mismo criterio que slugify() de la herramienta): sin
# path-traversal, minúsculas, solo [a-z0-9._-], colapsa separadores, recorta a 64.
slug_of(){ local s
  s="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[-.]+//; s/[-.]+$//')"
  printf '%s' "${s:0:64}"; }

# El motor PC escribe en <out>/<slug-name>/<run_id_UTC>/ (+ symlink latest). Devuelve la
# carpeta de la corrida MÁS NUEVA (run_id UTC ordena cronológicamente). Vacío si no hay.
latest_run(){ local d
  d="$(ls -d "$1"/*/[0-9]*Z 2>/dev/null | sort | tail -1)"
  [ -n "$d" ] && [ -d "$d" ] && printf '%s\n' "$d"; }

# El motor CSPM escribe en <out>/<run_id_UTC>/<prov>/<cuenta>/ (+ symlink latest).
latest_run_cspm(){ local d
  d="$(ls -d "$1"/[0-9]*Z 2>/dev/null | sort | tail -1)"
  [ -n "$d" ] && [ -d "$d" ] && printf '%s\n' "$d"; }

# Consolida la salida del motor PC (run_dir) al folder deliverables. Lee la corrida REAL
# (anidada por slug-name/run_id), no rutas planas — ese era el bug que dejaba 2-policy-xml/ vacío.
consolidate_pol(){ local RUN="$1" DELIV="$2" lvl f
  [ -n "$RUN" ] && [ -d "$RUN" ] || return 1
  mkdir -p "$DELIV/2-policy-xml"
  [ -f "$RUN/faltantes.txt" ]  && cp -f "$RUN/faltantes.txt"  "$DELIV/1-CIS-a-importar-en-POL.txt"
  [ -f "$RUN/subir.sh" ]       && cp -f "$RUN/subir.sh"       "$DELIV/2-policy-xml/subir.sh"
  [ -f "$RUN/subir-merge.sh" ] && cp -f "$RUN/subir-merge.sh" "$DELIV/2-policy-xml/subir-merge.sh"
  [ -f "$RUN/drift.md" ]       && cp -f "$RUN/drift.md"       "$DELIV/2-policy-xml/drift.md"
  for lvl in base sensible; do
    [ -d "$RUN/$lvl" ] || continue
    mkdir -p "$DELIV/2-policy-xml/$lvl"
    for f in policy.xml import-instructions.md mapping.csv gaps.md; do
      [ -f "$RUN/$lvl/$f" ] && cp -f "$RUN/$lvl/$f" "$DELIV/2-policy-xml/$lvl/$f"
    done
  done
  return 0; }

# Consolida la salida del motor CSPM (run_dir) al folder deliverables (sin el nivel run_id).
consolidate_cspm(){ local RUNC="$1" DELIV="$2"
  [ -n "$RUNC" ] && [ -d "$RUNC" ] || return 1
  mkdir -p "$DELIV/3-cloud-posture-CSPM"
  cp -R "$RUNC"/. "$DELIV/3-cloud-posture-CSPM/"
  return 0; }

# heartbeat: corre "$@" en background mostrando segundos; loguea; en fallo vuelca el log.
hb(){ local label="$1"; shift; local log; log="$(mktemp)"; ts "▶ $label"
  ( "$@" ) >"$log" 2>&1 & local pid=$! s=0
  while kill -0 "$pid" 2>/dev/null; do printf '\r    … %s — %ds' "$label" "$s"; sleep 3; s=$((s+3)); done
  wait "$pid"; local rc=$?; printf '\r\033[2K'
  if [ "$rc" -ne 0 ]; then ts "✗ $label (rc=$rc)"; sed 's/^/      /' "$log"; rm -f "$log"; exit "$rc"; fi
  ts "✓ $label (${s}s)"; rm -f "$log"; }

# stream: corre "$@" con salida en vivo (los motores imprimen su propio progreso). Falla -> die.
stream(){ local label="$1"; shift; ts "▶ $label"; "$@" 2>&1 | sed 's/^/    /'
  local rc=${PIPESTATUS[0]}; [ "$rc" -eq 0 ] || die "$label (rc=$rc)"; ts "✓ $label"; }

# stream_soft: como stream pero NO aborta si falla (un motor puede faltar; seguimos con el otro).
stream_soft(){ local label="$1"; shift; ts "▶ $label"; "$@" 2>&1 | sed 's/^/    /'
  local rc=${PIPESTATUS[0]}
  if [ "$rc" -eq 0 ]; then ts "✓ $label"; else ts "⚠ $label terminó con rc=$rc (continúo)"; fi; }

# --------------------------------------------------------------------------- #
main(){
  # ---- multi-tenant (opcional) ------------------------------------------- #
  # TENANT=<slug> aísla credenciales + reportes + caché por cliente. Sin TENANT: modo clásico.
  # OJO bash 3.2 (el de macOS, al que apunta `#!/usr/bin/env bash`): `local TENANT` SIN asignar
  # BORRA el valor heredado del entorno. Hay que capturarlo en la MISMA línea del `local`.
  local TENANT="${TENANT:-}"
  local TSLUG ENVFILE DEFAULT_NAME WIPE_ENGINE ART DELIV MAXHOSTS NAME k v
  ART="$HERE/artifacts"; DELIV="$HERE/deliverables"     # modo clásico (un tenant)
  ENVFILE="$HERE/.env"                                   # de dónde salen MAX_HOSTS/PACK_NAME
  DEFAULT_NAME="Ley 21.719 - Medidas de Seguridad"
  TSLUG=""
  WIPE_ENGINE=1                                          # clásico: limpia salida vieja (sin contaminación cruzada)

  if [ -n "$TENANT" ]; then
    TSLUG="$(slug_of "$TENANT")"
    [ -n "$TSLUG" ] || die "TENANT inválido: '$TENANT' (sin caracteres usables para un nombre de carpeta)."
    ART="$HERE/artifacts/$TSLUG"; DELIV="$HERE/deliverables/$TSLUG"
    ENVFILE="$HERE/.env.$TSLUG"
    DEFAULT_NAME="Ley 21.719 - $TENANT"
    WIPE_ENGINE=0                                        # tenant aislado -> conserva caché entre corridas
    if [ -f "$ENVFILE" ]; then
      ts "Tenant '$TSLUG' — credenciales desde $(basename "$ENVFILE")"
      # from_env() lee el entorno (precedencia máxima); exportamos las QUALYS_* de este env-file.
      for k in QUALYS_POD QUALYS_API_USER QUALYS_API_PASSWORD; do
        v="$(dotenv_get_from "$ENVFILE" "$k")"; [ -n "$v" ] && export "$k=$v"
      done
    else
      ts "Tenant '$TSLUG' — no existe $(basename "$ENVFILE"); uso credenciales del entorno/.env."
      ENVFILE="$HERE/.env"
    fi
  fi

  # precedencia: variable de entorno > env-file (del tenant o .env) > default
  MAXHOSTS="${MAX_HOSTS:-$(dotenv_get_from "$ENVFILE" MAX_HOSTS)}"; MAXHOSTS="${MAXHOSTS:-300}"
  NAME="${PACK_NAME:-$(dotenv_get_from "$ENVFILE" PACK_NAME)}";     NAME="${NAME:-$DEFAULT_NAME}"

  # ----------------------------------------------------------------------- #
  ts "qley21719 — pack Ley 21.719 (READ-ONLY) · max_hosts=$MAXHOSTS${TSLUG:+ · tenant=$TSLUG}"

  command -v python3 >/dev/null 2>&1 || die "python3 no está en el PATH."
  ts "Python: $(python3 --version 2>&1)"

  [ -x "$PY" ] || hb "Creando venv (.venv)" python3 -m venv "$VENV"
  hb "Instalando requirements" "$PY" -m pip install -q --disable-pip-version-check -r "$HERE/requirements.txt"

  # Credenciales: o están exportadas (incl. las del .env.<tenant> de arriba) o existe un .env que lee from_env().
  if [ -z "${QUALYS_API_USER:-}" ] && [ ! -f "$HERE/.env" ]; then
    die "Faltan credenciales. Crea .env (cp .env.example .env)${TSLUG:+ o .env.$TSLUG} o exporta QUALYS_POD/QUALYS_API_USER/QUALYS_API_PASSWORD."
  fi

  rm -rf "$DELIV"; mkdir -p "$DELIV"
  mkdir -p "$ART"
  # Clásico: limpia la salida de motores (la caché vive bajo el run-dir y se re-cosecha). Tenant: NO
  # limpia -> conserva la caché de harvest del cliente (re-runs más rápidos) y queda aislada por carpeta.
  [ "$WIPE_ENGINE" = 1 ] && rm -rf "$ART/tenant-pack" "$ART/cloud-pack"

  # --- módulos (POL/TC) — una sola probada, se muestra y se parsea --- #
  ts "▶ Verificando módulos (POL/TC)"
  local MODOUT POL TC
  MODOUT="$("$PY" -u scripts/check_modules.py --out "$DELIV/0-modulos.md" 2>&1)" \
    || { echo "$MODOUT" | sed 's/^/    /'; die "check_modules (¿credenciales válidas?)"; }
  echo "$MODOUT" | grep -v -E '^(POL|TC)=' | sed 's/^/    /'
  POL="$(echo "$MODOUT" | grep -E '^POL=' | tail -1 | cut -d= -f2)"
  TC="$(echo "$MODOUT" | grep -E '^TC=' | tail -1 | cut -d= -f2)"
  ts "✓ Módulos: POL=$POL · TC=$TC"

  # --- Policy Compliance (POL) -> policy.xml + faltantes --- #
  if [ "$POL" = "yes" ]; then
    stream_soft "Policy Compliance: barrido + policy.xml" \
      "$PY" -u scripts/tenant_coverage_pack.py --name "$NAME" --max-hosts "$MAXHOSTS" --out "$ART/tenant-pack"
    local RUN; RUN="$(latest_run "$ART/tenant-pack")"
    if [ -n "$RUN" ]; then
      consolidate_pol "$RUN" "$DELIV"
    else
      ts "⚠ Policy Compliance: no se encontró salida en $ART/tenant-pack (¿el motor falló?)."
    fi
  else
    ts "⚠ POL no disponible — se omite el pack Policy Compliance."
  fi

  # --- Cloud posture (TC) -> mapeo CSPM por cuenta --- #
  if [ "$TC" = "yes" ]; then
    stream_soft "Cloud posture (CSPM): auto-discovery + mapeo" \
      "$PY" -u scripts/cloud_posture_pack.py --provider all --out "$ART/cloud-pack"
    local RUNC; RUNC="$(latest_run_cspm "$ART/cloud-pack")"
    if [ -n "$RUNC" ]; then
      consolidate_cspm "$RUNC" "$DELIV"
    else
      ts "⚠ Cloud posture: no se encontró salida en $ART/cloud-pack."
    fi
  else
    ts "⚠ TC no disponible — se omite el pack cloud-posture."
  fi

  # --- índice --- #
  {
    echo "# Deliverables — Ley 21.719 (Qualys) — READ-ONLY"
    echo
    echo "Generado: $(date -u +%Y-%m-%dT%H:%M:%SZ) · POD según entorno/env-file · POL=$POL · TC=$TC${TSLUG:+ · tenant=$TSLUG}"
    echo
    echo "| # | Archivo | Qué es |"
    echo "|---|---|---|"
    echo "| 0 | \`0-modulos.md\` | Módulos POL/TC disponibles en el tenant (y si falta alguno). |"
    echo "| 1 | \`1-CIS-a-importar-en-POL.txt\` | Benchmarks CIS a cargar en Policy Compliance (Import from Library). |"
    echo "| 2 | \`2-policy-xml/{base,sensible}/policy.xml\` | La política importable de la Ley + \`import-instructions.md\`. |"
    echo "| 2 | \`2-policy-xml/subir.sh\` | Import como política NUEVA — lo corre el CLIENTE (human-gate). |"
    echo "| 2 | \`2-policy-xml/subir-merge.sh\` | Alternativa: merge in-place sobre una política Ley ya afinada (preview primero). |"
    echo "| 3 | \`3-cloud-posture-CSPM/<prov>/<cuenta>/\` | Mapeo de posture cloud (CSPM) por cuenta: mapping.csv, fails.csv, gaps.md. |"
    echo
    echo "**IMPORTANTE:** \`policy.xml\` lleva valores CIS endurecidos (contenido licenciado) y este"
    echo "folder datos del tenant -> NO se commitea (gitignored). El import lo ejecuta el cliente."
  } > "$DELIV/LEEME.md"

  ts "✓ LISTO. Deliverables en: ${DELIV#"$HERE"/}/"
  ( cd "$DELIV" && find . -type f | sort | sed 's/^/    /' )
}

# Permite sourcear las funciones para testear sin correr el orquestador completo.
[ "${QLEY_RUNSH_LIB:-}" = 1 ] || main "$@"
