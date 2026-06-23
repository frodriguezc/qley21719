#!/usr/bin/env bash
# run.sh — orquestador END-TO-END del pack Ley 21.719 (Qualys), READ-ONLY.
#
# Crea/usa el venv, instala requirements, verifica módulos (POL/TC), corre ambos motores
# y arma el folder `deliverables/`. NO muta el tenant (los scripts son read-only; el import
# lo corre el cliente con subir.sh). Monitoreo: banners con timestamp + heartbeat (segundos)
# en pasos largos/silenciosos + salida en vivo de los motores -> nunca parece "stalled".
#
# Uso:
#   cp .env.example .env   # completar QUALYS_POD/QUALYS_API_USER/QUALYS_API_PASSWORD (read-only)
#   ./run.sh
# Variables opcionales:  MAX_HOSTS=3000  PACK_NAME="Ley 21.719 - <cliente>"  ./run.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
VENV="$HERE/.venv"; PY="$VENV/bin/python"
ART="$HERE/artifacts"; DELIV="$HERE/deliverables"
MAXHOSTS="${MAX_HOSTS:-3000}"
NAME="${PACK_NAME:-Ley 21.719 - Medidas de Seguridad}"

ts(){ printf '\033[2K\r[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die(){ ts "✗ ERROR: $*"; exit 1; }

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

# ----------------------------------------------------------------------------- #
ts "qley21719 — pack Ley 21.719 (READ-ONLY) · max_hosts=$MAXHOSTS"

command -v python3 >/dev/null 2>&1 || die "python3 no está en el PATH."
ts "Python: $(python3 --version 2>&1)"

[ -x "$PY" ] || hb "Creando venv (.venv)" python3 -m venv "$VENV"
hb "Instalando requirements" "$PY" -m pip install -q --disable-pip-version-check -r "$HERE/requirements.txt"

if [ ! -f "$HERE/.env" ] && [ -z "${QUALYS_API_USER:-}" ]; then
  die "Faltan credenciales. Creá .env (cp .env.example .env) o exportá QUALYS_POD/QUALYS_API_USER/QUALYS_API_PASSWORD."
fi

rm -rf "$DELIV"; mkdir -p "$DELIV"

# --- módulos (POL/TC) — una sola probada, se muestra y se parsea --- #
ts "▶ Verificando módulos (POL/TC)"
MODOUT="$("$PY" -u scripts/check_modules.py --out "$DELIV/0-modulos.md" 2>&1)" \
  || { echo "$MODOUT" | sed 's/^/    /'; die "check_modules (¿credenciales válidas?)"; }
echo "$MODOUT" | grep -v -E '^(POL|TC)=' | sed 's/^/    /'
POL="$(echo "$MODOUT" | grep -E '^POL=' | tail -1 | cut -d= -f2)"
TC="$(echo "$MODOUT" | grep -E '^TC=' | tail -1 | cut -d= -f2)"
ts "✓ Módulos: POL=$POL · TC=$TC"

# --- Policy Compliance (POL) -> policy.xml + faltantes --- #
if [ "$POL" = "yes" ]; then
  rm -rf "$ART/tenant-pack"   # limpia salida vieja (NO el cache: artifacts/cache/ acelera el harvest)
  stream_soft "Policy Compliance: barrido + policy.xml" \
    "$PY" -u scripts/tenant_coverage_pack.py --name "$NAME" --max-hosts "$MAXHOSTS" --out "$ART/tenant-pack"
  mkdir -p "$DELIV/2-policy-xml"
  [ -f "$ART/tenant-pack/faltantes.txt" ] && cp -f "$ART/tenant-pack/faltantes.txt" "$DELIV/1-CIS-a-importar-en-POL.txt"
  [ -f "$ART/tenant-pack/subir.sh" ] && cp -f "$ART/tenant-pack/subir.sh" "$DELIV/2-policy-xml/subir.sh"
  for lvl in base sensible; do
    [ -d "$ART/tenant-pack/$lvl" ] || continue
    mkdir -p "$DELIV/2-policy-xml/$lvl"
    for f in policy.xml import-instructions.md mapping.csv gaps.md; do
      [ -f "$ART/tenant-pack/$lvl/$f" ] && cp -f "$ART/tenant-pack/$lvl/$f" "$DELIV/2-policy-xml/$lvl/$f"
    done
  done
else
  ts "⚠ POL no disponible — se omite el pack Policy Compliance."
fi

# --- Cloud posture (TC) -> mapeo CSPM por cuenta --- #
if [ "$TC" = "yes" ]; then
  rm -rf "$ART/cloud-pack"   # limpia salida vieja (evita arrastrar packs de cuentas que ya no existen)
  stream_soft "Cloud posture (CSPM): auto-discovery + mapeo" \
    "$PY" -u scripts/cloud_posture_pack.py --provider all --out "$ART/cloud-pack"
  if [ -d "$ART/cloud-pack" ]; then
    mkdir -p "$DELIV/3-cloud-posture-CSPM"
    cp -R "$ART/cloud-pack/." "$DELIV/3-cloud-posture-CSPM/"
  fi
else
  ts "⚠ TC no disponible — se omite el pack cloud-posture."
fi

# --- índice --- #
{
  echo "# Deliverables — Ley 21.719 (Qualys) — READ-ONLY"
  echo
  echo "Generado: $(date -u +%Y-%m-%dT%H:%M:%SZ) · POD según .env · POL=$POL · TC=$TC"
  echo
  echo "| # | Archivo | Qué es |"
  echo "|---|---|---|"
  echo "| 0 | \`0-modulos.md\` | Módulos POL/TC disponibles en el tenant (y si falta alguno). |"
  echo "| 1 | \`1-CIS-a-importar-en-POL.txt\` | Benchmarks CIS a cargar en Policy Compliance (Import from Library). |"
  echo "| 2 | \`2-policy-xml/{base,sensible}/policy.xml\` | La política importable de la Ley + \`import-instructions.md\`. |"
  echo "| 2 | \`2-policy-xml/subir.sh\` | Comando de import (lo corre el CLIENTE — human-gate). |"
  echo "| 3 | \`3-cloud-posture-CSPM/<prov>/<cuenta>/\` | Mapeo de posture cloud (CSPM) por cuenta: mapping.csv, fails.csv, gaps.md. |"
  echo
  echo "**IMPORTANTE:** \`policy.xml\` lleva valores CIS endurecidos (contenido licenciado) y este"
  echo "folder datos del tenant -> NO se commitea (gitignored). El import lo ejecuta el cliente."
} > "$DELIV/LEEME.md"

ts "✓ LISTO. Deliverables en: deliverables/"
( cd "$DELIV" && find . -type f | sort | sed 's/^/    /' )
