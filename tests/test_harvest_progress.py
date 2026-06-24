#!/usr/bin/env python3
"""Unit tests (sin red) del LATIDO de progreso del harvest del Policy Pack.

Corre standalone:  .venv/bin/python tests/test_harvest_progress.py
Verifica que `_harvest` invoque el callback `progress` una vez por benchmark (en orden, ANTES de
exportarlo) para que el orquestador no deje 'aire muerto' durante la cosecha live — y que sin
callback siga funcionando (retrocompat). No toca la red: el cliente es un doble que devuelve XML.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import compliance_pack.generator as g  # noqa: E402

_POLICY_XML = (
    '<POLICY><CONTROL><ID>{cid}</ID>'
    '<TECHNOLOGIES total="1"><TECHNOLOGY><ID>1</ID></TECHNOLOGY></TECHNOLOGIES>'
    '</CONTROL></POLICY>'
)


class _FakeClient:
    """Doble read-only: `fo_get` devuelve un POLICY XML mínimo; graba las llamadas. Cero red."""
    server = "https://qualysapi.example"
    pod = "US03"

    def __init__(self):
        self.calls = []

    def fo_get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        return 200, _POLICY_XML.format(cid=params["id"])


def test_harvest_calls_progress_once_per_source_in_order():
    c = _FakeClient()
    sources = [{"id": "10", "label": "Bench A"}, {"id": "20", "label": "Bench B"}]
    seen = []
    controls, _prov = g._harvest(c, sources, progress=seen.append)
    assert len(seen) == 2, seen
    assert "1/2" in seen[0] and "Bench A" in seen[0]
    assert "2/2" in seen[1] and "Bench B" in seen[1]
    assert len(c.calls) == 2                     # una export por fuente
    assert set(controls.keys()) == {"10", "20"}  # cosechó ambos


def test_harvest_progress_is_optional():
    # Sin callback no debe fallar (retrocompat con llamadores existentes).
    c = _FakeClient()
    controls, _prov = g._harvest(c, [{"id": "1", "label": "X"}])
    assert "1" in controls
    assert len(c.calls) == 1


def test_harvest_progress_fires_before_export():
    # El latido se emite ANTES de pegarle al tenant: si el progress de la fuente i ya ocurrió,
    # debe haber a lo sumo i exports hechos (nunca el export antes del aviso).
    c = _FakeClient()
    order = []
    orig = g._export_policy

    def _spy(client, pid):
        order.append(("export", str(pid)))
        return orig(client, pid)

    g._export_policy = _spy
    try:
        g._harvest(c, [{"id": "7", "label": "B7"}],
                   progress=lambda m: order.append(("progress", m)))
    finally:
        g._export_policy = orig
    assert order[0][0] == "progress" and "B7" in order[0][1]
    assert order[1] == ("export", "7")


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e or 'assertion'}")
        except Exception as e:  # noqa: BLE001 — un error inesperado tambien es fallo
            failed += 1
            print(f"ERR   {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
