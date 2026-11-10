import sys

import pytest

from scripts.byoc_pr.endpoint import Endpoint, Settings
from scripts.byoc_pr.github import (
    CommandError,
    GitHub,
    commit_message,
    pr_body,
    pr_title,
    run,
)
from scripts.byoc_pr.planner import build_changes, read_repo
from tests.unit.byoc_pr.conftest import REPO, git, make_request

ENDPOINT = Endpoint(make_request())


def test_run_returns_stdout_and_passes_stdin():
    assert run([sys.executable, "-c", "import sys; print(sys.stdin.read())"], input="hi") == "hi\n"


def test_run_failure_shows_tail_of_stderr():
    script = "import sys; sys.stderr.write('x' * 600 + 'boom'); sys.exit(3)"
    with pytest.raises(CommandError, match=r"failed \(exit 3\): x+boom$") as excinfo:
        run([sys.executable, "-c", script])
    assert len(str(excinfo.value)) < 600


def test_pr_text():
    body = pr_body([ENDPOINT])
    assert pr_title([ENDPOINT]) == "Add BYOC pulsar endpoint alice_hpc"
    assert "Requested by Galaxy user: `alice`" in body
    assert "16 cores, 64 GB memory, 0-1 GPUs" in body
    assert "RabbitMQ user `galaxy_alice_hpc` on vhost `/pulsar/galaxy_alice_hpc`" in body
    assert ENDPOINT.request.password not in body + commit_message([ENDPOINT])
    two = [ENDPOINT, Endpoint(make_request(galaxy_username="bob", username="bob-lab"))]
    assert pr_title(two) == "Add BYOC pulsar endpoints alice_hpc, bob_lab"
    assert "Requested by Galaxy user: `bob`" in pr_body(two)
    assert "Requested by Galaxy users alice, bob" in commit_message(two)


def test_clone_then_fetch(tmp_path, fake_gh):
    github = GitHub(REPO, "master", tmp_path / "work", fake_gh)
    github.checkout_base()
    assert (tmp_path / "work/group_vars/mq.yml").is_file()
    github.checkout_base()
    commands = [args[:3] for args, _ in fake_gh.calls]
    assert ["gh", "repo", "clone"] in commands
    assert ["git", "fetch", "--quiet"] in commands


def test_publish_pushes_branch_and_opens_pr(tmp_path, fake_gh, vault, origin):
    github = GitHub(REPO, "master", tmp_path / "work", fake_gh)
    github.checkout_base()
    assert github.open_pr_url(ENDPOINT.branch) is None
    assert not github.branch_exists(ENDPOINT.branch)
    texts = read_repo(github.workdir)
    changes = build_changes(texts, ENDPOINT, Settings(), vault)
    url = github.publish(ENDPOINT, changes)
    assert url == f"https://github.com/{REPO}/pull/1"
    assert github.open_pr_url(ENDPOINT.branch) == url
    assert github.branch_exists(ENDPOINT.branch)
    changed = git("diff", "--name-only", "master", "byoc/alice_hpc", cwd=origin).split()
    assert sorted(changed) == sorted(changes)
    created = fake_gh.created[0]
    assert created["args"][created["args"].index("--base") + 1] == "master"
    assert created["body"] == pr_body([ENDPOINT])
    assert git("log", "-1", "--format=%s", "byoc/alice_hpc", cwd=origin).strip() == pr_title(
        [ENDPOINT]
    )
