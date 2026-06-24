"""Utilidades de runtime compartidas por los orquestadores (todo READ-ONLY del tenant):

  - Layout de salida POR CORRIDA: `<base>/<slug>/<run_id>/` (o `<base>/<run_id>/` sin slug),
    con `run_id` = timestamp UTC ordenable, y un symlink `latest` -> última corrida.
  - Preflight de escritura: falla rápido ANTES de tocar el tenant si el destino no es escribible.
  - Run-log estructurado y SECRET-SAFE: una línea grep-friendly en UTC dentro del directorio de la
    corrida (bajo `artifacts/`, gitignored) -> nunca a un path versionado, nunca credenciales.

Sin dependencias nuevas (solo stdlib).
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_run_id() -> str:
    """Id de corrida ordenable lexicográficamente: 20260624T161800Z."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slugify(name: str) -> str:
    """Slug seguro para nombre de carpeta a partir de un título de cliente/policy."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (name or "").strip().lower()).strip("-")
    return s[:64] or "run"


def preflight_writable(path: str | Path) -> None:
    """Sube hasta el primer ancestro existente y verifica W_OK. Levanta PermissionError si no.
    Barato y temprano: evita barrer el tenant para recién fallar al escribir el artefacto."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        raise PermissionError(f"directorio de salida no escribible: {probe}")


def resolve_run_dir(base: str | Path, slug: str | None = None,
                    run_id: str | None = None) -> tuple[Path, str]:
    """Devuelve (run_dir, run_id). `<base>/<slug>/<run_id>` si hay slug, si no `<base>/<run_id>`.
    NO crea nada (el caller hace mkdir)."""
    rid = run_id or utc_run_id()
    parent = Path(base) / slug if slug else Path(base)
    return parent / rid, rid


def link_latest(run_dir: Path) -> None:
    """Mantiene `<run_dir>/../latest` -> nombre de la última corrida. Best-effort: si el FS no
    soporta symlinks (p.ej. Windows sin privilegios) no falla — el run_dir ya quedó escrito."""
    latest = run_dir.parent / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(run_dir.name)
    except (OSError, NotImplementedError):
        pass


class _RedactingFilter(logging.Filter):
    """Defensa en profundidad: jamás dejar pasar password/credenciales al run-log."""
    _PAT = re.compile(r"(?i)(password|api[_-]?password|authorization|secret|token)\s*[=:]\s*\S+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._PAT.sub(r"\1=***", record.msg)
        return True


def setup_run_log(run_dir: Path, name: str = "qley21719") -> logging.Logger:
    """Logger UTC, una línea por evento, a `<run_dir>/run.log`, con filtro de redacción.
    No propaga a root (evita doble salida). Idempotente: si ya tiene handler, lo reutiliza."""
    run_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"{name}.{run_dir.name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = time.gmtime  # timestamps en UTC
    fh = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(_RedactingFilter())
    logger.addHandler(fh)
    return logger
