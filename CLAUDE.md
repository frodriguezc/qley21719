# qley21719 — Ley 21.719 Deploy Assistant

> Herramienta **autónoma** y **READ-ONLY** que ayuda a un cliente de **Qualys Policy Compliance /
> Policy Audit** a desplegar los **controles técnicos** de la **Ley 21.719** (CL, protección de datos
> personales) en su suscripción. Evalúa la flota, arma la política importable y dice qué benchmarks
> CIS faltan. **No importa ni muta nada**: el `import` lo ejecuta el cliente (human-gate).
>
> Sin dependencias de ningún agente/LLM/MCP: es Python puro que habla a la API de Qualys (`requests` +
> `pyyaml`). Idioma: instrucciones en español; nombres de tools/endpoints/campos en inglés.

## 1. Qué hace

`scripts/tenant_coverage_pack.py` (todo lectura):
1. Barre el inventario (`asset/host`) → consolida el **SO de la flota**.
2. Best-effort: infiere **DB/middleware** del software del inventario (CSAM); lo no detectable → "verificar".
3. Lista las **policies importadas** (`compliance/policy list`) y las cruza con el catálogo curado
   `mapping/cis_catalog.yaml` (tecnología → benchmark CIS).
4. Genera el **`policy.xml`** de la Ley 21.719 cosechando los benchmarks CIS ya importados que aplican.
5. Emite **`faltantes.txt`** (qué importar para cobertura completa) y **`subir.sh`** (el import, que
   corre el cliente).

## 2. Arquitectura

Dos motores READ-ONLY independientes (host-based PC + cloud-posture CSPM) + una capa de mapeo.

```
scripts/tenant_coverage_pack.py   [PC] orquesta: sweep -> reconcile -> generate -> emit (policy.xml)
scripts/cloud_posture_pack.py     [CSPM] orquesta: harvest controls -> resolve posture -> classify -> emit (mapping report; NO policy.xml)
scripts/reconcile.py              [PC+CSPM] vista por-artículo: joinea los dos mapping.csv por familia -> coverage-by-article.md (UNIÓN por sustrato, sin sumar entre planos). Read-only, sin tenant.
scripts/verify_tenant.py          [CSPM] sonda READ-ONLY del tenant (rol Reader / OCI live / CIDs); cierra los confirmables-live de §7. NINGUNA mutación (solo GETs).
  ├─ qualys_client/               clientes HTTP READ-ONLY — IMPLEMENTACIÓN PROPIA (sin código de terceros).
  │     client.py                 QualysClient: FO XML (api/2.0|4.0/fo) + QPS. Solo list/fetch/count/export + /search|/count.
  │     cloudview.py              CloudViewClient: CSPM REST (cloudview-api/rest/v1), gate ALLOW-LIST-ONLY (solo GET enumerados).
  ├─ compliance_pack/             [PC] generador del Policy XML: harvest -> classify -> assemble -> validate -> emit.
  ├─ cloud_pack/                  [CSPM] generador del mapping report: classify (keyword) -> emit (mapping.csv/fails/gaps/apply-instructions); + reconcile.py (vista PC+cloud por artículo).
  └─ mapping/
       ley21719.yaml              [PC] spec de la ley: familias, niveles (Art. 14 septies), clasificación agnóstica.
       cis_catalog.yaml           [PC] catálogo tecnología -> benchmark CIS. `targets` (PC) + `additional_domains` (otro motor, pc_importable:false).
       ley21719-cloud.yaml        [CSPM] mismas 5 familias -> área de control cloud + keywords de clasificación + ejemplos CIS por proveedor.
       build_coverage_matrix.py   genera ley21719_coverage_matrix.csv (PC+cloud: módulo -> benchmark -> familias -> ARTÍCULOS).
       build_platform_matrix.py   genera ley21719_platform_coverage_matrix.csv desde platform_coverage.yaml.
       platform_coverage.yaml     mapeo Ley -> TODOS los módulos Qualys (24 obligaciones × 12 módulos + grounding). Posicionamiento, NO contrato.
```
Las matrices CSV son DERIVADAS (no editar a mano: regenerar con su builder). Motor CSPM: la **auth
y la detección** se verificaron contra un tenant live (jun-2026) — va por el **API Gateway + JWT**
(`gateway.<pod>`, `POST /auth` → `Bearer`), NO Basic contra `qualysguard` (eso daba 401 en un tenant
real). El **harvest también se ejerció live** (US03, `scripts/verify_tenant.py`, credencial Manager):
`controls/metadata` + **AWS/GCP `evaluations` = 200**, con el core Evaluations devolviendo 168 controles
AWS y paginación Spring bajo `content`. El 401 de `controls/metadata` del primer intento era un permiso
de **control-library** que ESE API user no tenía (con el permiso/rol correcto da 200); parsers
defensivos. Residuales: **OCI no se ejerció live** (connectors OCI por Connector Mgmt 3.0; eval/reportes a
confirmar en un tenant con OCI onboardeado) y falta un run **Reader-scoped** (aún no hay API user Reader).
Ver DESIGN-cloud-posture.md §7.

## 3. Guardrails (INVARIANTES — no romper)

- **READ-ONLY.** El cliente solo permite acciones `list/fetch/count/export`; cualquier otra levanta
  `QualysReadOnlyError`. El **import** (`action=import`, una mutación) lo corre **el cliente**, nunca esta
  herramienta. Si se agrega capacidad de escritura, va detrás de confirmación explícita por acción.
- **Credenciales fuera del repo.** `QUALYS_POD/QUALYS_API_USER/QUALYS_API_PASSWORD` por entorno (o `.env`
  gitignored, o `config.yaml` gitignored). El password jamás se loguea.
- **Nada de contenido licenciado en git.** El `policy.xml` y el cache del harvest llevan los valores CIS
  endurecidos (contenido licenciado) → la salida va a `artifacts/` (gitignored). No commitear esos archivos.
- **Alcance honesto.** Qualys PC audita controles técnicos → el pack cubre solo el **pilar de seguridad** de
  la ley (deber de seguridad, confidencialidad, protección desde el diseño, sustrato de detección). Lo
  organizacional/legal va a `gaps.md`. AWS/Azure/GCP foundations = CloudView/TotalCloud (CSPM), no PC.
- **Match exacto de tecnología.** Importar un benchmark solo evalúa hosts con ese fingerprint; la versión del
  benchmark debe coincidir con la flota (ver `faltantes.txt`).

## 4. Setup y uso

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # solo requests + pyyaml
export QUALYS_POD=US03 QUALYS_API_USER=... QUALYS_API_PASSWORD=...    # API user de SOLO LECTURA
.venv/bin/python scripts/tenant_coverage_pack.py --name "Ley 21.719 - <cliente>"
```
Salida en `artifacts/tenant-pack/<cliente>/<run_id_UTC>/` (con symlink `…/<cliente>/latest`):
`{base,sensible}/policy.xml`, `faltantes.txt`, `subir.sh` (import como policy nueva), `subir-merge.sh`
(merge in-place con `preview_merge=1` primero — lo corre el cliente), `drift.md` (con `--drift`: diff
read-only vs la policy Ley ya importada), `run.log` (traza sin credenciales), trazabilidad.
El cliente importa (UI: Policies > New > Import from XML File, o `bash subir.sh`) y asigna **scope** (asset
tags/groups). Para sumar tecnologías: importar los benchmarks de `faltantes.txt` desde la librería y
**re-correr** la herramienta. Ver `README.md` (cara al cliente) y `compliance_pack/README.md`.

**Orquestadores `run.sh` / `run.ps1`.** Encadenan venv → módulos → ambos motores → consolidan en
`deliverables/`. **Consolidan leyendo la corrida REAL del motor** (`latest_run` resuelve el `run_id`
más nuevo bajo `tenant-pack/<slug-name>/` — NO rutas planas; un mismatch ahí dejaba `2-policy-xml/`
vacío en silencio). **Multi-tenant:** `TENANT=<slug>` aísla credenciales (`.env.<slug>`, exportado al
entorno → precedencia de `from_env`), reportes (`deliverables/<slug>/`, `artifacts/<slug>/`) y la
caché de harvest por cliente; sin `TENANT` es el modo clásico de un solo tenant (sí limpia la salida
vieja). El slug usa el mismo criterio que `slugify()` (sin path-traversal). Los `.env.*` están gitignored.

**Observabilidad de la corrida.** El `run.log` registra (UTC, secret-safe) cada **backoff** por throttle
(`429/409`) y el **latido del harvest** (`cosechando N/total`) — útil porque exportar benchmarks grandes es
la parte lenta y antes quedaba en silencio. Con `--debug`, el diagnóstico de throttle (concurrency vs rate,
headers de Qualys, segundos de espera) además sale a **stderr** en vivo. Nada de esto loguea credenciales.

## 5. Convenciones de cambios

- **Flujo de cambios (`main` protegida):** los cambios van **por Pull Request** (no push directo a `main`)
  y la **CI `tests` debe estar en verde** para mergear. Ver `CONTRIBUTING.md`. El admin puede saltear la
  protección, pero el flujo por defecto es PR.
- Mantener el **invariante read-only** al tocar `qualys_client/` (no agregar `fo_post`/acciones de escritura
  sin un gate explícito).
- El **catálogo** `cis_catalog.yaml` es el lugar para ampliar cobertura (no hardcodear en el script):
  - **PC** (host/DB/web/contenedores/red/virt) → agregar `targets` con `os_match`/`software_match`/`title_match`.
    Sí entran en el `policy.xml` de PC; los cosecha `tenant_coverage_pack.py`.
  - **Cloud posture / identidad** (AWS/Azure/GCP/OCI/Entra) → agregar a `additional_domains` (campos:
    `qualys_app`/`control_system`/`pc_importable`/`benchmark`/`cis_version`/`pillars`). Son **OTRO motor**
    (TotalCloud/CloudView CSPM): `pc_importable:false`, NO se cosechan en el `policy.xml` de PC (sí los
    lista `faltantes.txt` como "fuera del alcance PC") — su pack es un flujo aparte a construir. AD es la excepción (es PC, vía perfil DC del CIS
    Windows Server). Tras editar cualquiera de los dos planos, re-correr `python mapping/build_coverage_matrix.py`
    para regenerar la matriz CSV (derivada de los YAML — fuente única; no editar el CSV a mano).
- La **clasificación** ley→familia vive en `mapping/ley21719.yaml` (`families[].match` + `classification`);
  es agnóstica (un control nuevo cae en `default_family` y se lista en `gaps.md` para revisión).
- No commitear credenciales ni salida (`artifacts/`). Verifica con `git status` antes de pushear.

## 6. Licencia

**Apache License 2.0** (ver `LICENSE` y `NOTICE`). El cliente `qualys_client/` es una implementación
propia e independiente (sin código de terceros). "Qualys"/"CIS" son marcas de sus titulares; se usan
solo para identificación. Al contribuir, se entiende que el aporte va bajo Apache-2.0.
