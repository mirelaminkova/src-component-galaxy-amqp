"""Checks for the EU-layout BYOC onboarding files (issue #21).

An onboarding pull request (modelled on usegalaxy-eu/infrastructure-playbook #1384 and #872)
adds one endpoint ``<id>`` to these files:

* ``files/galaxy/config/user_preferences_extra_conf.yml``: an option ``[label, <tag>]`` in
  ``preferences.distributed_compute`` (``remote_resources``);
* ``files/galaxy/tpv/destinations.yml.j2``: a destination with ``runner: pulsar_eu_<id>`` that
  requires ``<tag>``;
* ``group_vars/mq.yml``: a ``rabbitmq_users`` entry ``galaxy_<id>`` on vhost
  ``/pulsar/galaxy_<id>`` whose password is ``{{ rabbitmq_password_galaxy_<id> }}``;
* ``secret_group_vars/pulsar.yml`` (Ansible Vault): ``rabbitmq_password_galaxy_<id>``;
* ``templates/galaxy/config/job_conf.yml``: a ``galaxy_jobconf.plugins`` entry
  ``pulsar_eu_<id>`` (``PulsarMQJobRunner``) whose ``amqp_url`` uses that user and vhost.

:func:`byoc_onboarding_errors` checks that the files fit together. It runs in two modes:

* raw (``resolved`` false): on the files as committed, in CI. Passwords must be references
  to the vault variable, never literal values, and labels and names must not contain Jinja.
* resolved: at deploy time on the values Ansible resolved; passwords must be URL-safe.

:func:`byoc_runners` then points each runner at this Galaxy: an ``amqp_url`` copied from
usegalaxy.eu (``mq.galaxyproject.eu``) is rebuilt with this host's RabbitMQ, and
``galaxy_url`` is always this Galaxy's URL.

No message ever contains a password or an ``amqp_url``.
"""

import re

RUNNER_LOAD = "galaxy.jobs.runners.pulsar:PulsarMQJobRunner"
DEFAULT_TAG = "None"
# <id> becomes part of Jinja variable names (rabbitmq_password_galaxy_<id>), so no '-'.
_ID = r"[a-z0-9_]+"
_MQ_USER = re.compile(rf"galaxy_({_ID})")
_RUNNER_ID = re.compile(rf"pulsar_eu_({_ID})")
_TAG = re.compile(r"[A-Za-z0-9_.-]+")
_DESTINATION = re.compile(r"[A-Za-z0-9_]+")
_SECRET_NAME = re.compile(rf"rabbitmq_password_galaxy_{_ID}")
# Runner params become keys of the runner in job_conf.yml; `id` and `load` are the runner's own.
_PARAM_KEY = re.compile(r"[a-z][a-z0-9_]*")
_RESERVED_PARAMS = ("id", "load")
# Put into amqp_url unescaped: URL-unreserved characters only; usegalaxy.eu asks for >= 10.
_PASSWORD = re.compile(r"[A-Za-z0-9._~-]{10,}")
_AMQP_URL = re.compile(
    r"pyamqp://(?P<user>[^:@/]+):(?P<password>[^@]+)@(?P<host>[^@/:]+)(?::(?P<port>[^/]+))?"
    r"/(?P<vhost>[^?]*)(?:\?(?P<query>.*))?"
)
_JINJA = ("{{", "{%", "{#")


def _password_ref(user):
    return re.compile(rf"\{{\{{\s*rabbitmq_password_{re.escape(user)}\s*\}}\}}")


def _is_str(value):
    return isinstance(value, str)


def _has_jinja(value):
    return any(marker in value for marker in _JINJA)


def preference_options(preferences):
    """Return the ``remote_resources`` options of an EU-layout preferences file.

    The default option (``None``) is left out; our template adds its own. Malformed
    content gives what can be read; :func:`byoc_onboarding_errors` reports it.
    """
    section = (preferences or {}).get("preferences") or {}
    compute = section.get("distributed_compute") or {} if isinstance(section, dict) else {}
    inputs = compute.get("inputs") or [] if isinstance(compute, dict) else []
    options = []
    for item in inputs if isinstance(inputs, list) else []:
        if isinstance(item, dict) and item.get("name") == "remote_resources":
            options.extend(item.get("options") or [])
    return [
        option
        for option in options
        if not (isinstance(option, list) and len(option) == 2 and option[1] == DEFAULT_TAG)
    ]


def _mq_user_errors(users, resolved, reserved):
    errors, names = [], set()
    if not isinstance(users, list):
        return ["group_vars/mq.yml: rabbitmq_users must be a list"], names
    for number, user in enumerate(users, start=1):
        where = f"group_vars/mq.yml rabbitmq_users entry {number}"
        if not isinstance(user, dict) or not _is_str(user.get("user")):
            errors.append(f"{where}: must be a mapping with 'user', 'password' and 'vhost'")
            continue
        name = user["user"]
        if not _MQ_USER.fullmatch(name):
            errors.append(f"{where}: 'user' must match galaxy_<id> with <id> in [a-z0-9_]+")
            continue
        where = f"group_vars/mq.yml rabbitmq_users '{name}'"
        if name in reserved:
            errors.append(f"{where}: already used by src_galaxy_pulsar_amqp_configs or RabbitMQ")
        if name in names:
            errors.append(f"{where}: duplicate user")
        names.add(name)
        unknown = sorted(set(user) - {"user", "password", "vhost"})
        errors.extend(f"{where}: unknown key '{key}'" for key in unknown)
        if user.get("vhost") != f"/pulsar/{name}":
            errors.append(f"{where}: 'vhost' must be /pulsar/{name}")
        password = user.get("password")
        if not resolved:
            if not (_is_str(password) and _password_ref(name).fullmatch(password.strip())):
                errors.append(
                    f"{where}: 'password' must be '{{{{ rabbitmq_password_{name} }}}}' "
                    "(the value belongs in secret_group_vars/pulsar.yml)"
                )
        elif not (_is_str(password) and _PASSWORD.fullmatch(password)):
            errors.append(
                f"{where}: the password (rabbitmq_password_{name}) must be at least 10 "
                "characters from A-Z a-z 0-9 . _ ~ -"
            )
    return errors, names


def _amqp_url_errors(where, url, user, resolved):
    match = _AMQP_URL.fullmatch(url) if _is_str(url) else None
    if not match:
        return [
            f"{where}: params.amqp_url must look like "
            "pyamqp://<user>:<password>@<host>:<port>/<vhost>"
        ]
    errors = []
    if match["user"] != user:
        errors.append(f"{where}: params.amqp_url must use the RabbitMQ user {user}")
    if match["vhost"] != f"/pulsar/{user}":
        errors.append(f"{where}: params.amqp_url must use the vhost /pulsar/{user}")
    if not resolved and not _password_ref(user).fullmatch(match["password"].strip()):
        errors.append(
            f"{where}: the password in params.amqp_url must be '{{{{ rabbitmq_password_{user} }}}}'"
        )
    return errors


def _params_errors(where, params):
    return [
        f"{where}: params key {key!r} is not allowed (lowercase letters, digits and '_', "
        f"not {' or '.join(_RESERVED_PARAMS)})"
        for key in params
        if not (_is_str(key) and _PARAM_KEY.fullmatch(key)) or key in _RESERVED_PARAMS
    ]


def _runner_errors(runners, users, resolved):
    errors, ids = [], set()
    if not isinstance(runners, list):
        return ["templates/galaxy/config/job_conf.yml: galaxy_jobconf.plugins must be a list"], ids
    for number, runner in enumerate(runners, start=1):
        where = f"templates/galaxy/config/job_conf.yml plugin {number}"
        if not isinstance(runner, dict) or not _is_str(runner.get("id")):
            errors.append(f"{where}: must be a mapping with 'id', 'load' and 'params'")
            continue
        runner_id = runner["id"]
        match = _RUNNER_ID.fullmatch(runner_id)
        if not match:
            errors.append(f"{where}: 'id' must match pulsar_eu_<id> with <id> in [a-z0-9_]+")
            continue
        where = f"templates/galaxy/config/job_conf.yml plugin '{runner_id}'"
        if runner_id in ids:
            errors.append(f"{where}: duplicate id")
        ids.add(runner_id)
        unknown = sorted(set(runner) - {"id", "load", "params"})
        errors.extend(f"{where}: unknown key '{key}'" for key in unknown)
        if runner.get("load") != RUNNER_LOAD:
            errors.append(f"{where}: 'load' must be {RUNNER_LOAD}")
        params = runner.get("params")
        if not isinstance(params, dict):
            errors.append(f"{where}: 'params' must be a mapping")
            continue
        errors.extend(_params_errors(where, params))
        user = f"galaxy_{match[1]}"
        if user not in users:
            errors.append(f"{where}: no RabbitMQ user {user} in group_vars/mq.yml")
        errors.extend(_amqp_url_errors(where, params.get("amqp_url"), user, resolved))
    return errors, ids


def _option_errors(options, resolved):
    errors, tags = [], set()
    where = "files/galaxy/config/user_preferences_extra_conf.yml"
    if not isinstance(options, list):
        return [f"{where}: remote_resources options must be a list"], tags
    for number, option in enumerate(options, start=1):
        if not (
            isinstance(option, list)
            and len(option) == 2
            and all(_is_str(part) for part in option)
            and _TAG.fullmatch(option[1])
        ):
            errors.append(
                f'{where}: option {number} must be ["<label>", <tag>] with <tag> in [A-Za-z0-9_.-]+'
            )
            continue
        if not resolved and _has_jinja(option[0]):
            errors.append(f"{where}: option {number}: the label must not contain Jinja ({{{{ {{%)")
        if option[1] in tags:
            errors.append(f"{where}: duplicate option {option[1]}")
        tags.add(option[1])
    return errors, tags


def _destination_errors(destinations, runner_ids, tags):
    where = "files/galaxy/tpv/destinations.yml.j2"
    if not isinstance(destinations, dict):
        return [f"{where}: destinations must be a mapping"]
    errors, required = [], set()
    for name, destination in destinations.items():
        if not (_is_str(name) and _DESTINATION.fullmatch(name)):
            errors.append(f"{where}: destination name {name!r} must match [A-Za-z0-9_]+")
        destination = destination if isinstance(destination, dict) else {}
        runner = destination.get("runner")
        if _is_str(runner) and runner.startswith("pulsar_eu_") and runner not in runner_ids:
            errors.append(
                f"{where} destination '{name}': runner {runner} is not in "
                "templates/galaxy/config/job_conf.yml"
            )
        scheduling = destination.get("scheduling")
        require = scheduling.get("require") if isinstance(scheduling, dict) else None
        required.update(tag for tag in require or [] if _is_str(tag))
    errors.extend(
        f"{where}: no destination requires the tag {tag} offered in "
        "files/galaxy/config/user_preferences_extra_conf.yml"
        for tag in sorted(tags - required)
    )
    return errors


def byoc_onboarding_errors(
    mq_users, runners, options, destinations=None, resolved=False, reserved=()
):
    """Return one message per problem in the onboarding files (see the module docstring).

    ``mq_users``: ``rabbitmq_users``; ``runners``: ``galaxy_jobconf.plugins``; ``options``:
    :func:`preference_options`; ``destinations``: TPV ``destinations`` (skipped if None);
    ``resolved``: values resolved by Ansible (deploy) rather than the committed files (CI);
    ``reserved``: RabbitMQ user names in use.
    """
    errors, users = _mq_user_errors(mq_users, resolved, set(reserved))
    runner_errors, runner_ids = _runner_errors(runners, users, resolved)
    option_errors, tags = _option_errors(options, resolved)
    errors += runner_errors + option_errors
    if destinations is not None:
        errors += _destination_errors(destinations, runner_ids, tags)
    return errors


def byoc_runners(runners, amqp_endpoint, galaxy_url, endpoints=()):
    """Point validated runners at this Galaxy (see the module docstring).

    ``amqp_endpoint``: ``host:port`` of this host's AMQPS listener; an ``amqp_url`` whose
    ``host:port`` is not one of ``endpoints`` is rebuilt as
    ``pyamqp://<user>:<password>@<amqp_endpoint>/<vhost>?ssl=1``.
    """
    result = []
    for runner in runners or []:
        params = dict(runner["params"])
        match = _AMQP_URL.fullmatch(params["amqp_url"])
        if f"{match['host']}:{match['port']}" not in endpoints:
            params["amqp_url"] = (
                f"pyamqp://{match['user']}:{match['password']}@{amqp_endpoint}/"
                f"{match['vhost']}?ssl=1"
            )
        params["galaxy_url"] = galaxy_url
        result.append({**runner, "params": params})
    return result


def byoc_secret_errors(secrets, mq_users):
    """Return one message per problem in the decrypted ``secret_group_vars`` variables."""
    if not isinstance(secrets, dict):
        return ["secret_group_vars: the decrypted files must be mappings of variables"]
    errors = [
        f"secret_group_vars: unexpected variable '{name}' (only rabbitmq_password_galaxy_<id>)"
        for name in sorted(secrets, key=str)
        if not (_is_str(name) and _SECRET_NAME.fullmatch(name))
    ]
    entries = mq_users if isinstance(mq_users, list) else []
    users = [user.get("user") for user in entries if isinstance(user, dict)]
    errors.extend(
        f"secret_group_vars: rabbitmq_password_{name} is missing (needed by group_vars/mq.yml)"
        for name in users
        if f"rabbitmq_password_{name}" not in secrets
    )
    errors.extend(
        f"secret_group_vars: {name} must be at least 10 characters from A-Z a-z 0-9 . _ ~ -"
        for name, value in sorted(secrets.items(), key=lambda item: str(item[0]))
        if not (_is_str(value) and _PASSWORD.fullmatch(value))
    )
    return errors


class FilterModule:
    def filters(self):
        return {
            "byoc_preference_options": preference_options,
            "byoc_onboarding_errors": byoc_onboarding_errors,
            "byoc_runners": byoc_runners,
            "byoc_secret_errors": byoc_secret_errors,
        }
