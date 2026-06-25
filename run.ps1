<#
  run.ps1 - orquestador END-TO-END del pack Ley 21.719 (Qualys), READ-ONLY (PowerShell).

  Crea/usa el venv, instala requirements, verifica modulos (POL/TC), corre ambos motores y
  arma el folder `deliverables/`. NO muta el tenant (los scripts son read-only; el import lo
  corre el cliente con subir.sh). Monitoreo: banners con timestamp + salida en vivo de cada
  paso (pip y los motores imprimen progreso) -> nunca parece "stalled".

  ASCII-only a proposito: Windows PowerShell 5.1 lee un .ps1 SIN BOM con el codepage ANSI, y
  caracteres no-ASCII (p.ej. el guion largo) se corrompen y rompen el parseo. Mantener en ASCII.

  Uso (un tenant):
    Copy-Item .env.example .env   # completar QUALYS_POD/QUALYS_API_USER/QUALYS_API_PASSWORD
    ./run.ps1

  Uso (varios tenants, reportes SEPARADOS):
    Copy-Item .env.example .env.clienteA   # credenciales del cliente A (gitignored, igual que .env)
    Copy-Item .env.example .env.clienteB   # credenciales del cliente B
    $env:TENANT='clienteA'; ./run.ps1      # -> deliverables/clienteA/  + artifacts/clienteA/
    $env:TENANT='clienteB'; ./run.ps1      # -> deliverables/clienteB/  + artifacts/clienteB/
    Cada tenant aisla credenciales, reportes y cache de harvest; los demas NO se tocan.
    (Si no existe .env.<tenant>, cae a las credenciales del entorno/.env.)

  Variables opcionales:  $env:MAX_HOSTS=3000 ; $env:PACK_NAME="Ley 21.719 - <cliente>" ; $env:TENANT=<slug> ; ./run.ps1
#>
$ErrorActionPreference = 'Stop'

$Here  = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here
$Venv  = Join-Path $Here '.venv'

function Ts([string]$m){ Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }
function Die([string]$m){ Ts "X ERROR: $m"; exit 1 }

# lee una clave de un env-file (KEY=VALUE; ignora comillas/espacios/comentarios inline). $null si no esta.
function Get-DotEnvFrom([string]$file, [string]$key){
  if (-not (Test-Path $file)) { return $null }
  $m = Select-String -Path $file -Pattern ('^\s*' + [regex]::Escape($key) + '\s*=') | Select-Object -Last 1
  if (-not $m) { return $null }
  $v = ($m.Line -replace '^[^=]*=\s*','' -replace '\s+#.*$','').Trim().Trim("'").Trim('"')
  if ($v) { return $v } else { return $null }
}

# slug seguro para nombre de carpeta (mismo criterio que slugify() de la herramienta): sin
# path-traversal, minusculas, solo [a-z0-9._-], colapsa separadores, recorta a 64.
function Get-Slug([string]$s){
  $x = $s.ToLower()
  $x = [regex]::Replace($x, '[^a-z0-9._-]+', '-')
  $x = $x -replace '^[-.]+','' -replace '[-.]+$',''
  if ($x.Length -gt 64) { $x = $x.Substring(0,64) }
  return $x
}

# El motor PC escribe en <out>/<slug-name>/<run_id_UTC>/ (+ symlink latest). Devuelve la corrida
# MAS NUEVA (run_id UTC ordena cronologicamente). $null si no hay.
function Get-LatestRun([string]$base){
  if (-not (Test-Path $base)) { return $null }
  $d = Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue |
       ForEach-Object { Get-ChildItem -Path $_.FullName -Directory -ErrorAction SilentlyContinue } |
       Where-Object { $_.Name -match '^[0-9].*Z$' } |
       Sort-Object Name | Select-Object -Last 1
  if ($d) { return $d.FullName } else { return $null }
}

# El motor CSPM escribe en <out>/<run_id_UTC>/<prov>/<cuenta>/ (+ symlink latest).
function Get-LatestRunCspm([string]$base){
  if (-not (Test-Path $base)) { return $null }
  $d = Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue |
       Where-Object { $_.Name -match '^[0-9].*Z$' } |
       Sort-Object Name | Select-Object -Last 1
  if ($d) { return $d.FullName } else { return $null }
}

# Consolida la salida del motor PC (run_dir, anidada por slug-name/run_id) al folder deliverables.
function Copy-PolDeliv([string]$run, [string]$deliv){
  if (-not $run -or -not (Test-Path $run)) { return }
  $px = Join-Path $deliv '2-policy-xml'
  New-Item -ItemType Directory -Force -Path $px | Out-Null
  if (Test-Path (Join-Path $run 'faltantes.txt'))  { Copy-Item -Force (Join-Path $run 'faltantes.txt')  (Join-Path $deliv '1-CIS-a-importar-en-POL.txt') }
  if (Test-Path (Join-Path $run 'subir.sh'))        { Copy-Item -Force (Join-Path $run 'subir.sh')       (Join-Path $px 'subir.sh') }
  if (Test-Path (Join-Path $run 'subir-merge.sh'))  { Copy-Item -Force (Join-Path $run 'subir-merge.sh') (Join-Path $px 'subir-merge.sh') }
  if (Test-Path (Join-Path $run 'drift.md'))        { Copy-Item -Force (Join-Path $run 'drift.md')       (Join-Path $px 'drift.md') }
  foreach ($lvl in @('base','sensible')) {
    $src = Join-Path $run $lvl
    if (Test-Path $src) {
      $dst = Join-Path $px $lvl
      New-Item -ItemType Directory -Force -Path $dst | Out-Null
      foreach ($f in @('policy.xml','import-instructions.md','mapping.csv','gaps.md')) {
        if (Test-Path (Join-Path $src $f)) { Copy-Item -Force (Join-Path $src $f) (Join-Path $dst $f) }
      }
    }
  }
}

# Consolida la salida del motor CSPM (run_dir) al folder deliverables (sin el nivel run_id).
function Copy-CspmDeliv([string]$run, [string]$deliv){
  if (-not $run -or -not (Test-Path $run)) { return }
  $dst = Join-Path $deliv '3-cloud-posture-CSPM'
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  Copy-Item -Recurse -Force (Join-Path $run '*') $dst
}

# Stream: corre un scriptblock (comando externo) con salida en vivo. -Soft = no aborta si falla.
function Stream([string]$label, [scriptblock]$cmd, [switch]$Soft){
  Ts "> $label"
  & $cmd 2>&1 | ForEach-Object { Write-Host "    $_" }
  $rc = $LASTEXITCODE
  if ($rc -eq 0 -or $null -eq $rc) { Ts "OK $label" }
  elseif ($Soft) { Ts "! $label rc=$rc (continuo)" }
  else { Die "$label (rc=$rc)" }
}

# ---- multi-tenant (opcional) ------------------------------------------------ #
# $env:TENANT=<slug> aisla credenciales + reportes + cache por cliente. Sin TENANT: modo clasico.
$Tenant      = $env:TENANT
$Art         = Join-Path $Here 'artifacts'
$Deliv       = Join-Path $Here 'deliverables'
$EnvFile     = Join-Path $Here '.env'
$DefaultName = 'Ley 21.719 - Medidas de Seguridad'
$Tslug       = ''
$WipeEngine  = $true     # clasico: limpia salida vieja (sin contaminacion cruzada)

if ($Tenant) {
  $Tslug = Get-Slug $Tenant
  if (-not $Tslug) { Die "TENANT invalido: '$Tenant' (sin caracteres usables para un nombre de carpeta)." }
  $Art         = Join-Path (Join-Path $Here 'artifacts') $Tslug
  $Deliv       = Join-Path (Join-Path $Here 'deliverables') $Tslug
  $EnvFile     = Join-Path $Here ('.env.' + $Tslug)
  $DefaultName = "Ley 21.719 - $Tenant"
  $WipeEngine  = $false  # tenant aislado -> conserva cache entre corridas
  if (Test-Path $EnvFile) {
    Ts ("Tenant '{0}' - credenciales desde {1}" -f $Tslug, (Split-Path -Leaf $EnvFile))
    # from_env() lee el entorno (precedencia maxima); exportamos las QUALYS_* de este env-file.
    foreach ($k in @('QUALYS_POD','QUALYS_API_USER','QUALYS_API_PASSWORD')) {
      $val = Get-DotEnvFrom $EnvFile $k
      if ($val) { Set-Item -Path ("Env:" + $k) -Value $val }
    }
  } else {
    Ts ("Tenant '{0}' - no existe {1}; uso credenciales del entorno/.env." -f $Tslug, (Split-Path -Leaf $EnvFile))
    $EnvFile = Join-Path $Here '.env'
  }
}

# precedencia: variable de entorno > env-file (del tenant o .env) > default
$MaxHosts = $env:MAX_HOSTS; if (-not $MaxHosts) { $MaxHosts = Get-DotEnvFrom $EnvFile 'MAX_HOSTS' }; if (-not $MaxHosts) { $MaxHosts = '300' }
$Name     = $env:PACK_NAME; if (-not $Name)     { $Name     = Get-DotEnvFrom $EnvFile 'PACK_NAME' }; if (-not $Name)     { $Name = $DefaultName }

$onWin = ($env:OS -eq 'Windows_NT')
$Py = if ($onWin) { Join-Path $Venv 'Scripts\python.exe' } else { Join-Path $Venv 'bin/python' }

# --------------------------------------------------------------------------- #
$tenantNote = if ($Tslug) { " - tenant=$Tslug" } else { '' }
Ts "qley21719 - pack Ley 21.719 (READ-ONLY) - max_hosts=$MaxHosts$tenantNote"

$basePy = $null
foreach ($c in @('python3','python','py')) { if (Get-Command $c -ErrorAction SilentlyContinue) { $basePy = $c; break } }
if (-not $basePy) { Die 'No se encontro python (python3/python/py) en el PATH.' }
Ts ("Python base: {0}" -f $basePy)

if (-not (Test-Path $Py)) {
  $venvArgs = if ($basePy -eq 'py') { @('-3','-m','venv',$Venv) } else { @('-m','venv',$Venv) }
  Stream "Creando venv (.venv)" { & $basePy @venvArgs }
}
Stream "Instalando requirements" { & $Py -m pip install --disable-pip-version-check -r (Join-Path $Here 'requirements.txt') }

# Credenciales: o estan exportadas (incl. las del .env.<tenant> de arriba) o existe un .env que lee from_env().
if ((-not $env:QUALYS_API_USER) -and (-not (Test-Path (Join-Path $Here '.env')))) {
  $hint = if ($Tslug) { " o .env.$Tslug" } else { '' }
  Die "Faltan credenciales. Crea .env (Copy-Item .env.example .env)$hint o define `$env:QUALYS_POD/`$env:QUALYS_API_USER/`$env:QUALYS_API_PASSWORD."
}

if (Test-Path $Deliv) { Remove-Item -Recurse -Force $Deliv }
New-Item -ItemType Directory -Force -Path $Deliv | Out-Null
New-Item -ItemType Directory -Force -Path $Art | Out-Null
# Clasico: limpia la salida de motores (la cache vive bajo el run-dir y se re-cosecha). Tenant: NO
# limpia -> conserva la cache de harvest del cliente (re-runs mas rapidos) y queda aislada por carpeta.
if ($WipeEngine) {
  foreach ($d in @((Join-Path $Art 'tenant-pack'), (Join-Path $Art 'cloud-pack'))) {
    if (Test-Path $d) { Remove-Item -Recurse -Force $d }
  }
}

# --- modulos (POL/TC): una probada, se muestra y se parsea --- #
Ts "> Verificando modulos (POL/TC)"
$modout = & $Py -u scripts/check_modules.py --out (Join-Path $Deliv '0-modulos.md') 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { $modout | ForEach-Object { Write-Host "    $_" }; Die "check_modules (credenciales validas?)" }
$modout | Where-Object { $_ -notmatch '^(POL|TC)=' } | ForEach-Object { Write-Host "    $_" }
$POL = (($modout | Where-Object { $_ -match '^POL=' } | Select-Object -Last 1) -split '=')[1]
$TC  = (($modout | Where-Object { $_ -match '^TC='  } | Select-Object -Last 1) -split '=')[1]
Ts "OK Modulos: POL=$POL  TC=$TC"

# --- Policy Compliance (POL) --- #
if ($POL -eq 'yes') {
  Stream "Policy Compliance: barrido + policy.xml" {
    & $Py -u scripts/tenant_coverage_pack.py --name $Name --max-hosts $MaxHosts --out (Join-Path $Art 'tenant-pack')
  } -Soft
  $run = Get-LatestRun (Join-Path $Art 'tenant-pack')
  if ($run) { Copy-PolDeliv $run $Deliv } else { Ts "! Policy Compliance: no se encontro salida en (artifacts) (el motor fallo?)." }
} else { Ts "! POL no disponible - se omite el pack Policy Compliance." }

# --- Cloud posture (TC) --- #
if ($TC -eq 'yes') {
  Stream "Cloud posture (CSPM): auto-discovery + mapeo" {
    & $Py -u scripts/cloud_posture_pack.py --provider all --out (Join-Path $Art 'cloud-pack')
  } -Soft
  $runc = Get-LatestRunCspm (Join-Path $Art 'cloud-pack')
  if ($runc) { Copy-CspmDeliv $runc $Deliv } else { Ts "! Cloud posture: no se encontro salida en (artifacts)." }
} else { Ts "! TC no disponible - se omite el pack cloud-posture." }

# --- indice (here-string de comillas simples: |, *, backticks son literales) --- #
$now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$leeme = @'
# Deliverables - Ley 21.719 (Qualys) - READ-ONLY

Generado: __META__

| # | Archivo | Que es |
|---|---|---|
| 0 | `0-modulos.md` | Modulos POL/TC disponibles en el tenant (y si falta alguno). |
| 1 | `1-CIS-a-importar-en-POL.txt` | Benchmarks CIS a cargar en Policy Compliance (Import from Library). |
| 2 | `2-policy-xml/{base,sensible}/policy.xml` | La politica importable de la Ley + import-instructions.md. |
| 2 | `2-policy-xml/subir.sh` | Import como politica NUEVA - lo corre el CLIENTE (human-gate). |
| 2 | `2-policy-xml/subir-merge.sh` | Alternativa: merge in-place sobre una politica Ley ya afinada (preview primero). |
| 3 | `3-cloud-posture-CSPM/<prov>/<cuenta>/` | Mapeo de posture cloud (CSPM) por cuenta. |

**IMPORTANTE:** policy.xml lleva valores CIS endurecidos (contenido licenciado) y este folder
datos del tenant -> NO se commitea (gitignored). El import lo ejecuta el cliente.
'@
$metaLine = "$now  POL=$POL  TC=$TC"
if ($Tslug) { $metaLine = "$metaLine  tenant=$Tslug" }
$leeme = $leeme -replace '__META__', $metaLine
Set-Content -Path (Join-Path $Deliv 'LEEME.md') -Value $leeme -Encoding UTF8

Ts "OK LISTO. Deliverables en: $Deliv"
Get-ChildItem -Recurse -File $Deliv | ForEach-Object { Write-Host ("    " + $_.FullName.Substring($Deliv.Length+1)) }
