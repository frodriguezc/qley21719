# Ley 21.719 Deploy Assistant (Qualys)

[![tests](https://github.com/frodriguezc/qley21719/actions/workflows/tests.yml/badge.svg)](https://github.com/frodriguezc/qley21719/actions/workflows/tests.yml)

> Asistente **autónomo y READ-ONLY** para desplegar los **controles técnicos** de la **Ley 21.719**
> de Chile (protección de datos personales) sobre una suscripción de **Qualys**. Evalúa tu tenant y
> arma, listo para subir, el mapeo de la ley a la plataforma. **No muta nada**: el import lo ejecutas tú.

Cubre los **dos motores** de Qualys que auditan configuración y postura:

- **Policy Compliance / Policy Audit (POL)** — *host-based*. Cosecha los benchmarks **CIS** que ya tienes
  importados y arma el **`policy.xml`** de la Ley 21.719 a medida de tu flota, + indica qué CIS te **faltan**.
- **TotalCloud / CloudView (CSPM)** — *cloud posture*. Mapea la postura de tus cuentas cloud (AWS/Azure/
  GCP/OCI) contra las familias de la ley (sin `policy.xml`: en cloud el control se aplica por la UI).

> **Sin agentes ni servicios externos.** Es Python puro que habla **directo a la API de Qualys**
> (solo `requests` + `pyyaml`). No usa Claude, MCP, ni nada propietario. Corre en tu entorno, con tus credenciales.

---

## Inicio rápido (un comando)

```bash
cp .env.example .env          # completa QUALYS_POD / QUALYS_API_USER / QUALYS_API_PASSWORD (read-only)
./run.sh                      # macOS / Linux
```
```powershell
Copy-Item .env.example .env   # completa las credenciales
./run.ps1                     # Windows (PowerShell 5.1+ o 7)
```

El script **crea el venv**, **instala los requirements**, **verifica qué módulos tienes** (POL/TC),
corre los motores que apliquen y deja todo en **`deliverables/`**. Muestra **monitoreo en vivo**
(banners con hora + progreso por paso) para que sepas que no está colgado.

Variables opcionales: `MAX_HOSTS=5000` (tope de hosts a barrer), `PACK_NAME="Ley 21.719 - Mi Empresa"`.

---

## Qué obtienes (`deliverables/`)

| # | Archivo | Qué es |
|---|---|---|
| 0 | `0-modulos.md` | Qué módulos tiene el tenant (**POL** / **TC**) y, si falta alguno, lo indica. |
| 1 | `1-CIS-a-importar-en-POL.txt` | Los **benchmarks CIS a cargar** en Policy Compliance: `[A]` por SO de la flota · `[B]` por software · `[C]` a verificar. |
| 2 | `2-policy-xml/{base,sensible}/policy.xml` | La **política importable** de la Ley, en **dos niveles** (ver abajo) + `import-instructions.md`. |
| 2 | `2-policy-xml/subir.sh` | El comando de import como **política nueva** — **lo corres tú** (human-gate). |
| 2 | `2-policy-xml/subir-merge.sh` | Alternativa: **merge in-place** sobre una política Ley ya afinada, con **preview** primero (no guarda nada) — **lo corres tú**. Sobrescribe tu tuning de los controles comunes; si solo querés sumar cobertura, usá `subir.sh`. |
| 2 | `2-policy-xml/drift.md` | (con `--drift`) Diff **read-only** entre la política Ley ya importada y el pack regenerado: qué CIDs faltan, cuáles sobran y cuáles cambiaron. |
| 3 | `3-cloud-posture-CSPM/<proveedor>/<cuenta>/` | Mapeo de postura cloud por cuenta: `mapping.csv` (control → familia → artículo, PASS/FAIL), `fails.csv`, `gaps.md`. |

> `deliverables/` lleva el `policy.xml` (valores CIS endurecidos = **contenido licenciado**) y datos del
> tenant → está **gitignored**: no se commitea. Es tu pack para subir a la plataforma.

### Los dos niveles: `base` vs `sensible` (Art. 14 septies)

La herramienta emite **dos** `policy.xml` con la **misma estructura** (las 5 familias de la ley,
los mismos controles CIS de tu flota). Lo único que cambia es el **piso de criticidad**: cuántos
controles entran según la severidad que Qualys le asigna a cada uno
(`0 UNDEFINED · 1 MINIMAL · 2 MEDIUM · 3 SERIOUS · 4 CRITICAL · 5 URGENT`).

| Nivel | Incluye | Para qué dato | Aplícalo a (scope) |
|---|---|---|---|
| **`base`** | solo controles **SERIOUS o más** (criticidad ≥ 3) | datos personales **generales** | tus asset tags/groups comunes |
| **`sensible`** | **todos** los controles (criticidad ≥ 0, suma los MEDIUM/MINIMAL) | **datos sensibles** (estándar reforzado) | los tags/groups que alojan datos sensibles |

`sensible` es un **superconjunto** de `base`: trae los mismos controles de alta severidad **más**
los de severidad media/baja. (En una corrida real: `base` ≈ 2.638 controles, `sensible` ≈ 2.852.)

**Ejemplo.** Una empresa con servidores de producción y una base de datos de RR.HH.:

```
Asset group "Servidores-Produccion"  (web/app, datos personales generales)
    └─►  importa  base/policy.xml      → exige los controles críticos

Asset group "BD-RRHH"  (remuneraciones, salud, datos sensibles)
    └─►  importa  sensible/policy.xml  → exige TODOS los controles (estándar reforzado)
```

Así, los sistemas con datos sensibles quedan bajo un estándar más estricto, como exige el
Art. 14 *septies* (estándares diferenciados según el tipo de dato).

> **Importante:** el piso usa la **criticidad del control en Qualys**, no una clasificación jurídica
> del dato — Qualys no sabe qué activo tiene datos sensibles. **Esa decisión la tomas tú** al elegir
> a qué asset tags/groups le asignas cada policy. Si no estás seguro, `sensible` es el más exigente.

---

## Garantía read-only

La herramienta **solo lee** (inventario, policies, export de benchmarks, controls/evaluations cloud).
Los clientes HTTP incluidos **bloquean estructuralmente** cualquier escritura: FO solo permite
`list/fetch/count/export`; CSPM es **allow-list-only** (solo GETs enumerados). **El import (la mutación)
lo ejecutas tú**, a sabiendas, por la UI o con `subir.sh`. La herramienta nunca modifica tu suscripción.

> **Recomendado: usa un API user con rol de solo lectura (Reader / mínimo privilegio).** La herramienta
> es read-only de todos modos (no puede mutar), pero conviene que la credencial también lo sea: así el
> propio permiso del usuario garantiza que no se puede tocar nada, sin depender del software. Solo
> necesita **acceso de lectura** a Policy Compliance, al inventario de assets y a TotalCloud/CloudView.

---

## Cómo funciona

Es **Python puro y sin estado**: lee tus credenciales del entorno, abre una sesión HTTP contra la
**API de Qualys** y encadena llamadas de **solo lectura** hasta producir los `deliverables/`. No hay
base de datos, ni demonio, ni servicio externo — cada corrida es independiente.

### 1. Credenciales y conexión

`qualys_client.from_env()` resuelve las credenciales con esta precedencia:

```
variables de entorno  →  .env (gitignored)  →  config.yaml (gitignored)
QUALYS_POD · QUALYS_API_USER · QUALYS_API_PASSWORD
```

El **POD** se traduce a la URL del API server (`US03 → https://qualysapi.qg3.apps.qualys.com`, etc.) y
se construye un `QualysClient` con **HTTP Basic Auth** + el header `X-Requested-With` que exige Qualys.
El password **nunca** se loguea.

### 2. Pipeline Policy Compliance (`scripts/tenant_coverage_pack.py`)

```
from_env() ─► QualysClient(POD, user, pass)
   │
   ├─ 1. Barrer flota      GET  /api/5.0/fo/asset/host/?action=list        (pagina por id_min)
   ├─ 2. Inferir software  POST /qps/rest/2.0/search/am/hostasset          (pagina por startFromId)
   ├─ 3. Listar policies   GET  /api/4.0/fo/compliance/policy/?action=list
   │
   ├─ 4. reconcile()       cruza flota + software + policies  vs  mapping/cis_catalog.yaml
   │
   └─ 5. generate_pack()   por cada benchmark CIS importado que aplica:
          ├─ harvest       GET /api/4.0/fo/compliance/policy/?action=export&id=<pid>   (CONTROL+EVALUATE)
          ├─ categorías    GET /api/4.0/fo/compliance/control/?action=list&ids=...
          ├─ classify      CID → familia legal de la Ley   (mapping/ley21719.yaml)
          ├─ assemble      arma el <POLICY> import-XML (5 familias · 2 niveles)
          ├─ validate      well-formed + estructura + cada control con EVALUATE
          └─ emit          policy.xml · faltantes.txt · subir.sh · mapping.csv · gaps.md
```

La salida va a `artifacts/<pack>/<cliente>/<run_id_UTC>/` (+ symlink `latest`), con un `run.log` de la
corrida (sin credenciales). **Nada se importa:** `subir.sh` / `subir-merge.sh` los corres tú (human-gate).

### 3. Pipeline Cloud / CSPM (`scripts/cloud_posture_pack.py`)

Motor separado, mismo patrón read-only, con `CloudViewClient` (REST JSON, **allow-list de solo GET**):

```
GET /cloudview-api/rest/v1/controls/metadata/list             metadata de controles CSPM
GET /cloudview-api/rest/v1/<aws|azure|gcp|oci>/connectors     auto-descubre cuentas
GET /cloudview-api/rest/v1/<prov>/evaluations/<cuenta>        postura PASS/FAIL (pagina estilo Spring)
   └─► classify por keywords (mapping/ley21719-cloud.yaml) ─► mapping.csv · fails.csv · gaps.md
```

### 4. Caché y resiliencia

- **Caché de harvest:** tras una corrida online, los controles cosechados quedan en `artifacts/.../cache/`
  (gitignored). Con `--refresh` re-cosecha; sin él lee del caché y **no toca el tenant** (generación offline).
- **Rate limiting:** ante `429/409` el cliente respeta el `X-RateLimit-ToWait-Sec` / `Retry-After` que pide
  Qualys (hasta 300 s) y reintenta; las listas grandes se **paginan** por cursor. Cada backoff queda en el
  `run.log` (UTC, sin credenciales) **aunque no uses `--debug`**; con `--debug` además sale a **stderr** en
  vivo (concurrency vs rate, headers, segundos de espera) — así un throttle largo ya no parece un cuelgue.
- **Progreso del harvest:** la cosecha live emite un **latido por benchmark** (`cosechando N/total: <CIS>`)
  a stdout y al `run.log`; exportar benchmarks grandes es la parte lenta del pack.
- **Guard read-only horneado:** FO solo acepta `list/fetch/count/export`; QPS solo `/search/` y `/count/`;
  CSPM solo los GET enumerados. Cualquier otra cosa levanta `QualysReadOnlyError` **antes** de tocar la red
  (ver `tests/test_readonly_guards.py`).

---

## Requisitos

- Python 3.8+ (los scripts crean el venv e instalan `requests` + `pyyaml`).
- Un **API user de Qualys** con acceso a Policy Compliance y/o TotalCloud (según qué quieras generar).
- Tu **POD** (p.ej. `US03`, `EU01`, …) — el de tu consola Qualys.

---

## Uso manual (avanzado)

El orquestador llama a estos scripts; también puedes correrlos por separado:

```bash
.venv/bin/python scripts/check_modules.py --out deliverables/0-modulos.md       # módulos POL/TC
.venv/bin/python scripts/tenant_coverage_pack.py --name "Ley 21.719 - Cliente"   # motor POL -> policy.xml + faltantes
.venv/bin/python scripts/cloud_posture_pack.py --provider all                    # motor CSPM (auto-descubre cuentas)
```

Flags útiles de POL: `--level base|sensible`, `--max-hosts N`, `--out DIR`, `--debug` (diagnóstico de
throttle/backoff a stderr; el backoff se loguea al `run.log` igual sin el flag). De CSPM:
`--provider aws|azure|gcp|oci`, `--account <id>`, `--fixture sample.json` (correr sin tenant).
Credenciales: por entorno, por `.env`, o por `--pod/--user/--password`.

---

## Cómo importar (lo haces tú — human-gate)

**POL, por UI (recomendado):** Policy Audit / PC > **Policies** > **New** > **Import from XML File** →
sube `2-policy-xml/sensible/policy.xml` (o `base/`). Luego **asigna el alcance** (asset tags/groups): sin
alcance no evalúa. Para sumar tecnologías de `1-CIS-a-importar-en-POL.txt`: impórtalas desde **Import from
Library** y **vuelve a correr** la herramienta (las detecta y las suma al `policy.xml`).

**POL, por API:** `bash deliverables/2-policy-xml/subir.sh` (necesita las credenciales en el entorno).

**Cloud (CSPM):** no hay `policy.xml`. Sigue el `apply-instructions.md` de cada cuenta: creas la Custom
Policy por la UI (`Policy > New`) asociando los controles del `mapping.csv` y asignas connectors/tags.

> **Clasificación cloud (agnóstica):** un control CSPM que no matchea ninguna familia específica cae en
> el catch-all `hardening` y queda **listado en `gaps.md`** para revisión — la herramienta no inventa
> familia. Es por diseño, no un bug: cuando CIS agrega controles nuevos, van a aparecer ahí. Para afinar
> el mapeo, agrega keywords a `mapping/ley21719-cloud.yaml` (sección `classification`) y vuelve a correr.

---

## Alcance (honesto)

Qualys **audita/verifica configuración, detecta** vulnerabilidades/exposición y **produce evidencia**.
**No cifra, no respalda, no recupera datos ni clasifica jurídicamente el dato.** Por eso el pack cubre el
**pilar técnico de seguridad** de la ley (deber de seguridad, confidencialidad, protección desde el diseño,
sustrato de detección/registro) y declara como **gaps** lo que no toca (cifrado a nivel de dato, backup/DR,
consentimiento, derechos del titular, etc.). Ver `gaps.md` y, para el mapeo Ley → plataforma completo, la
matriz `mapping/ley21719_platform_coverage_matrix.csv`.

---

## Licencia

**Apache License 2.0** — ver [`LICENSE`](LICENSE) y [`NOTICE`](NOTICE). Los clientes de la API de Qualys
incluidos son una implementación propia e independiente (sin código de terceros). "Qualys" y "CIS" son
marcas de sus respectivos titulares; se usan solo con fines de identificación.
