"""Command line: Galaxy ``byoc_pulsar`` forms -> infrastructure-playbook changes.

Two modes:

- default (for a CI pipeline): ``--repo-path`` edits the five files in an existing checkout
  of the target repository. No git commands, no push, no PR: the pipeline commits and opens
  the PR, using ``--summary-json`` for its title and body.
- ``--open-pr`` (optional): clone ``--target-repo`` and open one PR per new endpoint on branch
  ``byoc/<id>``; a dry run unless ``--apply`` is given.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from scripts.byoc_pr.editors import PATHS, EditError
from scripts.byoc_pr.endpoint import Endpoint, Settings
from scripts.byoc_pr.galaxy import GalaxyClient, GalaxyError, fetch_forms
from scripts.byoc_pr.github import CommandError, GitHub, Runner, pr_body, pr_title, run
from scripts.byoc_pr.planner import Plan, build_changes, make_plan, read_repo, render_diff
from scripts.byoc_pr.validation import ByocRequest, validate
from scripts.byoc_pr.vault import Vault, VaultError

log = logging.getLogger("byoc_pr")
API_KEY_ENV = "GALAXY_API_KEY"
VAULT_PASSWORD_FILE_ENV = "ANSIBLE_VAULT_PASSWORD_FILE"  # noqa: S105 - a variable name

EXIT_CHANGES = 0  # something new was written / opened (or would be, in a dry run)
EXIT_ERROR = 1  # an error; with several endpoints the others are still processed
EXIT_USAGE = 2  # bad command line (argparse)
EXIT_NOTHING_NEW = 3  # every valid request is already present or has an open PR

DONE = {"written", "would_write", "opened", "would_open"}


class UsageError(RuntimeError):
    """Missing or contradicting options."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.byoc_pr",
        description=(
            "Read the byoc_pulsar user preference form of all Galaxy users (admin API key) and "
            "add the new BYOC Pulsar endpoints to an infrastructure-playbook checkout "
            "(--repo-path), or open one PR per endpoint (--open-pr). Secrets are read from "
            f"files or the environment (${API_KEY_ENV}, ${VAULT_PASSWORD_FILE_ENV}), never from "
            f"the command line. Exit codes: {EXIT_CHANGES} changes, {EXIT_NOTHING_NEW} nothing "
            f"new, {EXIT_ERROR} error, {EXIT_USAGE} usage."
        ),
    )
    parser.add_argument("--galaxy-url", required=True, help="e.g. https://galaxy.example.org")
    parser.add_argument(
        "--api-key-file", type=Path, help=f"file with the admin API key (else ${API_KEY_ENV})"
    )
    parser.add_argument(
        "--vault-password-file",
        type=Path,
        help="Ansible Vault password of secret_group_vars/pulsar.yml "
        f"(else ${VAULT_PASSWORD_FILE_ENV}); needed to write",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--repo-path", type=Path, help="checkout to edit in place (default mode; no git at all)"
    )
    target.add_argument(
        "--open-pr",
        action="store_true",
        help="instead: clone --target-repo, one branch byoc/<id> and PR per endpoint",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    mode.add_argument("--apply", action="store_true", help="with --open-pr: push and open PRs")
    parser.add_argument(
        "--summary-json", type=Path, help="write a machine-readable summary (no secrets) here"
    )
    pr = parser.add_argument_group("--open-pr options")
    pr.add_argument(
        "--target-repo",
        default="mirelaminkova/infrastructure-playbook",
        help="GitHub repository for the PRs (default: %(default)s)",
    )
    pr.add_argument("--base-branch", default="master", help="default: %(default)s")
    pr.add_argument("--workdir", type=Path, help="clone of --target-repo (default: temporary)")
    defaults = Settings()
    runner = parser.add_argument_group("new runner settings")
    runner.add_argument("--mq-host", default=defaults.mq_host, help="default: %(default)s")
    runner.add_argument(
        "--mq-port", type=int, default=defaults.mq_port, help="default: %(default)s"
    )
    runner.add_argument(
        "--pulsar-galaxy-url",
        default=defaults.galaxy_url,
        help="galaxy_url of the new runners (default: %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def read_api_key(api_key_file: Path | None, environ: Mapping[str, str]) -> str:
    if api_key_file is not None:
        try:
            key = api_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise UsageError(f"cannot read {api_key_file}: {exc.strerror}") from None
    else:
        key = environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise UsageError(f"no Galaxy API key: set ${API_KEY_ENV} or pass --api-key-file")
    return key


def read_vault(path: Path | None, environ: Mapping[str, str]) -> Vault | None:
    env_path = environ.get(VAULT_PASSWORD_FILE_ENV, "").strip()
    path = path or (Path(env_path) if env_path else None)
    return Vault.from_file(path) if path else None


@dataclass
class Summary:
    """What the pipeline needs for its PR; never passwords or email addresses."""

    mode: str
    dry_run: bool
    galaxy_url: str
    target: str
    new: list[dict[str, Any]] = field(default_factory=list)
    present: list[dict[str, str]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    invalid: list[dict[str, Any]] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list, repr=False)
    error: str | None = None

    def add_plan(self, plan: Plan) -> None:
        for username, reason in plan.skipped:
            self.skipped.append({"galaxy_username": username, "reason": reason})
        for endpoint in plan.present:
            self.present.append(
                {"id": endpoint.id, "galaxy_username": endpoint.request.galaxy_username}
            )

    def add_new(self, endpoint: Endpoint, status: str, **extra: Any) -> None:
        entry = {
            "id": endpoint.id,
            "galaxy_username": endpoint.request.galaxy_username,
            "status": status,
            "tag": endpoint.tag,
            "rabbitmq_user": endpoint.mq_user,
            "vhost": endpoint.vhost,
            "runner": endpoint.runner,
            "destination": endpoint.destination,
            **extra,
        }
        self.new.append(entry)
        if status in DONE:
            self.endpoints.append(endpoint)

    def as_dict(self, exit_code: int) -> dict[str, Any]:
        pr = None
        if self.endpoints:
            pr = {"title": pr_title(self.endpoints), "body": pr_body(self.endpoints)}
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "galaxy_url": self.galaxy_url,
            "target": self.target,
            "exit_code": exit_code,
            "error": self.error,
            "new": self.new,
            "present": self.present,
            "skipped": self.skipped,
            "invalid": self.invalid,
            "pr": pr,
        }


def collect(client: GalaxyClient, summary: Summary) -> list[ByocRequest]:
    """Valid requests; invalid forms are reported (field and rule only) and skipped."""
    valid: list[ByocRequest] = []
    total = 0
    for form in fetch_forms(client):
        total += 1
        request, errors = validate(form.galaxy_user_id, form.galaxy_username, form.values)
        if request is None:
            print(f"skipped: Galaxy user {form.galaxy_username!r}: {'; '.join(errors)}")
            summary.invalid.append({"galaxy_username": form.galaxy_username, "errors": errors})
        else:
            valid.append(request)
    print(f"BYOC forms: {total} filled in, {len(valid)} valid")
    return valid


def report_plan(plan: Plan, target: str) -> None:
    for username, reason in plan.skipped:
        print(f"skipped: Galaxy user {username!r}: {reason}")
    for endpoint in plan.present:
        print(f"present: {endpoint.id} is already in {target}")


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = run,
    session: requests.Session | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.open_pr and args.repo_path is None:
        parser.error("--repo-path is required unless --open-pr is given")
    if args.apply and not args.open_pr:
        parser.error("--apply only applies to --open-pr")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    environ = os.environ if environ is None else environ
    dry_run = not args.apply if args.open_pr else args.dry_run
    target = f"{args.target_repo}@{args.base_branch}" if args.open_pr else str(args.repo_path)
    summary = Summary("open-pr" if args.open_pr else "files", dry_run, args.galaxy_url, target)
    settings = Settings(args.mq_host, args.mq_port, args.pulsar_galaxy_url)
    try:
        api_key = read_api_key(args.api_key_file, environ)
        vault = read_vault(args.vault_password_file, environ)
        if not dry_run and vault is None:
            what = "--open-pr --apply" if args.open_pr else "writing the files"
            raise UsageError(f"{what} needs a vault password file")
        log.info("reading byoc_pulsar forms from %s", args.galaxy_url)
        found = collect(GalaxyClient(args.galaxy_url, api_key, session=session), summary)
        if args.open_pr:
            code = open_prs(args, found, vault, settings, runner, summary)
        else:
            code = write_files(args.repo_path, found, vault, settings, dry_run, summary)
    except (UsageError, GalaxyError, VaultError, EditError, CommandError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        summary.error = str(exc)
        code = EXIT_ERROR
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary.as_dict(code), indent=2) + "\n")
    return code


def _exit_code(summary: Summary) -> int:
    if any(entry["status"] == "failed" for entry in summary.new):
        return EXIT_ERROR
    return EXIT_CHANGES if summary.endpoints else EXIT_NOTHING_NEW


def write_files(
    root: Path,
    found: list[ByocRequest],
    vault: Vault | None,
    settings: Settings,
    dry_run: bool,
    summary: Summary,
) -> int:
    """Add all new endpoints to the checkout at ``root`` (no git operations)."""
    texts = read_repo(root)
    plan = make_plan(found, texts, vault)
    report_plan(plan, str(root))
    summary.add_plan(plan)
    current = dict(texts)
    for endpoint in plan.new:
        who = endpoint.request.galaxy_username
        try:
            changes = build_changes(current, endpoint, settings, vault)
        except (EditError, VaultError) as exc:
            print(f"failed: {endpoint.id}: {exc}", file=sys.stderr)
            summary.add_new(endpoint, "failed", error=str(exc))
            continue
        if dry_run:
            print(f"new: {endpoint.id} (Galaxy user {who!r}) would be written to {root}")
            print(render_diff(current, changes, endpoint), end="")
        else:
            print(f"written: {endpoint.id} (Galaxy user {who!r})")
        current.update(changes)
        status = "would_write" if dry_run else "written"
        summary.add_new(endpoint, status, files=list(changes))
    if not dry_run:
        for path in PATHS:
            if current[path] != texts[path]:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(current[path], encoding="utf-8")
    return _exit_code(summary)


def open_prs(
    args: argparse.Namespace,
    found: list[ByocRequest],
    vault: Vault | None,
    settings: Settings,
    runner: Runner,
    summary: Summary,
) -> int:
    with contextlib.ExitStack() as stack:
        workdir = (
            args.workdir
            or Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="byoc-pr-"))) / "repo"
        )
        github = GitHub(args.target_repo, args.base_branch, workdir, runner)
        log.info("checking out %s@%s in %s", args.target_repo, args.base_branch, workdir)
        github.checkout_base()
        texts = read_repo(workdir)
        plan = make_plan(found, texts, vault)
        report_plan(plan, summary.target)
        summary.add_plan(plan)
        for endpoint in plan.new:
            _open_pr(github, texts, endpoint, settings, vault, not summary.dry_run, summary)
    return _exit_code(summary)


def _open_pr(github, texts, endpoint, settings, vault, apply, summary) -> None:
    who = endpoint.request.galaxy_username
    branch = endpoint.branch
    url = github.open_pr_url(branch)
    if url:
        print(f"open PR: {endpoint.id} (Galaxy user {who!r}) already has {url}, reused")
        summary.add_new(endpoint, "pr_reused", branch=branch, pr_url=url)
        return
    if github.branch_exists(branch):
        print(f"skipped: {endpoint.id}: branch {branch} exists without an open PR; delete it")
        summary.add_new(endpoint, "branch_exists", branch=branch)
        return
    try:
        changes = build_changes(texts, endpoint, settings, vault)
    except (EditError, VaultError) as exc:
        print(f"failed: {endpoint.id}: {exc}", file=sys.stderr)
        summary.add_new(endpoint, "failed", branch=branch, error=str(exc))
        return
    if not apply:
        print(f"new: {endpoint.id} (Galaxy user {who!r}) would be pushed to {branch}")
        print(render_diff(texts, changes, endpoint), end="")
        summary.add_new(endpoint, "would_open", branch=branch, files=list(changes))
        return
    try:
        url = github.publish(endpoint, changes)
    except CommandError as exc:
        print(f"failed: {endpoint.id}: {exc}", file=sys.stderr)
        summary.add_new(endpoint, "failed", branch=branch, error=str(exc))
        return
    print(f"opened: {endpoint.id} (Galaxy user {who!r}): {url}")
    summary.add_new(endpoint, "opened", branch=branch, pr_url=url, files=list(changes))
