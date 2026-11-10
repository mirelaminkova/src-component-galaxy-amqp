"""The five file editors, on copies of the usegalaxy.eu files (see conftest.py)."""

import difflib

import pytest

from scripts.byoc_pr import editors
from scripts.byoc_pr.editors import (
    DESTINATIONS,
    JOB_CONF,
    MQ,
    SECRETS,
    USER_PREFERENCES,
    EditError,
)
from scripts.byoc_pr.endpoint import Endpoint, Settings
from scripts.byoc_pr.vault import Vault
from tests.unit.byoc_pr.conftest import make_request

ENDPOINT = Endpoint(make_request())
SETTINGS = Settings()


def change(old: str, new: str) -> tuple[list[str], list[str]]:
    """(context lines before the insertion, inserted lines); asserts a pure insertion."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    opcodes = [op for op in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes()]
    edits = [op for op in opcodes if op[0] != "equal"]
    assert len(edits) == 1
    tag, i1, _, j1, j2 = edits[0]
    assert tag == "insert"
    return [*old_lines[max(0, i1 - 2) : i1], "|", *old_lines[i1 : i1 + 2]], new_lines[j1:j2]


def test_user_preferences(texts):
    old = texts[USER_PREFERENCES]
    assert not editors.user_preferences_has(old, ENDPOINT)
    new = editors.user_preferences_add(old, ENDPOINT, SETTINGS)
    context, inserted = change(old, new)
    assert inserted == ['                  - ["BYOC Pulsar - alice-hpc", alice-hpc-pulsar]']
    assert context == [
        '                  - ["Clermont-Ferrand (France) - UCA", uca-pulsar]',
        '                  - ["UFZ Leipzig (Germany)", ufz-eve-pulsar]',
        "|",
        "",
        "    ega_account:",
    ]
    assert editors.user_preferences_has(new, ENDPOINT)


def test_destinations(texts):
    old = texts[DESTINATIONS]
    assert not editors.destinations_has(old, ENDPOINT)
    new = editors.destinations_add(old, ENDPOINT, SETTINGS)
    _, inserted = change(old, new)
    block = [
        "  pulsar_alice_hpc_tpv:",
        "    inherits: pulsar_default",
        "    runner: pulsar_eu_alice_hpc",
        "    max_accepted_cores: 16",
        "    max_accepted_mem: 64",
        "    min_accepted_gpus: 0",
        "    max_accepted_gpus: 1",
        "    scheduling:",
        "      require:",
        "        - alice-hpc-pulsar",
    ]
    assert [line for line in inserted if line] == block
    lines = new.splitlines()
    start = lines.index(block[0])
    # at the end of the PULSAR DESTINATIONS section, one blank line on each side
    assert lines[start - 2].lstrip().startswith("singularity_run_extra_arguments:")
    assert lines[start - 1] == ""
    assert lines[start + len(block) : start + len(block) + 3] == [
        "",
        "  #############################",
        "  # LOCAL CONDOR DESTINATIONS #",
    ]
    assert editors.destinations_has(new, ENDPOINT)


@pytest.mark.parametrize("line", ["  pulsar_alice_hpc_tpv:\n", "    runner: pulsar_eu_alice_hpc\n"])
def test_destinations_has_by_key_or_runner(line):
    assert editors.destinations_has("destinations:\n" + line, ENDPOINT)
    assert not editors.destinations_has("destinations:\n  #" + line, ENDPOINT)


def test_destinations_section_at_end_of_file():
    text = "destinations:\n  # PULSAR DESTINATIONS #\n  pulsar_default:\n    runner: x"
    new = editors.destinations_add(text, ENDPOINT, SETTINGS)
    assert new.startswith(text + "\n\n  pulsar_alice_hpc_tpv:\n")
    assert new.endswith("        - alice-hpc-pulsar\n\n")


def test_destinations_banner_without_rule_line_and_no_blank():
    text = (
        "destinations:\n  # PULSAR DESTINATIONS #\n  pulsar_default:\n    runner: x\n"
        "  # LOCAL CONDOR DESTINATIONS #\n"
    )
    new = editors.destinations_add(text, ENDPOINT, SETTINGS)
    assert "        - alice-hpc-pulsar\n\n  # LOCAL CONDOR DESTINATIONS #\n" in new
    assert "    runner: x\n\n  pulsar_alice_hpc_tpv:\n" in new


def test_destinations_without_pulsar_section():
    with pytest.raises(EditError, match="PULSAR DESTINATIONS"):
        editors.destinations_add("destinations: {}\n", ENDPOINT, SETTINGS)


def test_mq(texts):
    old = texts[MQ]
    assert not editors.mq_has(old, ENDPOINT)
    new = editors.mq_add(old, ENDPOINT, SETTINGS)
    context, inserted = change(old, new)
    assert inserted == [
        "  - user: galaxy_alice_hpc",
        "    password: '{{ rabbitmq_password_galaxy_alice_hpc }}'",
        "    vhost: /pulsar/galaxy_alice_hpc",
    ]
    assert context == [
        "    password: '{{ rabbitmq_password_galaxy_ufz_eve01 }}'",
        "    vhost: /pulsar/galaxy_ufz_eve01",
        "|",
        "",
        "rabbitmq_plugins:",
    ]
    assert editors.mq_has(new, ENDPOINT)


def test_mq_has_matches_vhost():
    text = "rabbitmq_users:\n  - user: other\n    vhost: /pulsar/galaxy_alice_hpc\n"
    assert editors.mq_has(text, ENDPOINT)


def test_mq_last_item_at_end_without_newline():
    new = editors.mq_add("rabbitmq_users:\n  - user: a\n    vhost: /a", ENDPOINT, SETTINGS)
    assert new.startswith(
        "rabbitmq_users:\n  - user: a\n    vhost: /a\n  - user: galaxy_alice_hpc\n"
    )


@pytest.mark.parametrize(
    "text", ["other: 1\n", "rabbitmq_users: none\n", "- a\n", "rabbitmq_users: []\n"]
)
def test_mq_layout_errors(text):
    with pytest.raises(EditError, match=MQ):
        editors.mq_add(text, ENDPOINT, SETTINGS)


def test_flow_style_list_is_refused():
    with pytest.raises(EditError, match="expected a block list item"):
        editors.mq_add("rabbitmq_users: [{user: a}]\n", ENDPOINT, SETTINGS)


def test_invalid_yaml():
    with pytest.raises(EditError, match="not valid YAML: "):
        editors.mq_has("a: [\n", ENDPOINT)


def test_job_conf(texts):
    old = texts[JOB_CONF]
    assert not editors.job_conf_has(old, ENDPOINT)
    new = editors.job_conf_add(old, ENDPOINT, SETTINGS)
    context, inserted = change(old, new)
    assert inserted == [
        "    - id: pulsar_eu_alice_hpc",
        "      load: galaxy.jobs.runners.pulsar:PulsarMQJobRunner",
        "      params:",
        '        amqp_url: "pyamqp://galaxy_alice_hpc:{{ rabbitmq_password_galaxy_alice_hpc }}'
        '@mq.galaxyproject.eu:5671//pulsar/galaxy_alice_hpc?ssl=1"',
        '        galaxy_url: "https://usegalaxy.eu"',
        "        manager: production",
        '        amqp_acknowledge: "true"',
        "        amqp_ack_republish_time: 300",
        "        amqp_consumer_timeout: 2.0",
        '        amqp_publish_retry: "true"',
        "        amqp_publish_retry_max_retries: 60",
    ]
    assert context[1] == '        galaxy_url: "https://usegalaxy.eu"'
    assert context[2:] == ["|", "", ""]
    assert editors.job_conf_has(new, ENDPOINT)


def test_job_conf_settings_and_has_by_vhost():
    text = "galaxy_jobconf:\n  plugins:\n    - id: local\n"
    settings = Settings(mq_host="mq.example.org", mq_port=5672, galaxy_url="https://g.example")
    new = editors.job_conf_add(text, ENDPOINT, settings)
    assert "@mq.example.org:5672//pulsar/galaxy_alice_hpc?ssl=1" in new
    assert 'galaxy_url: "https://g.example"' in new
    other_id = new.replace("id: pulsar_eu_alice_hpc", "id: renamed")
    assert editors.job_conf_has(other_id, ENDPOINT)
    prefix = new.replace("galaxy_alice_hpc?", "galaxy_alice_hpc2?").replace(
        "id: pulsar_eu_alice_hpc", "id: renamed"
    )
    assert not editors.job_conf_has(prefix, ENDPOINT)


@pytest.mark.parametrize(
    "text", ["galaxy_jobconf: {}\n", "galaxy_jobconf:\n  plugins: x\n", "[]\n"]
)
def test_job_conf_layout_errors(text):
    with pytest.raises(EditError, match=r"galaxy_jobconf\.plugins"):
        editors.job_conf_has(text, ENDPOINT)


@pytest.mark.parametrize(
    "text",
    [
        "other: 1\n",
        "preferences:\n  distributed_compute:\n    inputs:\n      - name: x\n",
        "preferences: 3\n",
    ],
)
def test_user_preferences_layout_errors(text):
    with pytest.raises(EditError, match="remote_resources"):
        editors.user_preferences_has(text, ENDPOINT)


def test_comment_lines_before_next_section_stay_with_it():
    text = (
        "preferences:\n"
        "    distributed_compute:\n"
        "        inputs:\n"
        "            - name: remote_resources\n"
        "              options:\n"
        "                  - [A, a]\n"
        "                  # - [B, b]\n"
        "\n"
        "    # comment of the next section\n"
        "    next: {}\n"
    )
    new = editors.user_preferences_add(text, ENDPOINT, SETTINGS)
    assert (
        '# - [B, b]\n                  - ["BYOC Pulsar - alice-hpc", alice-hpc-pulsar]\n\n' in new
    )


@pytest.mark.parametrize(
    ("function", "path"),
    [
        (editors.user_preferences_add, USER_PREFERENCES),
        (editors.mq_add, MQ),
        (editors.job_conf_add, JOB_CONF),
    ],
)
def test_result_is_verified(monkeypatch, texts, function, path):
    monkeypatch.setattr(editors, "_insert_after_last_item", lambda text, *args: text)
    with pytest.raises(EditError, match="did not parse back"):
        function(texts[path], ENDPOINT, SETTINGS)


# secret_group_vars/pulsar.yml


def test_secrets(texts, vault):
    old = texts[SECRETS]
    assert editors.secrets_has(old, Endpoint(make_request(username="eosc01")), vault)
    assert not editors.secrets_has(old, ENDPOINT, vault)
    new = editors.secrets_add(old, ENDPOINT, vault)
    assert Vault.is_encrypted(new)
    assert "AlicePuls4rPassw0rd" not in new
    plain = vault.decrypt(new)
    assert (
        plain == vault.decrypt(old) + 'rabbitmq_password_galaxy_alice_hpc: "AlicePuls4rPassw0rd"\n'
    )
    assert editors.secrets_has(new, ENDPOINT, vault)


def test_numeric_password_stays_a_string(vault):
    endpoint = Endpoint(make_request(password="1234567890"))
    new = editors.secrets_add(vault.encrypt("a: 1"), endpoint, vault)
    assert vault.decrypt(new) == 'a: 1\nrabbitmq_password_galaxy_alice_hpc: "1234567890"\n'


@pytest.mark.parametrize("plain", [None, "", "\n", "---\n"])
def test_secrets_new_or_empty_file(vault, plain):
    text = "" if plain is None else vault.encrypt(plain)
    assert not editors.secrets_has(text, ENDPOINT, vault)
    new = editors.secrets_add(text, ENDPOINT, vault)
    assert vault.decrypt(new).endswith(
        'rabbitmq_password_galaxy_alice_hpc: "AlicePuls4rPassw0rd"\n'
    )
    assert vault.decrypt(new).startswith("---\n")


def test_plaintext_secrets_file_is_refused(vault):
    with pytest.raises(EditError, match="refusing to write a password in plain text"):
        editors.secrets_add("rabbitmq_password_x: y\n", ENDPOINT, vault)


def test_secrets_not_a_mapping(vault):
    with pytest.raises(EditError, match="not a YAML mapping"):
        editors.secrets_has(vault.encrypt("- a\n"), ENDPOINT, vault)


def test_secrets_parse_error_does_not_show_content(vault):
    with pytest.raises(EditError) as excinfo:
        editors.secrets_has(vault.encrypt("key: [SecretValue12\n"), ENDPOINT, vault)
    assert str(excinfo.value) == f"{SECRETS}: not valid YAML"


def test_secrets_variable_already_set(vault):
    endpoint = Endpoint(make_request())
    with pytest.raises(EditError, match="rabbitmq_password_galaxy_alice_hpc is already set"):
        editors.secrets_add(
            vault.encrypt("rabbitmq_password_galaxy_alice_hpc: old\n"), endpoint, vault
        )
