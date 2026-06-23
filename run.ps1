<#
  run.ps1 — orquestador END-TO-END del pack Ley 21.719 (Qualys), READ-ONLY (PowerShell).

  Crea/usa el venv, instala requirements, verifica módulos (POL/TC), corre ambos motores y
  arma el folder `deliverables/`. NO muta el tenant (los scripts son read-only; el import lo
  corre el cliente con subir.sh). Monitoreo: banners con timestamp + salida en vivo de cada
  paso (pip y los motores imprimen progreso) -> nunca parece "stalled".

  Uso (PowerShell 5.1+ o PowerShell 7):
    Copy-Item .env.example .env   # completar QUALYS_POD/QUALYS_API_USER/QUALYS_API_PASSWORD
    ./run.ps1
  Variables opcionales:  $env:MAX_HOSTS=3000 ; $env:PACK_NAME="Ley 21.719 - <cliente>" ; ./run.ps1
#>
$ErrorActionPreference = 'Stop'

$Here  = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
$Venv  = Join-Path $Here '.venv'
$Art   = Join-Path $Here 'artifacts'
$Deliv = Join-Path $Here 'deliverables'
$MaxHosts = if ($env:MAX_HOSTS) { $env:MAX_HOSTS } else { '3000' }
$Name     = if ($env:PACK_NAME) { $env:PACK_NAME } else { 'Ley 21.719 - Medidas de Seguridad' }

$onWin = ($env:OS -eq 'Windows_NT')
$Py = if ($onWin) { Join-Path $Venv 'Scripts\python.exe' } else { Join-Path $Venv 'bin/python' }

function Ts([string]$m){ Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m){ Ts "X ERROR: $m"; exit 1 }

# Stream: corre un scriptblock (comando externo) con salida en vivo. -Soft = no aborta si falla.
function Stream([string]$label, [scriptblock]$cmd, [switch]$Soft){
  Ts "> $label"
  & $cmd 2>&1 | ForEach-Object { Write-Host "    $_" }
  $rc = $LASTEXITCODE
  if ($rc -eq 0 -or $null -eq $rc) { Ts "OK $label" }
  elseif ($Soft) { Ts "! $label rc=$rc (continuo)" }
  else { Die "$label (rc=$rc)" }
}

# ----------------------------------------------------------------------------- #
Ts "qley21719 — pack Ley 21.719 (READ-ONLY) · max_hosts=$MaxHosts"

$basePy = $null
foreach ($c in @('python3','python','py')) { if (Get-Command $c -ErrorAction SilentlyContinue) { $basePy = $c; break } }
if (-not $basePy) { Die 'No se encontró python (python3/python/py) en el PATH.' }
Ts ("Python base: {0}" -f $basePy)

if (-not (Test-Path $Py)) {
  $venvArgs = if ($basePy -eq 'py') { @('-3','-m','venv',$Venv) } else { @('-m','venv',$Venv) }
  Stream "Creando venv (.venv)" { & $basePy @venvArgs }
}
Stream "Instalando requirements" { & $Py -m pip install --disable-pip-version-check -r (Join-Path $Here 'requirements.txt') }

if ((-not (Test-Path (Join-Path $Here '.env'))) -and (-not $env:QUALYS_API_USER)) {
  Die "Faltan credenciales. Creá .env (Copy-Item .env.example .env) o definí `$env:QUALYS_POD/`$env:QUALYS_API_USER/`$env:QUALYS_API_PASSWORD."
}

if (Test-Path $Deliv) { Remove-Item -Recurse -Force $Deliv }
New-Item -ItemType Directory -Force -Path $Deliv | Out-Null

# --- módulos (POL/TC): una probada, se muestra y se parsea --- #
Ts "> Verificando módulos (POL/TC)"
$modout = & $Py -u scripts/check_modules.py --out (Join-Path $Deliv '0-modulos.md') 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { $modout | ForEach-Object { Write-Host "    $_" }; Die "check_modules (¿credenciales válidas?)" }
$modout | Where-Object { $_ -notmatch '^(POL|TC)=' } | ForEach-Object { Write-Host "    $_" }
$POL = (($modout | Where-Object { $_ -match '^POL=' } | Select-Object -Last 1) -split '=')[1]
$TC  = (($modout | Where-Object { $_ -match '^TC='  } | Select-Object -Last 1) -split '=')[1]
Ts "OK Módulos: POL=$POL · TC=$TC"

# --- Policy Compliance (POL) --- #
if ($POL -eq 'yes') {
  $tpDir = Join-Path $Art 'tenant-pack'
  if (Test-Path $tpDir) { Remove-Item -Recurse -Force $tpDir }   # limpia salida vieja (NO el cache)
  Stream "Policy Compliance: barrido + policy.xml" {
    & $Py -u scripts/tenant_coverage_pack.py --name $Name --max-hosts $MaxHosts --out (Join-Path $Art 'tenant-pack')
  } -Soft
  New-Item -ItemType Directory -Force -Path (Join-Path $Deliv '2-policy-xml') | Out-Null
  $tp = Join-Path $Art 'tenant-pack'
  if (Test-Path (Join-Path $tp 'faltantes.txt')) { Copy-Item -Force (Join-Path $tp 'faltantes.txt') (Join-Path $Deliv '1-CIS-a-importar-en-POL.txt') }
  if (Test-Path (Join-Path $tp 'subir.sh'))      { Copy-Item -Force (Join-Path $tp 'subir.sh') (Join-Path $Deliv '2-policy-xml/subir.sh') }
  foreach ($lvl in @('base','sensible')) {
    $src = Join-Path $tp $lvl
    if (Test-Path $src) {
      $dst = Join-Path $Deliv (Join-Path '2-policy-xml' $lvl)
      New-Item -ItemType Directory -Force -Path $dst | Out-Null
      foreach ($f in @('policy.xml','import-instructions.md','mapping.csv','gaps.md')) {
        if (Test-Path (Join-Path $src $f)) { Copy-Item -Force (Join-Path $src $f) (Join-Path $dst $f) }
      }
    }
  }
} else { Ts "! POL no disponible — se omite el pack Policy Compliance." }

# --- Cloud posture (TC) --- #
if ($TC -eq 'yes') {
  $cpDir = Join-Path $Art 'cloud-pack'
  if (Test-Path $cpDir) { Remove-Item -Recurse -Force $cpDir }   # limpia salida vieja
  Stream "Cloud posture (CSPM): auto-discovery + mapeo" {
    & $Py -u scripts/cloud_posture_pack.py --provider all --out (Join-Path $Art 'cloud-pack')
  } -Soft
  $cp = Join-Path $Art 'cloud-pack'
  if (Test-Path $cp) {
    $dst = Join-Path $Deliv '3-cloud-posture-CSPM'
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item -Recurse -Force (Join-Path $cp '*') $dst
  }
} else { Ts "! TC no disponible — se omite el pack cloud-posture." }

# --- índice --- #
$now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
@(
  "# Deliverables — Ley 21.719 (Qualys) — READ-ONLY"
  ""
  "Generado: $now · POL=$POL · TC=$TC"
  ""
  "| # | Archivo | Qué es |"
  "|---|---|---|"
  "| 0 | ``0-modulos.md`` | Módulos POL/TC disponibles en el tenant (y si falta alguno). |"
  "| 1 | ``1-CIS-a-importar-en-POL.txt`` | Benchmarks CIS a cargar en Policy Compliance (Import from Library). |"
  "| 2 | ``2-policy-xml/{base,sensible}/policy.xml`` | La política importable de la Ley + import-instructions.md. |"
  "| 2 | ``2-policy-xml/subir.sh`` | Comando de import (lo corre el CLIENTE — human-gate). |"
  "| 3 | ``3-cloud-posture-CSPM/<prov>/<cuenta>/`` | Mapeo de posture cloud (CSPM) por cuenta. |"
  ""
  "**IMPORTANTE:** policy.xml lleva valores CIS endurecidos (contenido licenciado) y este folder"
  "datos del tenant -> NO se commitea (gitignored). El import lo ejecuta el cliente."
) | Set-Content -Encoding UTF8 (Join-Path $Deliv 'LEEME.md')

Ts "OK LISTO. Deliverables en: deliverables/"
Get-ChildItem -Recurse -File $Deliv | ForEach-Object { Write-Host ("    " + $_.FullName.Substring($Deliv.Length+1)) }
