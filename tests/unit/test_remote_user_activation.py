"""Remote-user (SRAM) accounts are created active (issue #39).

Galaxy's ``User.__init__`` sets ``active = False``; registration (``UserManager.create()``)
then activates the account, but ``UserManager.get_or_create_remote_user()`` never does. On a
``use_remote_user`` Galaxy every SRAM account therefore stays inactive and byoc-sync skips it.
``tasks/remote_user_activation.yml`` installs a PostgreSQL trigger that activates remote-user
accounts as Galaxy inserts them and activates the existing ones once.

The SQL is run against a throwaway PostgreSQL cluster when the PostgreSQL server binaries are
installed (Ubuntu: ``/usr/lib/postgresql/<version>/bin``, present on GitHub's runners).
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

from tests.helpers.ansible_vars import REPO_ROOT

PLAYBOOK = REPO_ROOT / "galaxysrv.yml"
TASKS = REPO_ROOT / "tasks" / "remote_user_activation.yml"
GALAXY_VARS = REPO_ROOT / "vars" / "galaxy_vars.yml"
QUERY = "community.postgresql.postgresql_query"


def _load(path):
    return yaml.safe_load(Path(path).read_text())


def _block():
    (block,) = _load(TASKS)
    return block


def _tasks():
    return {task["name"]: task for task in _block()["block"]}


def _render(value, **extra):
    """Render ``value`` with the block's variables, like Ansible does."""
    variables = {key: trust_as_template(val) for key, val in _block()["vars"].items()}
    variables.update(extra)
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(value))


def _query(name):
    return _render(_tasks()[name][QUERY]["query"])


# --- playbook wiring -------------------------------------------------------------------------


def test_play_includes_the_activation_tasks_after_galaxy_starts():
    """On a new database Galaxy creates galaxy_user on its first start, not in its role."""
    (play,) = _load(PLAYBOOK)
    names = [task["name"] for task in play["tasks"]]
    includes = [
        task
        for task in play["tasks"]
        if task.get("ansible.builtin.include_tasks") == "tasks/remote_user_activation.yml"
    ]
    start = "Ensure services enabled and started"
    (start_task,) = [task for task in play["tasks"] if task["name"] == start]

    assert len(includes) == 1
    assert "when" not in includes[0]
    assert "galaxy.target" in start_task["with_items"]
    assert names.index(includes[0]["name"]) > names.index(start)


def test_tasks_run_as_the_postgres_superuser_on_galaxys_database():
    galaxy_vars = _load(GALAXY_VARS)
    database = _render(_block()["vars"]["_galaxy_activation_db"])

    assert _block()["become"] is True
    assert _block()["become_user"] == "postgres"
    assert galaxy_vars["galaxy_configuration"]["galaxy"]["database_connection"].startswith(
        f"postgresql:///{database}?"
    )
    assert database in [db["name"] for db in galaxy_vars["postgresql_objects_databases"]]
    for task in _tasks().values():
        assert task[QUERY]["login_db"] == "{{ _galaxy_activation_db }}"


@pytest.mark.parametrize(
    "name",
    [
        "Wait for Galaxy to create its database schema",
        "Read the installed remote-user activation trigger",
    ],
)
def test_reads_never_report_a_change(name):
    assert _tasks()[name]["changed_when"] is False


def test_tasks_wait_for_the_schema_first():
    wait = _block()["block"][0]

    assert wait["name"] == "Wait for Galaxy to create its database schema"
    assert wait["until"] == "_galaxy_activation_schema.query_result[0].ready"
    assert wait["retries"] * wait["delay"] >= 300


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        ([], True),  # no trigger yet
        ([{"marker": "an older definition"}], True),
        ([{"marker": None}], True),  # comment removed by hand
        (["current"], False),
    ],
)
def test_trigger_is_installed_only_when_missing_or_changed(installed, expected):
    marker = _render("{{ _galaxy_activation_marker }}")
    rows = [{"marker": marker} if row == "current" else row for row in installed]
    condition = _tasks()[
        "Install the remote-user activation trigger and activate existing accounts"
    ]["when"]

    result = _render(
        f"{{{{ {condition} }}}}",
        _galaxy_activation_installed={"query_result": rows},
    )

    assert result is expected


# --- the SQL against a real PostgreSQL ---------------------------------------------------------

# galaxy_user as Galaxy 26.1 creates it, reduced to the columns the trigger and backfill touch.
GALAXY_USER = """
CREATE TABLE public.galaxy_user (
    id serial PRIMARY KEY,
    email varchar(255) NOT NULL,
    external boolean,
    deleted boolean,
    active boolean NOT NULL DEFAULT true
);
"""


def _pg_bin():
    found = sorted(Path("/usr/lib/postgresql").glob("*/bin/initdb"))
    return found[-1].parent if found else None


def _postgres_unavailable():
    """Why the SQL tests cannot run here, or None. CI must run them, so it never skips."""
    if os.environ.get("CI"):
        return None
    if _pg_bin() is None:
        return "PostgreSQL server binaries not installed (apt install postgresql)"
    if os.geteuid() == 0:
        return "initdb refuses to run as root"
    return None


_POSTGRES_UNAVAILABLE = _postgres_unavailable()
requires_postgres = pytest.mark.skipif(
    _POSTGRES_UNAVAILABLE is not None, reason=str(_POSTGRES_UNAVAILABLE)
)


def _pg(tool, *args):
    """Run a PostgreSQL server tool; return its stdout."""
    result = subprocess.run(  # noqa: S603 - fixed tools, arguments from the test suite
        [_pg_bin() / tool, *map(str, args)], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def psql(tmp_path_factory):
    """Run SQL against a throwaway cluster; return the unaligned, tuples-only output."""
    data = tmp_path_factory.mktemp("pgdata")
    sock = tmp_path_factory.mktemp("pgsock")
    _pg("initdb", "-D", data, "-A", "trust", "-U", "postgres")
    _pg(
        "pg_ctl",
        "-D",
        data,
        "-w",
        "-l",
        data / "log",
        "-o",
        f"-k {sock} -c listen_addresses=''",
        "start",
    )

    def run(sql, database="postgres"):
        return _pg(
            "psql",
            "-h",
            sock,
            "-U",
            "postgres",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-qAt",
            "-c",
            sql,
        )

    run(GALAXY_USER)
    yield run
    _pg("pg_ctl", "-D", data, "-m", "fast", "stop")


@pytest.fixture
def db(psql):
    """The cluster with an empty galaxy_user and no trigger, whatever ran before."""
    psql(
        "DROP TRIGGER IF EXISTS src_activate_remote_user ON public.galaxy_user;"
        "DROP FUNCTION IF EXISTS public.src_activate_remote_user();"
        "TRUNCATE public.galaxy_user RESTART IDENTITY"
    )
    return psql


def _insert_like_galaxy(psql, email, external, deleted=False):
    """INSERT the way Galaxy's ORM does: User.__init__ set active = False explicitly."""
    flags = f"{str(external).lower()}, {str(deleted).lower()}"
    return psql(
        "INSERT INTO public.galaxy_user (email, external, deleted, active) "  # noqa: S608 - test values
        f"VALUES ('{email}', {flags}, false) RETURNING active"
    )


@requires_postgres
def test_sql_activates_remote_users_only(db):
    psql = db
    read = _query("Read the installed remote-user activation trigger")
    marker = _render("{{ _galaxy_activation_marker }}")
    assert _insert_like_galaxy(psql, "before@sram", external=True) == "f"
    assert _insert_like_galaxy(psql, "gone@sram", external=True, deleted=True) == "f"
    assert psql(read) == ""

    psql(_query("Install the remote-user activation trigger and activate existing accounts"))
    psql(
        _query("Install the remote-user activation trigger and activate existing accounts")
    )  # re-running is safe

    assert psql(read) == marker
    assert _insert_like_galaxy(psql, "new@sram", external=True) == "t"
    assert _insert_like_galaxy(psql, "local@example.org", external=False) == "f"

    rows = psql("SELECT email, active FROM public.galaxy_user ORDER BY id").splitlines()
    assert rows == ["before@sram|t", "gone@sram|f", "new@sram|t", "local@example.org|f"]


@requires_postgres
def test_schema_wait_is_false_until_galaxy_user_exists(db):
    wait = _query("Wait for Galaxy to create its database schema")
    db("DROP DATABASE IF EXISTS fresh")
    db("CREATE DATABASE fresh")

    assert db(wait, database="fresh") == "f"
    assert db(wait) == "t"


@requires_postgres
def test_disabled_trigger_counts_as_missing(db):
    psql = db
    read = _query("Read the installed remote-user activation trigger")
    psql(_query("Install the remote-user activation trigger and activate existing accounts"))
    psql("ALTER TABLE public.galaxy_user DISABLE TRIGGER src_activate_remote_user")

    assert psql(read) == ""

    psql(_query("Install the remote-user activation trigger and activate existing accounts"))
    assert psql(read) == _render("{{ _galaxy_activation_marker }}")
