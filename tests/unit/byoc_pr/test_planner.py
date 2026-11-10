import pytest

from scripts.byoc_pr.editors import PATHS, SECRETS, EditError
from scripts.byoc_pr.endpoint import Endpoint, Settings
from scripts.byoc_pr.planner import build_changes, make_plan, presence, read_repo, render_diff
from scripts.byoc_pr.vault import Vault
from tests.unit.byoc_pr.conftest import make_request

ALICE = make_request()


def test_read_repo(repo_dir):
    texts = read_repo(repo_dir)
    assert set(texts) == set(PATHS)
    assert Vault.is_encrypted(texts[SECRETS])


def test_read_repo_missing_files(repo_dir):
    (repo_dir / SECRETS).unlink()
    assert read_repo(repo_dir)[SECRETS] == ""
    (repo_dir / "group_vars/mq.yml").unlink()
    with pytest.raises(EditError, match=r"group_vars/mq\.yml: not found"):
        read_repo(repo_dir)


def test_endpoint_names():
    endpoint = Endpoint(make_request(username="my-lab_01"))
    assert endpoint.id == "my_lab_01"
    assert endpoint.tag == "my-lab_01-pulsar"
    assert endpoint.branch == "byoc/my_lab_01"
    assert (endpoint.mq_user, endpoint.vhost) == ("galaxy_my_lab_01", "/pulsar/galaxy_my_lab_01")
    assert (endpoint.runner, endpoint.destination) == (
        "pulsar_eu_my_lab_01",
        "pulsar_my_lab_01_tpv",
    )


def test_plan_new_partial_and_collision(texts, vault):
    requests = [
        ALICE,
        make_request(galaxy_username="dave", username="egi01"),
        make_request(galaxy_username="mallory", username="alice_hpc"),
    ]
    plan = make_plan(requests, texts, vault)
    assert [endpoint.id for endpoint in plan.new] == ["alice_hpc"]
    assert plan.present == []
    assert plan.skipped == [
        (
            "dave",
            "endpoint 'egi01' partly present (files/galaxy/tpv/destinations.yml.j2, "
            "group_vars/mq.yml, templates/galaxy/config/job_conf.yml, "
            "secret_group_vars/pulsar.yml); fix by hand",
        ),
        ("mallory", "endpoint id 'alice_hpc' already requested by 'alice'"),
    ]


def test_without_vault_the_secrets_file_is_not_checked(texts):
    found = presence(texts, Endpoint(ALICE), None)
    assert SECRETS not in found
    assert not any(found.values())


def test_changes_are_idempotent(texts, vault):
    endpoint = Endpoint(ALICE)
    changes = build_changes(texts, endpoint, Settings(), vault)
    assert list(changes) == list(PATHS)
    after = {**texts, **changes}
    assert all(presence(after, endpoint, vault).values())
    plan = make_plan([ALICE], after, vault)
    assert (plan.new, [e.id for e in plan.present]) == ([], ["alice_hpc"])


def test_changes_without_vault_skip_secrets(texts):
    changes = build_changes(texts, Endpoint(ALICE), Settings(), None)
    assert SECRETS not in changes
    assert len(changes) == 4


def test_render_diff(texts, vault):
    endpoint = Endpoint(ALICE)
    diff = render_diff(texts, build_changes(texts, endpoint, Settings(), vault), endpoint)
    assert "+++ b/group_vars/mq.yml\n" in diff
    assert "+  - user: galaxy_alice_hpc\n" in diff
    assert diff.endswith(
        "~~~ secret_group_vars/pulsar.yml: re-encrypted with Ansible Vault, "
        "adds rabbitmq_password_galaxy_alice_hpc (value not shown)\n"
    )
    assert "$ANSIBLE_VAULT" not in diff
    assert ALICE.password not in diff
    dry = render_diff(texts, build_changes(texts, endpoint, Settings(), None), endpoint)
    assert dry.endswith("(needs --vault-password-file): rabbitmq_password_galaxy_alice_hpc\n")
