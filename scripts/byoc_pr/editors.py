"""Add one BYOC endpoint to the five infrastructure-playbook files.

YAML files are parsed with ``ruamel.yaml`` in round-trip mode to find the exact line of the
list to extend (and to check the result); the new entry is then inserted as text, so the rest
of the file, its comments, quoting and indentation stay byte-for-byte unchanged and the PR
diff only shows the new lines, as in the hand-written usegalaxy.eu PRs.
``destinations.yml.j2`` is a Jinja template and is edited as text only.
``secret_group_vars/pulsar.yml`` is an Ansible Vault file: decrypted in memory, extended and
encrypted again with the operator's vault password.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from scripts.byoc_pr.endpoint import Endpoint, Settings
from scripts.byoc_pr.vault import Vault

USER_PREFERENCES = "files/galaxy/config/user_preferences_extra_conf.yml"
DESTINATIONS = "files/galaxy/tpv/destinations.yml.j2"
MQ = "group_vars/mq.yml"
SECRETS = "secret_group_vars/pulsar.yml"
JOB_CONF = "templates/galaxy/config/job_conf.yml"
PATHS = (USER_PREFERENCES, DESTINATIONS, MQ, SECRETS, JOB_CONF)

PULSAR_BANNER = re.compile(r"^\s*#\s*PULSAR DESTINATIONS\s*#\s*$")
SECTION_BANNER = re.compile(r"^\s*#\s*[A-Z][A-Z ]*\s*#\s*$")
RULE_LINE = re.compile(r"^\s*#+\s*$")


class EditError(ValueError):
    """A target file does not have the expected layout."""


def _load(text: str, path: str, *, quiet: bool = False) -> Any:
    try:
        return YAML(typ="rt").load(io.StringIO(text))
    except YAMLError as exc:
        detail = "" if quiet else f": {exc}"
        raise EditError(f"{path}: not valid YAML{detail}") from None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _insert_after_last_item(text: str, seq: Any, item_lines: list[str], path: str) -> str:
    """Insert a block sequence item after the last item of ``seq`` (a parsed CommentedSeq)."""
    if not seq:
        raise EditError(f"{path}: the list to extend is empty")
    lines = text.splitlines(keepends=True)
    start = seq.lc.item(len(seq) - 1)[0]
    if not lines[start].lstrip().startswith("- "):
        raise EditError(f"{path}: expected a block list item on line {start + 1}")
    dash = _indent(lines[start])
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped and not stripped.startswith("#") and _indent(lines[end]) <= dash:
            break
        end += 1
    while end - 1 > start and (
        not lines[end - 1].strip()
        or (lines[end - 1].lstrip().startswith("#") and _indent(lines[end - 1]) < dash)
    ):
        end -= 1
    if not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    prefix = " " * dash
    new = [f"{prefix}- {item_lines[0]}\n"] + [f"{prefix}  {line}\n" for line in item_lines[1:]]
    return "".join(lines[:end] + new + lines[end:])


# files/galaxy/config/user_preferences_extra_conf.yml


def _remote_resources_options(data: Any) -> Any:
    try:
        inputs = data["preferences"]["distributed_compute"]["inputs"]
        return next(i for i in inputs if i.get("name") == "remote_resources")["options"]
    except (KeyError, TypeError, StopIteration):
        raise EditError(
            f"{USER_PREFERENCES}: no preferences.distributed_compute remote_resources options"
        ) from None


def user_preferences_has(text: str, endpoint: Endpoint) -> bool:
    options = _remote_resources_options(_load(text, USER_PREFERENCES))
    return any(len(option) > 1 and option[1] == endpoint.tag for option in options)


def user_preferences_add(text: str, endpoint: Endpoint, settings: Settings) -> str:
    options = _remote_resources_options(_load(text, USER_PREFERENCES))
    item = f"[{json.dumps(endpoint.label, ensure_ascii=False)}, {endpoint.tag}]"
    result = _insert_after_last_item(text, options, [item], USER_PREFERENCES)
    if list(_remote_resources_options(_load(result, USER_PREFERENCES))[-1]) != [
        endpoint.label,
        endpoint.tag,
    ]:
        raise EditError(f"{USER_PREFERENCES}: the new option did not parse back as expected")
    return result


# files/galaxy/tpv/destinations.yml.j2 (Jinja: text only)


def destinations_has(text: str, endpoint: Endpoint) -> bool:
    key = rf"^\s+{re.escape(endpoint.destination)}:\s*$"
    runner = rf"^\s+runner:\s*{re.escape(endpoint.runner)}\s*$"
    return bool(re.search(key, text, re.MULTILINE) or re.search(runner, text, re.MULTILINE))


def destinations_add(text: str, endpoint: Endpoint, settings: Settings) -> str:
    lines = text.splitlines(keepends=True)
    banner = next((i for i, line in enumerate(lines) if PULSAR_BANNER.match(line)), None)
    if banner is None:
        raise EditError(f"{DESTINATIONS}: no '# PULSAR DESTINATIONS #' section")
    indent = " " * _indent(lines[banner])
    position = len(lines)
    for i in range(banner + 1, len(lines)):
        if SECTION_BANNER.match(lines[i]) and not RULE_LINE.match(lines[i]):
            position = i - 1 if RULE_LINE.match(lines[i - 1]) else i
            break
    while position > banner and not lines[position - 1].strip():
        position -= 1
    if not lines[position - 1].endswith("\n"):
        lines[position - 1] += "\n"
    request = endpoint.request
    block = [
        f"{endpoint.destination}:",
        "  inherits: pulsar_default",
        f"  runner: {endpoint.runner}",
        f"  max_accepted_cores: {request.max_accepted_cores}",
        f"  max_accepted_mem: {request.max_accepted_mem}",
        f"  min_accepted_gpus: {request.min_accepted_gpus}",
        f"  max_accepted_gpus: {request.max_accepted_gpus}",
        "  scheduling:",
        "    require:",
        f"      - {endpoint.tag}",
    ]
    new = ["\n"] + [f"{indent}{line}\n" for line in block]
    if position == len(lines) or lines[position].strip():
        new.append("\n")
    return "".join(lines[:position] + new + lines[position:])


# group_vars/mq.yml


def _rabbitmq_users(data: Any) -> Any:
    users = data.get("rabbitmq_users") if hasattr(data, "get") else None
    if not isinstance(users, list):
        raise EditError(f"{MQ}: no rabbitmq_users list")
    return users


def mq_has(text: str, endpoint: Endpoint) -> bool:
    return any(
        user.get("user") == endpoint.mq_user or user.get("vhost") == endpoint.vhost
        for user in _rabbitmq_users(_load(text, MQ))
    )


def mq_add(text: str, endpoint: Endpoint, settings: Settings) -> str:
    users = _rabbitmq_users(_load(text, MQ))
    item = [
        f"user: {endpoint.mq_user}",
        f"password: '{{{{ {endpoint.password_var} }}}}'",
        f"vhost: {endpoint.vhost}",
    ]
    result = _insert_after_last_item(text, users, item, MQ)
    if _rabbitmq_users(_load(result, MQ))[-1].get("user") != endpoint.mq_user:
        raise EditError(f"{MQ}: the new user did not parse back as expected")
    return result


# templates/galaxy/config/job_conf.yml


def _plugins(data: Any) -> Any:
    try:
        plugins = data["galaxy_jobconf"]["plugins"]
    except (KeyError, TypeError):
        plugins = None
    if not isinstance(plugins, list):
        raise EditError(f"{JOB_CONF}: no galaxy_jobconf.plugins list")
    return plugins


def job_conf_has(text: str, endpoint: Endpoint) -> bool:
    for plugin in _plugins(_load(text, JOB_CONF)):
        params = plugin.get("params") or {}
        if plugin.get("id") == endpoint.runner or f"/{endpoint.vhost}?" in str(
            params.get("amqp_url", "")
        ):
            return True
    return False


def job_conf_add(text: str, endpoint: Endpoint, settings: Settings) -> str:
    plugins = _plugins(_load(text, JOB_CONF))
    item = [
        f"id: {endpoint.runner}",
        "load: galaxy.jobs.runners.pulsar:PulsarMQJobRunner",
        "params:",
        f'  amqp_url: "{endpoint.amqp_url(settings)}"',
        f'  galaxy_url: "{settings.galaxy_url}"',
        f"  manager: {settings.manager}",
        '  amqp_acknowledge: "true"',
        "  amqp_ack_republish_time: 300",
        "  amqp_consumer_timeout: 2.0",
        '  amqp_publish_retry: "true"',
        "  amqp_publish_retry_max_retries: 60",
    ]
    result = _insert_after_last_item(text, plugins, item, JOB_CONF)
    if _plugins(_load(result, JOB_CONF))[-1].get("id") != endpoint.runner:
        raise EditError(f"{JOB_CONF}: the new runner did not parse back as expected")
    return result


# secret_group_vars/pulsar.yml (Ansible Vault)


def _secret_vars(text: str, vault: Vault) -> tuple[str, dict[str, Any]]:
    if not text.strip():
        return "", {}
    if not Vault.is_encrypted(text):
        raise EditError(
            f"{SECRETS}: not Ansible Vault encrypted; refusing to write a password in plain text"
        )
    plain = vault.decrypt(text)
    data = _load(plain, SECRETS, quiet=True)
    if data is None:
        return plain, {}
    if not isinstance(data, dict):
        raise EditError(f"{SECRETS}: the decrypted file is not a YAML mapping")
    return plain, data


def secrets_has(text: str, endpoint: Endpoint, vault: Vault) -> bool:
    return endpoint.password_var in _secret_vars(text, vault)[1]


def secrets_add(text: str, endpoint: Endpoint, vault: Vault) -> str:
    plain, data = _secret_vars(text, vault)
    if endpoint.password_var in data:
        raise EditError(f"{SECRETS}: {endpoint.password_var} is already set")
    if not plain.strip():
        plain = "---\n"
    if not plain.endswith("\n"):
        plain += "\n"
    # The password is validated as alphanumeric; quoted so that digits stay a string.
    plain += f'{endpoint.password_var}: "{endpoint.request.password}"\n'
    return vault.encrypt(plain)


CHECKS: dict[str, Callable[[str, Endpoint], bool]] = {
    USER_PREFERENCES: user_preferences_has,
    DESTINATIONS: destinations_has,
    MQ: mq_has,
    JOB_CONF: job_conf_has,
}
EDITS: dict[str, Callable[[str, Endpoint, Settings], str]] = {
    USER_PREFERENCES: user_preferences_add,
    DESTINATIONS: destinations_add,
    MQ: mq_add,
    JOB_CONF: job_conf_add,
}
