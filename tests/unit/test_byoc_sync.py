"""BYOC Pulsar sync script (files/byoc/galaxy_byoc_sync.py, issue #6).

The script runs as root from a systemd timer. It reads the ``byoc_pulsar|*`` extra
preferences of all Galaxy users, reconciles RabbitMQ (``byoc_<username>`` users and
``/pulsar/byoc_<username>`` vhosts) and regenerates job_conf.yml from the base file
Ansible renders. These tests drive it with a fake command runner standing in for psql,
rabbitmqctl and galaxyctl, so every command line (and its stdin) is recorded.

Galaxy facts the fakes rely on (release_26.0, see /home/mminkova3/research-amqp.md §4):
extra preferences live in ``user_preference`` rows named ``extra_user_preferences``,
as one JSON object keyed ``"<section>|<input>"``; ``type: secret`` inputs without
``store: vault`` are stored there in plain text.
"""

import grp
import importlib.util
import json
import logging
import os
import re
import runpy
import stat
import subprocess
import sys

import pytest
import yaml

from tests.helpers.ansible_vars import REPO_ROOT

SCRIPT = REPO_ROOT / "files" / "byoc" / "galaxy_byoc_sync.py"

CTL = "/usr/sbin/rabbitmqctl"
NODE = "rabbit@localhost"
DB_COMMAND = ["/usr/sbin/runuser", "-u", "galaxy", "--", "/usr/bin/psql", "-d", "galaxy"]
RESTART = ["/usr/local/bin/galaxyctl", "graceful"]
GALAXY_PW = "GalaxyPassw0rd1234567890"
ALICE_PW = "AlicePassw0rd"
BOB_PW = "BobPassw0rd99"
CAROL_PW = "CarolPassw0rd"
BASE_JOB_CONF = """\
runners:
  local:
    load: galaxy.jobs.runners.local:LocalJobRunner
    workers: 4
execution:
  default: default_dispatcher
  environments:
    local:
      runner: local
    default_dispatcher:
      runner: dynamic
      type: python
      function: default_dispatcher
tools:
- class: local
  environment: local
"""
RUNNER_SETTINGS = {
    "galaxy_url": "https://galaxy.example.org",
    "manager": "_default_",
    "amqp_acknowledge": True,
    "amqp_ack_republish_time": 1200,
    "amqp_consumer_timeout": 2.0,
    "amqp_publish_retry": True,
    "amqp_publish_retry_max_retries": 60,
}
ENVIRONMENT_SETTINGS = {
    "jobs_directory": "/mnt/pulsar/files/staging",
    "default_file_action": "remote_transfer",
    "dependency_resolution": "remote",
    "rewrite_parameters": True,
    "remote_metadata": False,
    "outputs_to_working_directory": False,
}


def _load_module():
    spec = importlib.util.spec_from_file_location("galaxy_byoc_sync", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


sync = _load_module()


def _prefs(username=None, password=None, **extra):
    prefs = {"distributed_compute|remote_resources": "byoc"}
    if username is not None:
        prefs["byoc_pulsar|username"] = username
    if password is not None:
        prefs["byoc_pulsar|password"] = password
    prefs.update({f"byoc_pulsar|{key}": value for key, value in extra.items()})
    return prefs


def _row(user_id, prefs):
    return {"id": user_id, "preferences": json.dumps(prefs)}


class FakeSystem:
    """psql, rabbitmqctl and galaxyctl in one callable with the signature of run_command."""

    def __init__(self, rows=(), users=("galaxy",), vhosts=("/",), galaxy_vhosts=()):
        self.rows = list(rows)
        self.users = {name: None for name in users}
        self.vhosts = set(vhosts)
        self.permissions = {"galaxy": set(galaxy_vhosts)}
        self.calls = []
        self.restarts = 0
        self.fail_on = None
        self.db_output = None

    def mutations(self):
        read_only = {"list_users", "list_vhosts", "list_user_permissions"}
        return [argv[4:] for argv, _ in self.calls if argv[0] == CTL and argv[4] not in read_only]

    def __call__(self, argv, stdin=None):
        argv = list(argv)
        self.calls.append((argv, stdin))
        if argv == DB_COMMAND:
            assert "SELECT" in stdin
            if self.db_output is not None:
                return self.db_output
            return json.dumps(self.rows) + "\n"
        if argv == RESTART:
            self.restarts += 1
            return ""
        assert argv[:4] == [CTL, "-n", NODE, "-q"]
        command, args = argv[4], argv[5:]
        if command == self.fail_on:
            raise subprocess.CalledProcessError(70, argv, "", "boom")
        return getattr(self, "_" + command)(args, stdin)

    def _list_users(self, args, stdin):
        assert args == ["--no-table-headers"]
        return "".join(f"{name}\t[]\n" for name in sorted(self.users))

    def _list_vhosts(self, args, stdin):
        assert args == ["--no-table-headers"]
        return "".join(f"{name}\n" for name in sorted(self.vhosts))

    def _list_user_permissions(self, args, stdin):
        assert args[1:] == ["--no-table-headers"]
        return "".join(f"{v}\t.*\t.*\t.*\n" for v in sorted(self.permissions.get(args[0], ())))

    def _add_vhost(self, args, stdin):
        assert args[0] not in self.vhosts
        self.vhosts.add(args[0])

    def _delete_vhost(self, args, stdin):
        self.vhosts.remove(args[0])
        for vhosts in self.permissions.values():
            vhosts.discard(args[0])

    def _add_user(self, args, stdin):
        assert args[0] not in self.users
        assert stdin
        self.users[args[0]] = stdin

    def _change_password(self, args, stdin):
        assert args[0] in self.users
        assert stdin
        self.users[args[0]] = stdin

    def _delete_user(self, args, stdin):
        del self.users[args[0]]
        self.permissions.pop(args[0], None)

    def _set_permissions(self, args, stdin):
        assert args[0] == "-p"
        assert args[3:] == [".*", ".*", ".*"]
        assert args[2] in self.users
        assert args[1] in self.vhosts
        self.permissions.setdefault(args[2], set()).add(args[1])


@pytest.fixture(autouse=True)
def _info_logs(caplog):
    caplog.set_level(logging.INFO, logger="galaxy-byoc-sync")


@pytest.fixture
def setup(tmp_path):
    """Write config.json, the galaxy password file and the base job config; return paths."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "galaxy.pass").write_text(GALAXY_PW)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "job_conf_base.yml").write_text(BASE_JOB_CONF)
    settings = {
        "rabbitmqctl": CTL,
        "node": NODE,
        "galaxy_password_file": str(secrets / "galaxy.pass"),
        "amqp_local_port": 5672,
        "base_job_config": str(config_dir / "job_conf_base.yml"),
        "job_config": str(config_dir / "job_conf.yml"),
        "job_config_group": grp.getgrgid(os.getgid()).gr_name,
        "state_file": str(tmp_path / "state" / "state.json"),
        "max_endpoints": 20,
        "min_restart_seconds": 810,
        "restart_command": RESTART,
        "database_command": DB_COMMAND,
        "runner": RUNNER_SETTINGS,
        "environment": ENVIRONMENT_SETTINGS,
    }
    (tmp_path / "state").mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(settings))
    return {
        "config": config,
        "settings": settings,
        "job_conf": config_dir / "job_conf.yml",
        "state": tmp_path / "state" / "state.json",
    }


def _run(setup, system, now=10_000.0, extra_args=()):
    return sync.main(["--config", str(setup["config"]), *extra_args], run=system, now=lambda: now)


def _job_conf(setup):
    return yaml.safe_load(setup["job_conf"].read_text())


def _state(setup):
    return json.loads(setup["state"].read_text())


def _all_text(system, caplog, capsys=None):
    argvs = " ".join(" ".join(argv) for argv, _ in system.calls)
    out = capsys.readouterr() if capsys else ("", "")
    return argvs + caplog.text + out[0] + out[1]


# --- validating preferences ------------------------------------------------------


@pytest.mark.parametrize(
    "prefs",
    [
        {},
        {"distributed_compute|remote_resources": "None"},
        _prefs("", ""),
        _prefs(None, None, max_accepted_cores="4"),
        _prefs("", None),
    ],
    ids=["empty", "other-section", "blank", "only-limits", "blank-username"],
)
def test_users_without_byoc_credentials_are_not_endpoints(prefs):
    assert sync.parse_preferences(1, json.dumps(prefs)) is None


def test_valid_preferences_give_an_endpoint():
    raw = json.dumps(
        _prefs(
            "my-pulsar_1",
            ALICE_PW,
            max_accepted_cores="8",
            max_accepted_mem=15.5,
            min_accepted_gpus=0,
            max_accepted_gpus="",
        )
    )

    endpoint = sync.parse_preferences(7, raw)

    assert endpoint.user_id == 7
    assert endpoint.username == "my-pulsar_1"
    assert endpoint.password == ALICE_PW
    assert endpoint.name == "byoc_my-pulsar_1"
    assert endpoint.vhost == "/pulsar/byoc_my-pulsar_1"
    assert endpoint.runner == "pulsar_mq_byoc_my-pulsar_1"
    assert endpoint.environment == "byoc_my-pulsar_1"
    assert endpoint.limits == {
        "max_accepted_cores": 8,
        "max_accepted_mem": 15.5,
        "min_accepted_gpus": 0,
    }
    assert ALICE_PW not in repr(endpoint)


@pytest.mark.parametrize(
    ("prefs", "reason"),
    [
        (_prefs("ab", ALICE_PW), "username"),
        (_prefs("a" * 33, ALICE_PW), "username"),
        (_prefs("Alice", ALICE_PW), "username"),
        (_prefs("al ice", ALICE_PW), "username"),
        (_prefs("al.ice", ALICE_PW), "username"),
        (_prefs(123456, ALICE_PW), "username"),
        (_prefs(None, ALICE_PW), "username"),
        (_prefs("alice", None), "password"),
        (_prefs("alice", ""), "password"),
        (_prefs("alice", "Short123"), "password"),
        (_prefs("alice", "has space 123"), "password"),
        (_prefs("alice", "Symbols!@#$%^&*"), "password"),
        (_prefs("alice", "x" * 129), "password"),
        (_prefs("alice", 12345678901), "password"),
        (_prefs("alice", "__SECRET_PLACEHOLDER__"), "password"),
        (_prefs("alice", ALICE_PW, max_accepted_cores="four"), "max_accepted_cores"),
        (_prefs("alice", ALICE_PW, max_accepted_cores="-1"), "max_accepted_cores"),
        (_prefs("alice", ALICE_PW, max_accepted_cores=-1), "max_accepted_cores"),
        (_prefs("alice", ALICE_PW, max_accepted_cores="1.5"), "max_accepted_cores"),
        (_prefs("alice", ALICE_PW, max_accepted_cores=1.5), "max_accepted_cores"),
        (_prefs("alice", ALICE_PW, max_accepted_cores=True), "max_accepted_cores"),
        (_prefs("alice", ALICE_PW, max_accepted_mem="lots"), "max_accepted_mem"),
        (_prefs("alice", ALICE_PW, max_accepted_mem="nan"), "max_accepted_mem"),
        (_prefs("alice", ALICE_PW, max_accepted_mem="inf"), "max_accepted_mem"),
        (_prefs("alice", ALICE_PW, max_accepted_mem=[1]), "max_accepted_mem"),
        (_prefs("alice", ALICE_PW, max_accepted_mem=-0.5), "max_accepted_mem"),
        (_prefs("alice", ALICE_PW, min_accepted_gpus=2, max_accepted_gpus=1), "gpus"),
    ],
)
def test_invalid_preferences_are_rejected_without_echoing_the_password(prefs, reason):
    with pytest.raises(sync.InvalidEntry, match=reason) as error:
        sync.parse_preferences(1, json.dumps(prefs))

    password = prefs.get("byoc_pulsar|password")
    if isinstance(password, str) and password:
        assert password not in str(error.value)


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"', "null"])
def test_unreadable_preferences_are_rejected(raw):
    with pytest.raises(sync.InvalidEntry, match="preferences"):
        sync.parse_preferences(1, raw)


@pytest.mark.parametrize("mem", ["7.5", 7, "0", "  16 "])
def test_memory_may_be_fractional(mem):
    endpoint = sync.parse_preferences(
        1, json.dumps(_prefs("alice", ALICE_PW, max_accepted_mem=mem))
    )

    assert endpoint.limits["max_accepted_mem"] == float(str(mem).strip())


# --- choosing endpoints: collisions and the cap -----------------------------------------


def test_username_collisions_go_to_the_lowest_galaxy_user_id(caplog):
    rows = [
        _row(9, _prefs("shared", BOB_PW)),
        _row(3, _prefs("shared", ALICE_PW)),
        _row(5, _prefs("other", CAROL_PW)),
    ]

    endpoints = sync.select_endpoints(rows, max_endpoints=20)

    assert [(e.user_id, e.username) for e in endpoints] == [(3, "shared"), (5, "other")]
    assert "user 9" in caplog.text
    assert "already taken" in caplog.text
    assert BOB_PW not in caplog.text


def test_endpoints_are_capped_first_come_by_user_id(caplog):
    rows = [_row(i, _prefs(f"user{i:03d}", ALICE_PW)) for i in (4, 1, 3, 2)]

    endpoints = sync.select_endpoints(rows, max_endpoints=2)

    assert [e.user_id for e in endpoints] == [1, 2]
    assert "src_galaxy_byoc_pulsar_max" in caplog.text
    assert "user 3" in caplog.text
    assert "user 4" in caplog.text


def test_a_cap_of_zero_disables_all_endpoints():
    assert sync.select_endpoints([_row(1, _prefs("alice", ALICE_PW))], max_endpoints=0) == []


def test_invalid_and_unused_rows_are_skipped_and_logged_without_passwords(caplog):
    rows = [
        _row(1, _prefs("alice", "bad pass word!")),
        _row(2, {}),
        {"id": 3, "preferences": "{broken"},
        {"id": 4, "preferences": None},
        _row(5, _prefs("bob", BOB_PW)),
    ]

    endpoints = sync.select_endpoints(rows, max_endpoints=20)

    assert [e.username for e in endpoints] == ["bob"]
    assert "user 1" in caplog.text
    assert "user 3" in caplog.text
    assert "bad pass word!" not in caplog.text
    assert "user 2" not in caplog.text
    assert "user 4" not in caplog.text


def test_duplicate_rows_for_one_user_keep_the_first():
    rows = [_row(1, _prefs("alice", ALICE_PW)), _row(1, _prefs("other", BOB_PW))]

    assert [e.username for e in sync.select_endpoints(rows, 20)] == ["alice"]


# --- reading the database --------------------------------------------------------------


def test_database_is_read_through_the_configured_command_with_a_read_only_query(setup):
    system = FakeSystem(rows=[_row(1, _prefs("alice", ALICE_PW))])

    rows = sync.read_rows(system, setup["settings"])

    [(argv, query)] = system.calls
    assert argv == DB_COMMAND
    assert "default_transaction_read_only" in query
    assert "user_preference" in query
    assert "extra_user_preferences" in query
    assert "deleted" in query
    assert "purged" in query
    for keyword in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "GRANT"):
        assert not re.search(rf"\b{keyword}\b", query, re.IGNORECASE)
    assert rows == [_row(1, _prefs("alice", ALICE_PW))]


@pytest.mark.parametrize("output", ["", "not json", '{"id": 1}', '[{"id": 1}]', '["x"]'])
def test_unexpected_database_output_is_an_error(setup, output):
    system = FakeSystem()
    system.db_output = output

    with pytest.raises(sync.SyncError, match="database"):
        sync.read_rows(system, setup["settings"])


# --- end to end: RabbitMQ --------------------------------------------------------------


def test_new_endpoint_gets_vhost_user_and_permissions(setup, caplog):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])

    assert _run(setup, system) == 0

    assert "/pulsar/byoc_alice" in system.vhosts
    assert system.users["byoc_alice"] == ALICE_PW
    assert system.permissions["byoc_alice"] == {"/pulsar/byoc_alice"}
    assert "/pulsar/byoc_alice" in system.permissions["galaxy"]
    assert "byoc_alice" in caplog.text


def test_passwords_only_travel_on_stdin(setup, caplog, capsys):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW)), _row(4, _prefs("bob", BOB_PW))])
    _run(setup, system)
    system.rows = [_row(3, _prefs("alice", CAROL_PW))]
    _run(setup, system, now=20_000.0)

    password_stdins = [stdin for _, stdin in system.calls if stdin in (ALICE_PW, BOB_PW, CAROL_PW)]
    assert len(password_stdins) == 3
    text = _all_text(system, caplog, capsys)
    for secret in (ALICE_PW, BOB_PW, CAROL_PW, GALAXY_PW):
        assert secret not in text
    assert ALICE_PW not in setup["state"].read_text()


def test_unchanged_preferences_change_nothing_and_do_not_restart(setup):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])
    _run(setup, system)
    assert system.restarts == 1
    before = setup["job_conf"].stat().st_mtime_ns
    mutations = len(system.mutations())

    assert _run(setup, system, now=50_000.0) == 0

    assert len(system.mutations()) == mutations
    assert system.restarts == 1
    assert setup["job_conf"].stat().st_mtime_ns == before


def test_changed_password_is_updated_through_stdin(setup):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])
    _run(setup, system)
    system.rows = [_row(3, _prefs("alice", BOB_PW))]

    _run(setup, system, now=50_000.0)

    assert system.users["byoc_alice"] == BOB_PW
    assert system.mutations()[-2:] == [
        ["change_password", "byoc_alice"],
        ["set_permissions", "-p", "/pulsar/byoc_alice", "byoc_alice", ".*", ".*", ".*"],
    ]
    assert system.restarts == 1  # the job config does not contain the endpoint's password


def test_existing_user_unknown_to_the_state_gets_password_and_permissions(setup):
    # e.g. a run that stopped after add_user: the password and permissions are set again.
    system = FakeSystem(
        rows=[_row(3, _prefs("alice", ALICE_PW))],
        users=("galaxy", "byoc_alice"),
        vhosts=("/", "/pulsar/byoc_alice"),
    )

    _run(setup, system)

    assert ["change_password", "byoc_alice"] in system.mutations()
    assert system.permissions["byoc_alice"] == {"/pulsar/byoc_alice"}
    assert "/pulsar/byoc_alice" in system.permissions["galaxy"]


def test_corrupt_state_file_is_treated_as_empty(setup, caplog):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])
    _run(setup, system)
    setup["state"].write_text("{broken")

    assert _run(setup, system, now=50_000.0) == 0

    assert system.mutations()[-2:] == [
        ["change_password", "byoc_alice"],
        ["set_permissions", "-p", "/pulsar/byoc_alice", "byoc_alice", ".*", ".*", ".*"],
    ]
    assert "state" in caplog.text


@pytest.mark.parametrize("stored", ["garbage", "pbkdf2_sha256$x$00$00", "md5$1$00$00"])
def test_malformed_stored_hashes_never_match(stored):
    assert sync.password_matches(stored, ALICE_PW) is False
    assert sync.password_matches(None, ALICE_PW) is False
    assert sync.password_matches(sync.hash_password(ALICE_PW), ALICE_PW) is True
    assert sync.password_matches(sync.hash_password(ALICE_PW), BOB_PW) is False


def test_galaxy_permission_revoked_elsewhere_is_granted_again(setup):
    # community.rabbitmq.rabbitmq_user in tasks/rabbitmq.yml sets the galaxy user's full
    # permission list on every playbook run, which drops the BYOC vhosts.
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])
    _run(setup, system)
    system.permissions["galaxy"].clear()

    _run(setup, system, now=50_000.0)

    assert system.permissions["galaxy"] == {"/pulsar/byoc_alice"}
    assert system.mutations()[-1] == [
        "set_permissions",
        "-p",
        "/pulsar/byoc_alice",
        "galaxy",
        ".*",
        ".*",
        ".*",
    ]


def test_removed_endpoints_are_deleted_and_nothing_else_is_touched(setup):
    others_users = ("galaxy", "hpc", "byocx", "BYOC_upper")
    others_vhosts = ("/", "/pulsar/hpc", "/pulsar/byocx", "/byoc_alice")
    system = FakeSystem(
        rows=[_row(3, _prefs("alice", ALICE_PW)), _row(4, _prefs("bob", BOB_PW))],
        users=others_users,
        vhosts=others_vhosts,
        galaxy_vhosts=["/pulsar/hpc"],
    )
    _run(setup, system)
    system.rows = [_row(4, _prefs("bob", BOB_PW))]

    _run(setup, system, now=50_000.0)

    assert set(system.users) == {*others_users, "byoc_bob"}
    assert system.vhosts == {*others_vhosts, "/pulsar/byoc_bob"}
    assert system.permissions["galaxy"] == {"/pulsar/hpc", "/pulsar/byoc_bob"}
    assert "byoc_alice" not in _state(setup)["users"]
    deletions = [m for m in system.mutations() if m[0].startswith("delete_")]
    assert deletions == [["delete_user", "byoc_alice"], ["delete_vhost", "/pulsar/byoc_alice"]]


def test_leftover_byoc_objects_without_any_preference_are_removed(setup):
    system = FakeSystem(users=("galaxy", "byoc_ghost"), vhosts=("/", "/pulsar/byoc_ghost"))

    assert _run(setup, system) == 0

    assert set(system.users) == {"galaxy"}
    assert system.vhosts == {"/"}


def test_rabbitmq_failure_fails_the_run_but_keeps_completed_work(setup, caplog):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])
    system.fail_on = "set_permissions"

    assert _run(setup, system) == 1

    assert "byoc_alice" in system.users
    assert "set_permissions" in caplog.text
    assert not setup["job_conf"].exists()
    assert _state(setup)["users"] == {}
    assert ALICE_PW not in caplog.text


# --- end to end: job config ---------------------------------------------------------------


def test_job_config_is_the_base_plus_one_runner_and_environment_per_endpoint(setup):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW)), _row(8, _prefs("bob", BOB_PW))])

    _run(setup, system)

    text = setup["job_conf"].read_text()
    conf = yaml.safe_load(text)
    base = yaml.safe_load(BASE_JOB_CONF)
    assert text.startswith("# Generated by galaxy-byoc-sync")
    assert conf["tools"] == base["tools"]
    assert conf["execution"]["default"] == "default_dispatcher"
    assert list(conf["runners"]) == ["local", "pulsar_mq_byoc_alice", "pulsar_mq_byoc_bob"]
    assert conf["runners"]["pulsar_mq_byoc_alice"] == {
        "load": "galaxy.jobs.runners.pulsar:PulsarMQJobRunner",
        "amqp_url": f"pyamqp://galaxy:{GALAXY_PW}@127.0.0.1:5672//pulsar/byoc_alice",
        **RUNNER_SETTINGS,
    }
    assert conf["execution"]["environments"]["byoc_bob"] == {
        "runner": "pulsar_mq_byoc_bob",
        **ENVIRONMENT_SETTINGS,
        "byoc_owner_id": 8,
    }
    assert ALICE_PW not in text
    assert BOB_PW not in text


def test_job_config_is_written_atomically_for_galaxy_only(setup):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])

    _run(setup, system)

    info = setup["job_conf"].stat()
    assert stat.S_IMODE(info.st_mode) == 0o640
    assert info.st_gid == grp.getgrnam(setup["settings"]["job_config_group"]).gr_gid
    leftovers = [p.name for p in setup["job_conf"].parent.iterdir()]
    assert sorted(leftovers) == ["job_conf.yml", "job_conf_base.yml"]
    assert stat.S_IMODE(setup["state"].stat().st_mode) == 0o600


def test_without_endpoints_the_job_config_equals_the_base(setup):
    _run(setup, FakeSystem())

    assert _job_conf(setup) == yaml.safe_load(BASE_JOB_CONF)


def test_base_change_alone_regenerates_and_restarts(setup):
    system = FakeSystem()
    _run(setup, system)
    restarts = system.restarts
    base = setup["settings"]["base_job_config"]
    with open(base, "a") as handle:
        handle.write("- id: bwa\n  environment: local\n")

    _run(setup, system, now=50_000.0)

    assert system.restarts == restarts + 1
    assert _job_conf(setup)["tools"][-1] == {"id": "bwa", "environment": "local"}


def test_invalid_base_job_config_fails_without_echoing_it(setup, caplog):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])
    with open(setup["settings"]["base_job_config"], "w") as handle:
        handle.write(f"runners: [pyamqp://galaxy:{GALAXY_PW}@x\n  bad: : :\n")

    assert _run(setup, system) == 1

    assert GALAXY_PW not in caplog.text
    assert "base job config" in caplog.text
    assert system.mutations() == []


def test_base_without_runners_or_environments_is_an_error(setup, caplog):
    with open(setup["settings"]["base_job_config"], "w") as handle:
        handle.write("tools: []\n")

    assert _run(setup, FakeSystem()) == 1

    assert "base job config" in caplog.text


# --- restarts -------------------------------------------------------------------------


def test_restarts_are_limited_to_one_per_interval(setup, caplog):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])
    _run(setup, system, now=10_000.0)
    assert system.restarts == 1

    system.rows = [_row(4, _prefs("bob", BOB_PW))]
    _run(setup, system, now=10_300.0)  # 5 minutes later: deferred

    assert system.restarts == 1
    assert _state(setup)["restart_pending"] is True
    assert "deferred" in caplog.text
    # Galaxy still consumes the old vhost until it restarts, so it is kept for now.
    assert "/pulsar/byoc_alice" in system.vhosts
    assert "byoc_alice" not in system.users

    _run(setup, system, now=10_000.0 + 810)  # nothing changed, but the restart is due

    assert system.restarts == 2
    assert _state(setup)["restart_pending"] is False
    assert "/pulsar/byoc_alice" not in system.vhosts


def test_failed_restart_stays_pending(setup):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])

    def failing(argv, stdin=None):
        if argv == RESTART:
            raise subprocess.CalledProcessError(1, argv)
        return system(argv, stdin)

    assert sync.main(["--config", str(setup["config"])], run=failing, now=lambda: 1e6) == 1

    assert _state(setup)["restart_pending"] is True
    _run(setup, system, now=2e6)
    assert system.restarts == 1


def test_no_restart_mode_reports_instead_of_restarting(setup, capsys):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])

    assert _run(setup, system, extra_args=["--no-restart"]) == 0

    assert system.restarts == 0
    assert capsys.readouterr().out.strip() == sync.RESTART_REQUIRED
    assert _state(setup)["restart_pending"] is False

    _run(setup, system, extra_args=["--no-restart"])
    assert capsys.readouterr().out == ""


# --- failures before anything is changed -------------------------------------------------


def test_database_failure_changes_nothing(setup, caplog):
    system = FakeSystem(users=("galaxy", "byoc_alice"), vhosts=("/", "/pulsar/byoc_alice"))
    system.db_output = "ERROR"

    assert _run(setup, system) == 1

    assert system.mutations() == []
    assert not setup["job_conf"].exists()
    assert "database" in caplog.text


def test_missing_config_fails(tmp_path, caplog):
    assert sync.main(["--config", str(tmp_path / "missing.json")], run=FakeSystem()) == 1

    assert "missing.json" in caplog.text


def test_settings_are_complete(setup):
    assert set(setup["settings"]) == set(sync.SETTINGS)


def test_incomplete_config_fails(setup, caplog):
    settings = dict(setup["settings"])
    del settings["max_endpoints"]
    setup["config"].write_text(json.dumps(settings))

    assert _run(setup, FakeSystem()) == 1

    assert "max_endpoints" in caplog.text


# --- running commands and the script entry point ------------------------------------------


def test_failed_write_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "job_conf.yml"

    with pytest.raises(LookupError):
        sync.write_if_changed(target, "x: 1\n", "no-such-group-for-byoc-tests")

    assert list(tmp_path.iterdir()) == []


def test_run_command_passes_stdin_and_returns_stdout():
    assert sync.run_command(["/bin/cat"], stdin="hello") == "hello"
    assert sync.run_command(["/bin/echo", "hi"]) == "hi\n"


def test_run_command_raises_on_failure():
    with pytest.raises(subprocess.CalledProcessError):
        sync.run_command(["/bin/false"])


def test_script_entry_point(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["galaxy-byoc-sync", "--config", str(tmp_path / "x.json")])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(SCRIPT), run_name="__main__")

    assert exit_info.value.code == 1


def test_script_logs_a_summary(setup, caplog):
    system = FakeSystem(rows=[_row(3, _prefs("alice", ALICE_PW))])

    _run(setup, system)

    assert any(r.name == "galaxy-byoc-sync" for r in caplog.records)
    assert "1 endpoint" in caplog.text
