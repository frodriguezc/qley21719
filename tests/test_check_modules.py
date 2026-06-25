"""Tests de check_modules.py: clasificación de estados (ok/absent/auth/error) y el aborto
fuerte ante un 401/403 (que NO debe disfrazarse de 'módulo faltante')."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scripts.check_modules as cm  # noqa: E402


class FakeFO:
    def __init__(self, code, text, pod="US03", server="https://qualysapi.qg3.apps.qualys.com"):
        self._r = (code, text)
        self.pod = pod
        self.server = server

    def fo_get(self, path, params):
        return self._r


class FakeCV:
    def __init__(self, code, text, server="https://gateway.qg3.apps.qualys.com"):
        self._r = (code, text)
        self.server = server

    def list_connectors(self, provider, params=None):
        return self._r

    def list_controls(self, params=None):
        return self._r


def test_probe_pol_states():
    assert cm._probe_pol(FakeFO(200, "<POLICY></POLICY>"))[0] == "ok"
    assert cm._probe_pol(FakeFO(401, "<SIMPLE_RETURN/>"))[0] == "auth"   # 401 != ausencia
    assert cm._probe_pol(FakeFO(403, "x"))[0] == "auth"
    assert cm._probe_pol(FakeFO(200, "... not subscribed ..."))[0] == "absent"
    assert cm._probe_pol(FakeFO(500, "boom"))[0] == "error"


def test_probe_tc_states():
    assert cm._probe_tc(FakeCV(200, "{}"))[0] == "ok"
    assert cm._probe_tc(FakeCV(401, "{}"))[0] == "auth"
    assert cm._probe_tc(FakeCV(403, "{}"))[0] == "auth"
    assert cm._probe_tc(FakeCV(500, "boom"))[0] == "error"


def test_main_auth_aborts(tmp_path, monkeypatch):
    """401 en ambos -> aborta (rc!=0), el reporte explica auth (no 'falta')."""
    monkeypatch.setattr(cm, "from_env", lambda: FakeFO(401, "<SIMPLE_RETURN/>"))
    monkeypatch.setattr(cm, "cv_from_env", lambda server=None: FakeCV(401, "{}"))
    out = tmp_path / "0-modulos.md"
    rc = cm.main(["--out", str(out)])
    assert rc != 0
    txt = out.read_text()
    assert "Autenticación rechazada" in txt
    assert "🔐" in txt


def test_main_pol_ok_tc_auth_does_not_abort(tmp_path, monkeypatch, capsys):
    """POL ok + TC auth -> NO aborta: el pack POL sigue siendo útil. POL=yes, TC=no."""
    monkeypatch.setattr(cm, "from_env", lambda: FakeFO(200, "<POLICY></POLICY>"))
    monkeypatch.setattr(cm, "cv_from_env", lambda server=None: FakeCV(401, "{}"))
    rc = cm.main(["--out", str(tmp_path / "m.md")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "POL=yes" in out
    assert "TC=no" in out


def test_main_both_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cm, "from_env", lambda: FakeFO(200, "<POLICY></POLICY>"))
    monkeypatch.setattr(cm, "cv_from_env", lambda server=None: FakeCV(200, "{}"))
    rc = cm.main(["--out", str(tmp_path / "m.md")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "POL=yes" in out and "TC=yes" in out
