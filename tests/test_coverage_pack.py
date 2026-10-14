#!/usr/bin/env python3
"""Tests del coverage pack (scripts/tenant_coverage_pack.py): el split de SO por autenticación
(point 1). El SO de un host es CONFIABLE solo si está autenticado (Cloud Agent o auth scan); el
resto es un fingerprint remoto AMBIGUO que NO debe disparar una recomendación dura de import.

Sin red ni pytest: FakeClient devuelve XML canónico de FO asset/host. Usa el catálogo REAL
(mapping/cis_catalog.yaml). Corre standalone (como en CI) y bajo pytest.
"""
import sys
import tempfile
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import scripts.tenant_coverage_pack as tcp  # noqa: E402

CATALOG = yaml.safe_load((ROOT / "mapping" / "cis_catalog.yaml").read_text())


def _host(hid, track, os_txt=None, auth_date=None):
    parts = [f"<ID>{hid}</ID>", f"<TRACKING_METHOD>{track}</TRACKING_METHOD>"]
    if os_txt is not None:
        parts.append(f"<OS>{os_txt}</OS>")
    if auth_date:
        parts.append(f"<LAST_VM_AUTH_SCANNED_DATE>{auth_date}</LAST_VM_AUTH_SCANNED_DATE>")
    return f"<HOST>{''.join(parts)}</HOST>"


_PAGE = (
    "<HOST_LIST_OUTPUT><RESPONSE><HOST_LIST>"
    + _host(1, "Cloud Agent", "Oracle Enterprise Linux 8.10")
    + _host(2, "Cloud Agent", "Oracle Enterprise Linux 8.10")
    + _host(3, "IP", "Windows Server 2019 Standard", auth_date="2026-06-26T13:00:00Z")  # IP pero auth
    + _host(4, "Cloud Agent", None)                       # auth sin OS
    + _host(5, "IP", "Ubuntu/Linux")                      # unauth ambiguo
    + _host(6, "IP", "Ubuntu/Linux")
    + _host(7, "IP", "EulerOS / Ubuntu / Fedora / Linux 3.x")
    + _host(8, "DNS", None)                               # unauth sin OS
    + "</HOST_LIST></RESPONSE></HOST_LIST_OUTPUT>"
)


class FakeClient:
    """Devuelve una sola página (sin WARNING -> _fleet_os corta)."""
    def __init__(self, page=_PAGE):
        self._page = page
        self.calls = 0

    def fo_get(self, path, params=None):
        self.calls += 1
        return 200, self._page


# ------------------------------------------------------------ _is_authenticated

def test_is_authenticated_cloud_agent():
    assert tcp._is_authenticated("Cloud Agent", "") is True
    assert tcp._is_authenticated("cloud agent", "") is True   # case-insensitive


def test_is_authenticated_by_auth_scan_date():
    assert tcp._is_authenticated("IP", "2026-06-26T13:00:00Z") is True


def test_is_authenticated_unauth_ip_dns():
    assert tcp._is_authenticated("IP", "") is False
    assert tcp._is_authenticated("DNS", "") is False
    assert tcp._is_authenticated("", "") is False


# ------------------------------------------------------------------- _fleet_os

def test_fleet_os_splits_auth_unauth():
    fleet = tcp._fleet_os(FakeClient(), max_hosts=1000)
    assert fleet["total"] == 8
    assert fleet["n_auth"] == 4 and fleet["n_unauth"] == 4
    # SO confiable: 2 oracle (agent) + 1 winserver (IP pero auth scan)
    assert fleet["os_auth"]["Oracle Enterprise Linux 8.10"] == 2
    assert fleet["os_auth"]["Windows Server 2019 Standard"] == 1
    # SO ambiguo: 2 ubuntu + 1 euleros (todos IP sin auth)
    assert fleet["os_unauth"]["Ubuntu/Linux"] == 2
    assert fleet["os_unauth"]["EulerOS / Ubuntu / Fedora / Linux 3.x"] == 1
    # los slash-ambiguos NUNCA caen en os_auth
    assert not any("/" in o for o in fleet["os_auth"])
    assert fleet["no_os_auth"] == 1 and fleet["no_os_unauth"] == 1


# ------------------------------------------------------------------- reconcile

def test_reconcile_presence_from_auth_only():
    os_auth = Counter({"Oracle Enterprise Linux 8.10": 144, "Windows Server 2019 Standard": 14})
    os_unauth = Counter({"Ubuntu/Linux": 11, "EulerOS / Ubuntu / Fedora": 7, "CentOS": 2})
    rec = tcp.reconcile(CATALOG, os_auth, os_unauth, set(), [])
    by_key = {r["key"]: r for r in rec["rows"]}

    # Oracle Linux: confiable -> presente, dispara [A]
    assert by_key["oracle_linux"]["present_in_fleet"] is True
    assert by_key["oracle_linux"]["fleet_hosts"] == 144
    assert by_key["oracle_linux"]["fleet_hosts_unauth"] == 0

    # Ubuntu: SOLO en hosts sin auth -> NO presente (no [A]); cuenta como posible [A']
    assert by_key["ubuntu"]["present_in_fleet"] is False
    assert by_key["ubuntu"]["fleet_hosts"] == 0
    assert by_key["ubuntu"]["fleet_hosts_unauth"] == 18   # 11 + 7 (ambos matchean 'ubuntu')

    # CentOS: idem, solo unauth
    assert by_key["centos"]["present_in_fleet"] is False
    assert by_key["centos"]["fleet_hosts_unauth"] == 2

    # Windows Server 2019: confiable -> presente
    assert by_key["win_server_2019"]["present_in_fleet"] is True
    assert by_key["win_server_2019"]["fleet_hosts"] == 14


# -------------------------------------------------------------- _write_faltantes

def _section(text, start, end):
    a = text.index(start)
    b = text.index(end, a)
    return text[a:b]


def test_write_faltantes_splits_sections_and_routes_unauth_to_possibles():
    os_auth = Counter({"Oracle Enterprise Linux 8.10": 144})
    os_unauth = Counter({"Ubuntu/Linux": 11, "EulerOS / Ubuntu / Fedora": 7})
    fleet = {"total": 162, "os_auth": os_auth, "os_unauth": os_unauth,
             "n_auth": 144, "n_unauth": 18, "no_os_auth": 0, "no_os_unauth": 0}
    rec = tcp.reconcile(CATALOG, os_auth, os_unauth, set(), [])
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "faltantes.txt"
        tcp._write_faltantes(out, CATALOG, rec, fleet, False, [], "US03", "Ley 21.719 - X", "NOW")
        text = out.read_text(encoding="utf-8")

    # los dos bloques de SO + el callout de no-auth
    assert "SO CONFIABLE" in text and "Oracle Enterprise Linux 8.10" in text
    assert "SO AMBIGUO" in text and "Ubuntu/Linux" in text
    assert "18 hosts SIN autenticar" in text

    # [A] sale del SO confiable -> Oracle SÍ (con versión EXACTA), Ubuntu NO (Ubuntu bogus no es import duro)
    a_block = _section(text, "[A] Detectados", "[A'] Posibles")
    assert "CIS Oracle Linux 8 Benchmark" in a_block          # point 2: versión exacta, no genérico
    assert "CIS Ubuntu Linux LTS Benchmark" not in a_block

    # [A'] son los posibles vistos solo sin auth -> Ubuntu SÍ (sin versión parseable -> nombre genérico)
    ap_block = _section(text, "[A'] Posibles", "[B] Detectados")
    assert "CIS Ubuntu Linux LTS Benchmark" in ap_block


# ----------------------------------------------------------- point 2: versión exacta

def test_extract_ver():
    assert tcp._extract_ver(r'oracle (?:enterprise )?linux\s+(\d+)', "Oracle Enterprise Linux 8.10") == "8"
    assert tcp._extract_ver(r'ubuntu\s+(\d+\.\d+)', "Ubuntu 22.04.3 LTS") == "22.04"
    assert tcp._extract_ver(r'ubuntu\s+(\d+\.\d+)', "Ubuntu/Linux") is None   # SO ambiguo -> sin versión
    assert tcp._extract_ver(None, "x") is None


def test_bench_name_versioned_vs_generic():
    row = {"benchmark": "CIS Oracle Linux Benchmark (versión = la de la flota)",
           "benchmark_versioned": "CIS Oracle Linux {ver} Benchmark"}
    assert tcp._bench_name(row, "8") == "CIS Oracle Linux 8 Benchmark"
    assert tcp._bench_name(row, None) == "CIS Oracle Linux Benchmark (versión = la de la flota)"
    assert tcp._bench_name({"benchmark": "X"}, "9") == "X"   # target sin plantilla


def test_reconcile_version_aware_imported_coverage():
    # OL8 (144) + OL9 (22) + OL10 (1) autenticados; OL8 y OL9 YA importados, OL10 NO.
    os_auth = Counter({"Oracle Enterprise Linux 8.10": 144,
                       "Oracle Enterprise Linux 9.8": 22,
                       "Oracle Enterprise Linux 10.1": 1})
    policies = [("1", "CIS Benchmark for Oracle Linux 8, v4.0.0 [Level 1 and Level 2] v.3.0"),
                ("2", "CIS Benchmark for Oracle Linux 9, v2.0.0 [Level 1 and Level 2] v.9.0")]
    rec = tcp.reconcile(CATALOG, os_auth, Counter(), set(), policies)
    r = next(x for x in rec["rows"] if x["key"] == "oracle_linux")
    assert r["versioned"] is True
    assert r["versions_auth"] == {"8": 144, "9": 22, "10": 1}
    assert r["imported_versions"] == {"8", "9"}        # versión extraída del TITLE de la policy
    # [A]: SOLO OL10 (las importadas OL8/OL9 NO tapan el host OL10)
    a = "\n".join(tcp._os_missing_auth(rec["rows"]))
    assert "CIS Oracle Linux 10 Benchmark" in a
    assert "CIS Oracle Linux 8 Benchmark" not in a
    assert "CIS Oracle Linux 9 Benchmark" not in a


def test_possible_unauth_versioned_and_nover_fallback():
    # versión parseable sin auth -> [A'] con versión exacta; SO ambiguo -> fallback genérico
    os_unauth = Counter({"Ubuntu 22.04.3 LTS": 5, "Ubuntu/Linux": 11})
    rec = tcp.reconcile(CATALOG, Counter(), os_unauth, set(), [])
    ap = "\n".join(tcp._os_possible_unauth(rec["rows"]))
    assert "CIS Ubuntu Linux 22.04 LTS Benchmark" in ap   # versión parseada
    assert "CIS Ubuntu Linux LTS Benchmark" in ap          # los "Ubuntu/Linux" sin versión -> genérico


# ------------------------------------------------- software: name+version + GAV fallback

class FakeQps:
    """Cliente QPS fake: responde por path EXACTO (hostasset=CSAM, asset=GAV)."""
    def __init__(self, by_path):
        self._by = by_path
        self.seen = []

    def qps_search(self, path, limit=None, start_from_id=None):
        self.seen.append(path)
        return (200, self._by[path]) if path in self._by else (404, "<x/>")


_HOSTASSET_XML = (
    "<ServiceResponse><data>"
    "<HostAsset><softwareList>"
    "<HostAssetSoftware><name>Microsoft SQL Server 2019</name><version>15.0.2000</version></HostAssetSoftware>"
    "<HostAssetSoftware><name>PostgreSQL</name><version>14.2</version></HostAssetSoftware>"
    "</softwareList></HostAsset>"
    "</data><hasMoreRecords>false</hasMoreRecords></ServiceResponse>"
)
_EMPTY_XML = "<ServiceResponse><hasMoreRecords>false</hasMoreRecords></ServiceResponse>"
_ASSET_XML = (
    "<ServiceResponse><data>"
    "<Asset><softwareListData>"
    "<SoftwareAssetSoftware><name>MariaDB</name><version>10.6</version></SoftwareAssetSoftware>"
    "</softwareListData></Asset>"
    "</data><hasMoreRecords>false</hasMoreRecords></ServiceResponse>"
)
_CSAM = "/qps/rest/2.0/search/am/hostasset"
_GAV = "/qps/rest/2.0/search/am/asset"


def test_collect_software_pairs_name_and_version():
    c = FakeQps({_CSAM: _HOSTASSET_XML})
    got = tcp._collect_software(c, _CSAM, "hostasset", 1000, 500)
    assert "microsoft sql server 2019 15.0.2000" in got    # name + version pareados
    assert "postgresql 14.2" in got


def test_fleet_software_falls_back_to_gav_when_csam_empty():
    c = FakeQps({_CSAM: _EMPTY_XML, _GAV: _ASSET_XML})
    got = tcp._fleet_software(c, 1000, 500)
    assert "mariadb 10.6" in got                            # vino de GAV
    assert any("hostasset" in p for p in c.seen)            # intentó CSAM primero
    assert any(p.endswith("/am/asset") for p in c.seen)     # y cayó a GAV


def test_fleet_software_prefers_csam_when_available():
    c = FakeQps({_CSAM: _HOSTASSET_XML, _GAV: _ASSET_XML})
    got = tcp._fleet_software(c, 1000, 500)
    assert "postgresql 14.2" in got and "mariadb 10.6" not in got   # usó CSAM, no tocó GAV
    assert not any(p.endswith("/am/asset") for p in c.seen)


def test_reconcile_software_evidence_for_B():
    sw = {"microsoft sql server 2019 15.0.2000", "postgresql database 16.15"}
    rec = tcp.reconcile(CATALOG, Counter(), Counter(), sw, [])
    by_key = {r["key"]: r for r in rec["rows"]}
    assert by_key["mssql"]["sw_hit"] is True
    assert "microsoft sql server 2019 15.0.2000" in by_key["mssql"]["sw_matches"]
    assert by_key["postgres"]["sw_matches"] == ["postgresql database 16.15"]


# --- servidor vs cliente: cada caso es un nombre REAL del inventario CSAM de un tenant (2026-10).
# Un benchmark de DB evalúa la INSTANCIA; recomendarlo por un cliente manda al DBA a buscar bases
# que no existen (y lo contrario la deja sin evaluar).

def _db(sw):
    rec = tcp.reconcile(CATALOG, Counter(), Counter(), set(sw), [])
    return {r["key"]: r for r in rec["rows"]}


def test_oracle_preinstall_e_instantclient_no_son_la_base():
    """Antes 'oracle.*\\d+c\\b' tomaba el preinstall-19c como Oracle DB."""
    r = _db(["oracle-database-preinstall-19c 1.0-4.el8.x86_64",
             "oracle-database-preinstall-23ai 1.0-4.el8.x86_64",
             "oracle-instantclient19.25-sqlplus 19.25.0.0.0-1.x86_64",
             "oracle client for microsoft tools (64bit) 19.31"])["oracle_db"]
    assert r["sw_hit"] is False and r["sw_matches"] == []
    assert r["sw_clients"], "los clientes se informan en [C] como contexto"


def test_oracle_motor_normalizado_por_csam_si_es_servidor():
    """CSAM normaliza el motor como 'oracle 19 19.0.0.0.0': antes NO lo detectaba."""
    r = _db(["oracle 19 19.0.0.0.0"])["oracle_db"]
    assert r["sw_hit"] is True and r["sw_matches"] == ["oracle 19 19.0.0.0.0"]


def test_oracle_no_confunde_paquetes_del_so():
    r = _db(["oracle-cloud-agent 1.64.0-16.el8.x86_64", "oracle-logos 84.5-1.0.2.el8.x86_64",
             "oracle-release-el8 1.0-3.el8.x86_64"])["oracle_db"]
    assert r["sw_hit"] is False


def test_postgres_libs_y_psql_son_cliente_server_es_servidor():
    cli = _db(["postgresql15 15.7-3pgdg.rhel8.x86_64", "postgresql16-libs 16.3-3pgdg.rhel8.x86_64",
               "postgresql16-odbc 18.00.0004-1pgdg.rhel9.8.x86_64",
               "postgresql 10.23-4.0.1.module+el8.9.0+90164+87901204"])["postgres"]
    assert cli["sw_hit"] is False and len(cli["sw_clients"]) == 4
    srv = _db(["postgresql16-server 16.15-1pgdg.rhel9.8.x86_64", "postgresql database 10.23"])["postgres"]
    assert srv["sw_hit"] is True and len(srv["sw_matches"]) == 2


def test_postgres_instalador_windows_es_servidor():
    """EDB en Windows: 'postgresql 16 16.4-1' (mayor, ESPACIO, versión) es el motor; el RPM
    'postgresql 16.4-...' (sin espacio tras el mayor) es el cliente psql."""
    assert _db(["postgresql 16 16.4-1"])["postgres"]["sw_hit"] is True
    assert _db(["postgresql 16.4-1pgdg"])["postgres"]["sw_hit"] is False


def test_mssql_no_se_cruza_con_mysql_server():
    """'sql server' sin \\b tomaba 'mysql server 8.4' como SQL Server."""
    d = _db(["mysql server 8.4 8.4.9"])
    assert d["mssql"]["sw_hit"] is False
    assert d["mysql"]["sw_hit"] is True


def test_mssql_drivers_y_tools_son_cliente():
    r = _db(["microsoft odbc driver 18 for sql server 18.6.1.1",
             "microsoft ole db driver 19 for sql server 19.4.1.0",
             "mssql-tools18 18.6.2.1-1",
             "microsoft command line utilities 15 for sql server 15.0.4298.1"])["mssql"]
    assert r["sw_hit"] is False and r["sw_clients"]


def test_mysql_workbench_y_docs_no_son_servidor():
    r = _db(["mysql workbench 8.0 ce 8.0.47", "mysql documents 5.7 5.7.44",
             "mysql examples and samples 5.7 5.7.44", "mysql-release-el8 1.0-4.el8"])["mysql"]
    assert r["sw_hit"] is False


def test_mariadb_connector_es_libreria_cliente():
    r = _db(["mariadb-connector-c 3.1.11-2.el8_3.x86_64"])["mariadb"]
    assert r["sw_hit"] is False and r["sw_clients"]


def test_mongodb_compass_es_gui_y_gnome_shell_no_es_mongo():
    r = _db(["mongodb compass 1.49.4", "gnome-shell 40.10-35.el9_8.1.x86_64"])["mongodb"]
    assert r["sw_hit"] is False
    assert r["sw_clients"] == ["mongodb compass 1.49.4"], "gnome-shell no es un cliente Mongo"


def test_faltantes_C_informa_los_clientes_vistos():
    """Un DB visto solo como cliente cae en [C] con la evidencia, no en [B]."""
    rec = tcp.reconcile(CATALOG, Counter(), Counter(),
                        {"mariadb-connector-c 3.1.11-2.el8_3.x86_64", "postgresql database 16.15"}, [])
    fleet = {"total": 0, "os_auth": Counter(), "os_unauth": Counter(),
             "n_auth": 0, "n_unauth": 0, "no_os_auth": 0, "no_os_unauth": 0}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "faltantes.txt"
        tcp._write_faltantes(out, CATALOG, rec, fleet, False, [], "US03", "Ley 21.719 - X", "NOW")
        text = out.read_text(encoding="utf-8")
    b = _section(text, "[B] Detectados", "[C] DB")
    c = _section(text, "[C] DB", "Fuera del alcance")
    assert "PostgreSQL" in b, "el motor real sí va a [B]"
    assert "MariaDB" not in b, "un conector cliente NO recomienda el benchmark"
    mdb = next(l for l in c.splitlines() if "MariaDB" in l)
    assert "solo CLIENTES vistos" in mdb and "mariadb-connector-c" in mdb


def test_sw_evidence_dedupes_arch_and_caps():
    matches = [
        "postgresql 10.23-4.0.1.module+el8",
        "postgresql 10.23-4.0.1.module+el8.x86_64",     # dup por arch -> colapsa
        "postgresql15 15.7-3pgdg.rhel8",
        "postgresql15 15.8-1pgdg.rhel8.x86_64",          # versión distinta -> se conserva
        "postgresql 9.6",
        "postgresql 12.1",
    ]
    ev = tcp._sw_evidence(matches, cap=4)
    assert "postgresql 10.23-4.0.1.module+el8" in ev
    assert ".x86_64" not in ev                            # arch podada
    assert ev.count("postgresql 10.23") == 1             # dedupeado (la dup por arch colapsó)
    assert ev.endswith("; …")                             # 5 distintos > cap 4 -> elipsis


def _run():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e or 'assertion'}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERR   {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
