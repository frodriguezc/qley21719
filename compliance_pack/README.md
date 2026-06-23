# compliance_pack — generador del Policy XML (READ-ONLY)

Convierte una ley (mapeada en un spec YAML, p.ej. `mapping/ley21719.yaml`) en un **Policy XML
importable** de Qualys Policy Compliance, **cosechando** los controles de las policies de librería
CIS que el tenant ya tiene importadas. No muta el tenant: solo **lee** (export de policies + catálogo
de controles) vía `qualys_client` y **emite archivos**. El `import` lo corre el cliente (human-gate),
fuera de la herramienta.

Normalmente no se invoca solo: lo orquesta [`scripts/tenant_coverage_pack.py`](../scripts/tenant_coverage_pack.py),
que primero barre la flota y resuelve qué benchmarks aplican. Este paquete es el paso `generate`.

## Pipeline

```
harvest         export de las policies fuente (compliance/policy action=export) -> controles + provenance
  -> resolve    CID -> CATEGORY / SUB_CATEGORY / statement (compliance/control action=list), cacheado
  -> classify   CID -> familia legal (_classify: SUB-first -> CATEGORY -> default_family) — ver abajo
  -> assemble   arma el <POLICY> import-XML (preserva EVALUATE verbatim; CDATA fiel al export)
  -> validate   chequea que el XML cierre y que los CID esperados estén presentes
  -> emit        un subdirectorio por nivel (`levels:` del spec)
```

## API

```python
from compliance_pack import generate_pack

generate_pack(spec_path, out_dir, client=None, source_ids=None,
              level=None, offline=False, refresh=False, ui_safe=False) -> dict
```

- `client` — un `qualys_client.QualysClient` (read-only). Si el cache cubre todo, puede correr `offline=True` sin tenant.
- `source_ids` — IDs de las policies de librería a cosechar (los resuelve el orquestador desde el catálogo).
- `level` — genera un solo nivel; vacío = todos los de `levels:` del spec (Art. 14 septies: `base` / `sensible`).
- `refresh` — fuerza re-cosecha live ignorando el cache.

## Salida (por nivel, en `<out>/<nivel>/`)

| Archivo | Qué es |
|---|---|
| `policy.xml` | la política importable (referencia controles de librería por CID) |
| `mapping.csv` | trazabilidad CID → familia → artículos de la ley, criticidad, ruta de clasificación |
| `gaps.md` | lo NO cubierto: controles sin clasificar y los asignados por `default_family` (**REVISAR**) |
| `import-instructions.md` | cómo importar el `policy.xml` (UI o API), con el server del tenant |

## Clasificación (`_classify`)

CID → `(family_id | None, route)`. Orden: **(1) SUB_CATEGORY** (parte `Database Settings` en su pilar),
**(2) CATEGORY** (backbone), **(3)** fallback `classification.default_family`. Rutas de trazabilidad:
`sub` · `category` · `default` (cayó al fallback → se lista en `gaps.md`) · `excluded` (SUB en
`exclude_sub_categories`, descarte intencional) · `unmatched` · `no_meta`. Es **agnóstica**: un control
nuevo que no matchea ninguna regla cae en `default_family` y queda visible en `gaps.md` para revisión —
no se inventa familia. Las reglas viven en `mapping/ley21719.yaml` (`families[].match` + `classification`),
no en el código. Cubierto por [`tests/test_classify.py`](../tests/test_classify.py).

## Invariantes

- **READ-ONLY.** Todo lo que toca el tenant es lectura (`action=export` / `action=list`). El cliente
  `qualys_client` bloquea estructuralmente cualquier escritura.
- **Contenido licenciado fuera de git.** El `policy.xml` y el **cache del harvest** (`<out>/../cache/`,
  gitignored) llevan los valores CIS endurecidos (contenido licenciado) → la salida va a `artifacts/`
  (gitignored). No commitear esos archivos. El cache permite regenerar offline sin re-exportar ni que
  las policies sigan importadas.
