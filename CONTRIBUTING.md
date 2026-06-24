# Contribuir a qley21719

La rama `main` está **protegida**: los cambios entran **por Pull Request**, no por push directo.
Cada PR tiene que pasar la **CI en verde** (`tests (3.11)` y `tests (3.12)` — los 40 tests) y estar
**actualizado contra `main`** antes de poder mergear.

## Flujo de trabajo

```bash
# 1. rama desde main
git checkout main && git pull
git checkout -b feat/mi-cambio

# 2. editar, y correr los tests localmente (sin red, sin tenant)
for t in tests/test_*.py; do .venv/bin/python "$t"; done

# 3. commit + push + PR
git add -A && git commit -m "feat: ..."
git push -u origin feat/mi-cambio
gh pr create --fill

# 4. esperar la CI verde, y mergear (squash mantiene main lineal)
gh pr merge --squash --delete-branch
```

## Reglas de la protección (perfil "Medio")

- **Se exige PR** para cambiar `main` (0 aprobaciones — proyecto solo-dev, podés auto-mergear tu propio PR).
- **Se exigen los checks** `tests (3.11)` y `tests (3.12)` en verde.
- **`strict`**: la rama del PR debe estar al día con `main` antes de mergear.
- **El admin (dueño del repo) puede saltear** la protección si hace falta; el flujo por defecto es PR.

## Invariantes que NO se rompen (ver `CLAUDE.md` §3)

- **READ-ONLY**: el cliente Qualys solo permite `list/fetch/count/export` (FO) y GETs allow-list (CSPM).
  El `import` lo corre el cliente (human-gate), nunca la herramienta.
- **Nada de credenciales ni salida en git**: `.env`, `artifacts/`, `deliverables/`, `policy.xml` (contenido
  CIS licenciado) están gitignored. Verifica con `git status` antes de pushear.
- **Clean-room**: `qualys_client/` es implementación propia (sin código de terceros) — eso habilita Apache-2.0.
