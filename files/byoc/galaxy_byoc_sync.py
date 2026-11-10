#!/usr/bin/python3
"""Provision "bring your own Pulsar" (BYOC) endpoints from Galaxy user preferences.

Managed by the src-component-galaxy playbook (files/byoc/galaxy_byoc_sync.py); installed as
/usr/local/sbin/galaxy-byoc-sync and run as root by galaxy-byoc-sync.timer.

Every run:

1. reads the ``byoc_pulsar|*`` extra preferences of all active Galaxy users (read-only, as
   the galaxy database role over the PostgreSQL unix socket);
2. validates them: username ``[a-z0-9_-]{3,32}``, password 10-128 letters/digits, resource
   fields non-negative numbers. Invalid entries are skipped and logged without the password.
   A username claimed by several users goes to the lowest Galaxy user id; at most
   ``max_endpoints`` endpoints exist, again first come by user id;
3. reconciles RabbitMQ with rabbitmqctl: user ``byoc_<username>`` with access to vhost
   ``/pulsar/byoc_<username>`` only, the galaxy user on that vhost too; ``byoc_*`` users and
   ``/pulsar/byoc_*`` vhosts without a valid preference are deleted. Nothing else is touched.
   Passwords are passed on stdin, never on a command line;
4. writes job_conf.yml = the base job config rendered by Ansible + one PulsarMQJobRunner and
   one environment per endpoint (atomically, mode 0640, group galaxy) and restarts Galaxy
   gracefully only if it changed, at most once per ``min_restart_seconds``.

``--no-restart`` (used by the playbook, which restarts Galaxy itself) prints
``galaxy restart required`` instead of restarting.
"""

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = "/etc/galaxy-byoc-sync/config.json"
SETTINGS = (
    "rabbitmqctl",
    "node",
    "galaxy_password_file",
    "amqp_local_port",
    "base_job_config",
    "job_config",
    "job_config_group",
    "state_file",
    "max_endpoints",
    "min_restart_seconds",
    "restart_command",
    "database_command",
    "runner",
    "environment",
)
RESTART_REQUIRED = "galaxy restart required"

PREFIX = "byoc_"
VHOST_PREFIX = "/pulsar/" + PREFIX
SECTION = "byoc_pulsar|"
USERNAME = re.compile(r"[a-z0-9_-]{3,32}")
# The password goes unescaped into the Pulsar's message_queue_url.
PASSWORD = re.compile(r"[A-Za-z0-9]{10,128}")
INTEGER_LIMITS = ("max_accepted_cores", "min_accepted_gpus", "max_accepted_gpus")
NUMBER_LIMITS = ("max_accepted_mem",)
HASH_ITERATIONS = 200_000

# Users that are deleted, purged or deactivated lose their endpoint. The session is read-only.
QUERY = """\
SET default_transaction_read_only = on;
SELECT coalesce(json_agg(json_build_object('id', u.id, 'preferences', p.value)
                         ORDER BY u.id, p.id), '[]')
FROM galaxy_user u
JOIN user_preference p ON p.user_id = u.id
WHERE p.name = 'extra_user_preferences'
  AND NOT coalesce(u.deleted, false)
  AND NOT coalesce(u.purged, false)
  AND u.active;
"""

log = logging.getLogger("galaxy-byoc-sync")


class SyncError(Exception):
    """A problem that stops the run before or while changing anything."""


class InvalidEntry(ValueError):
    """A user's BYOC preferences cannot be used; the message never contains the password."""


@dataclass(frozen=True)
class Endpoint:
    user_id: int
    username: str
    password: str = field(repr=False)
    limits: dict = field(default_factory=dict, compare=False)

    @property
    def name(self):
        return PREFIX + self.username

    @property
    def vhost(self):
        return VHOST_PREFIX + self.username

    @property
    def runner(self):
        return "pulsar_mq_" + self.name

    @property
    def environment(self):
        return self.name


# --- preferences ------------------------------------------------------------------------


def _limit(name, value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidEntry(f"{name} must be a number")
    text = str(value).strip()
    if name in INTEGER_LIMITS:
        if not re.fullmatch(r"[0-9]+(\.0*)?", text):
            raise InvalidEntry(f"{name} must be a whole number >= 0")
        return int(float(text))
    try:
        number = float(text)
    except ValueError:
        raise InvalidEntry(f"{name} must be a number") from None
    if not math.isfinite(number) or number < 0:
        raise InvalidEntry(f"{name} must be a number >= 0")
    return number


def parse_preferences(user_id, raw):
    """Return the user's Endpoint, None if they did not fill in BYOC, or raise InvalidEntry."""
    if raw is None:
        return None
    try:
        prefs = json.loads(raw)
    except ValueError:
        raise InvalidEntry("extra preferences are not valid JSON") from None
    if not isinstance(prefs, dict):
        raise InvalidEntry("extra preferences are not a JSON object")
    username = prefs.get(SECTION + "username")
    password = prefs.get(SECTION + "password")
    if username in (None, "") and password in (None, ""):
        return None
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise InvalidEntry("username must be 3-32 characters from a-z 0-9 _ -")
    if not isinstance(password, str) or not PASSWORD.fullmatch(password):
        raise InvalidEntry("password must be 10-128 letters and digits")
    limits = {}
    for name in (*INTEGER_LIMITS, *NUMBER_LIMITS):
        value = _limit(name, prefs.get(SECTION + name))
        if value is not None:
            limits[name] = value
    gpus = (limits.get("min_accepted_gpus"), limits.get("max_accepted_gpus"))
    if None not in gpus and gpus[0] > gpus[1]:
        raise InvalidEntry("min_accepted_gpus must not exceed max_accepted_gpus")
    return Endpoint(user_id, username, password, limits)


def select_endpoints(rows, max_endpoints):
    """Valid endpoints by Galaxy user id; first come wins usernames and places under the cap."""
    endpoints = []
    seen_users = set()
    owners = {}
    for row in sorted(rows, key=lambda r: r["id"]):
        user_id = row["id"]
        if user_id in seen_users:
            continue
        seen_users.add(user_id)
        try:
            endpoint = parse_preferences(user_id, row["preferences"])
        except InvalidEntry as error:
            log.warning("user %s: BYOC preferences skipped: %s", user_id, error)
            continue
        if endpoint is None:
            continue
        if endpoint.username in owners:
            log.warning(
                "user %s: BYOC username %r is already taken by user %s; skipped",
                user_id,
                endpoint.username,
                owners[endpoint.username],
            )
            continue
        owners[endpoint.username] = user_id
        if len(endpoints) >= max_endpoints:
            log.warning(
                "user %s: BYOC endpoint %r skipped: limit of %s endpoints reached "
                "(src_galaxy_byoc_pulsar_max)",
                user_id,
                endpoint.username,
                max_endpoints,
            )
            continue
        endpoints.append(endpoint)
    return endpoints


def read_rows(run, settings):
    """Read user ids and extra preferences; raise SyncError on unexpected output."""
    output = run(settings["database_command"], stdin=QUERY)
    try:
        rows = json.loads(output)
    except ValueError:
        raise SyncError("could not read user preferences from the database") from None
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and {"id", "preferences"} <= row.keys() for row in rows
    ):
        raise SyncError("unexpected user preferences from the database")
    return rows


# --- RabbitMQ -----------------------------------------------------------------------------


class RabbitMQ:
    """rabbitmqctl commands; secrets only ever go to stdin."""

    def __init__(self, run, rabbitmqctl, node):
        self._run = run
        self._base = [rabbitmqctl, "-n", node, "-q"]

    def _ctl(self, *args, stdin=None):
        return self._run([*self._base, *args], stdin=stdin)

    def _names(self, *args):
        return {line.split("\t")[0] for line in self._ctl(*args).splitlines()}

    def users(self):
        return self._names("list_users", "--no-table-headers")

    def vhosts(self):
        return self._names("list_vhosts", "--no-table-headers")

    def permitted_vhosts(self, user):
        return self._names("list_user_permissions", user, "--no-table-headers")

    def add_vhost(self, vhost):
        self._ctl("add_vhost", vhost)

    def delete_vhost(self, vhost):
        self._ctl("delete_vhost", vhost)

    def add_user(self, user, password):
        self._ctl("add_user", user, stdin=password)

    def change_password(self, user, password):
        self._ctl("change_password", user, stdin=password)

    def delete_user(self, user):
        self._ctl("delete_user", user)

    def grant(self, vhost, user):
        self._ctl("set_permissions", "-p", vhost, user, ".*", ".*", ".*")


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), HASH_ITERATIONS)
    return f"pbkdf2_sha256${HASH_ITERATIONS}${salt}${digest.hex()}"


def password_matches(stored, password):
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(computed.hex(), digest)


def reconcile(rabbit, endpoints, state):
    """Create/update the endpoints' users and vhosts, delete stale users; return stale vhosts.

    ``state["users"]`` maps each provisioned user to a hash of its password, so a password is
    only (re)set when it changed; an entry is recorded once the user is fully set up.
    """
    users = rabbit.users()
    vhosts = rabbit.vhosts()
    galaxy_vhosts = rabbit.permitted_vhosts("galaxy")
    known = state["users"]
    wanted = {endpoint.name for endpoint in endpoints}

    for user in sorted(users):
        if user.startswith(PREFIX) and user not in wanted:
            rabbit.delete_user(user)
            log.info("deleted RabbitMQ user %s", user)
    for user in set(known) - wanted:
        del known[user]

    for endpoint in endpoints:
        fresh = False
        if endpoint.vhost not in vhosts:
            rabbit.add_vhost(endpoint.vhost)
            fresh = True
        if endpoint.name not in users:
            rabbit.add_user(endpoint.name, endpoint.password)
            fresh = True
        elif not password_matches(known.get(endpoint.name), endpoint.password):
            rabbit.change_password(endpoint.name, endpoint.password)
            fresh = True
        if fresh:
            rabbit.grant(endpoint.vhost, endpoint.name)
            known[endpoint.name] = hash_password(endpoint.password)
            log.info("set up RabbitMQ user %s on %s", endpoint.name, endpoint.vhost)
        if endpoint.vhost not in galaxy_vhosts:
            rabbit.grant(endpoint.vhost, "galaxy")

    wanted_vhosts = {endpoint.vhost for endpoint in endpoints}
    return sorted(v for v in vhosts if v.startswith(VHOST_PREFIX) and v not in wanted_vhosts)


# --- job config ---------------------------------------------------------------------------


def build_job_config(base_text, endpoints, settings, galaxy_password):
    """The base job config plus a PulsarMQJobRunner and an environment per endpoint."""
    try:
        config = yaml.safe_load(base_text)
        runners = config["runners"]
        environments = config["execution"]["environments"]
    except (yaml.YAMLError, TypeError, KeyError):
        # Never include the parser's message: it quotes the file, which holds a password.
        raise SyncError(
            "the base job config is not a job config with runners and environments"
        ) from None
    for endpoint in endpoints:
        runners[endpoint.runner] = {
            "load": "galaxy.jobs.runners.pulsar:PulsarMQJobRunner",
            "amqp_url": (
                f"pyamqp://galaxy:{galaxy_password}@127.0.0.1:{settings['amqp_local_port']}"
                f"/{endpoint.vhost}"
            ),
            **settings["runner"],
        }
        # byoc_owner_id: the default_dispatcher job rule only sends the owner's jobs here.
        environments[endpoint.environment] = {
            "runner": endpoint.runner,
            **settings["environment"],
            "byoc_owner_id": endpoint.user_id,
        }
    header = (
        "# Generated by galaxy-byoc-sync from the base job config and the users'\n"
        "# byoc_pulsar preferences; local changes are overwritten.\n"
    )
    return header + yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


def _atomic_write(path, text, mode, group=None):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), mode)
            if group is not None:
                shutil.chown(tmp, group=group)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def write_if_changed(path, text, group):
    """Atomically replace ``path`` (mode 0640, ``group``) if its content differs."""
    with contextlib.suppress(FileNotFoundError):
        if Path(path).read_text() == text:
            return False
    _atomic_write(path, text, 0o640, group)
    return True


# --- state, restarts and the main loop ----------------------------------------------------


def load_state(path):
    state = {"users": {}, "last_restart": 0, "restart_pending": False}
    try:
        stored = json.loads(Path(path).read_text())
        state.update(stored)
    except FileNotFoundError:
        pass
    except (ValueError, TypeError):
        log.warning("ignoring unreadable state file %s; passwords will be set again", path)
    return state


def save_state(path, state):
    _atomic_write(path, json.dumps(state, indent=1, sort_keys=True) + "\n", 0o600)


def restart_if_needed(changed, state, settings, run, now, no_restart):
    if not (changed or state["restart_pending"]):
        return
    if no_restart:
        print(RESTART_REQUIRED)
        state["restart_pending"] = False
        return
    if now - state["last_restart"] < settings["min_restart_seconds"]:
        state["restart_pending"] = True
        log.info("Galaxy restart deferred: it was restarted less than an interval ago")
        return
    state["restart_pending"] = True  # stays set if the restart fails
    run(settings["restart_command"])
    state.update(restart_pending=False, last_restart=now)
    log.info("Galaxy restarted gracefully to load the new job config")


def sync(settings, run, now, no_restart):
    galaxy_password = Path(settings["galaxy_password_file"]).read_text().strip()
    base_text = Path(settings["base_job_config"]).read_text()
    endpoints = select_endpoints(read_rows(run, settings), settings["max_endpoints"])
    job_config = build_job_config(base_text, endpoints, settings, galaxy_password)

    state = load_state(settings["state_file"])
    rabbit = RabbitMQ(run, settings["rabbitmqctl"], settings["node"])
    try:
        stale_vhosts = reconcile(rabbit, endpoints, state)
        changed = write_if_changed(settings["job_config"], job_config, settings["job_config_group"])
        restart_if_needed(changed, state, settings, run, now, no_restart)
        if state["restart_pending"]:
            # Galaxy still consumes these vhosts until it restarts.
            stale_vhosts = []
        for vhost in stale_vhosts:
            rabbit.delete_vhost(vhost)
            log.info("deleted RabbitMQ vhost %s", vhost)
    finally:
        save_state(settings["state_file"], state)
    log.info("%d endpoint(s): %s", len(endpoints), ", ".join(e.name for e in endpoints) or "-")
    return 0


def load_settings(path):
    settings = json.loads(Path(path).read_text())
    missing = [key for key in SETTINGS if key not in settings]
    if missing:
        raise SyncError(f"{path} is missing: {', '.join(missing)}")
    return settings


@contextlib.contextmanager
def _locked(path):
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def run_command(argv, stdin=None):
    """Run ``argv`` (a list, no shell) with ``stdin``; return stdout, raise on failure."""
    result = subprocess.run(  # noqa: S603 - fixed executables from the root-only config
        argv, input=stdin, capture_output=True, text=True, check=True, timeout=300
    )
    return result.stdout


def main(argv=None, run=run_command, now=time.time):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--no-restart", action="store_true", help=f"print '{RESTART_REQUIRED}'")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = load_settings(args.config)
        with _locked(settings["state_file"] + ".lock"):
            return sync(settings, run, now(), args.no_restart)
    except subprocess.CalledProcessError as error:
        # Only the command line (never a password) and rabbitmqctl's/psql's error output.
        log.error("command failed: %s: %s", error.cmd[:6], (error.stderr or "").strip())
    except (OSError, ValueError, SyncError, subprocess.SubprocessError) as error:
        log.error("BYOC sync failed: %s", error)
    return 1


if __name__ == "__main__":
    sys.exit(main())
