"""Decide which BYOC endpoints are new and build their file changes."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from scripts.byoc_pr.editors import CHECKS, EDITS, PATHS, SECRETS, EditError, secrets_add
from scripts.byoc_pr.editors import secrets_has as _secrets_has
from scripts.byoc_pr.endpoint import Endpoint, Settings
from scripts.byoc_pr.validation import ByocRequest
from scripts.byoc_pr.vault import Vault


def read_repo(root: Path) -> dict[str, str]:
    """The five target files of a checkout; a missing secrets file reads as empty."""
    texts: dict[str, str] = {}
    for path in PATHS:
        file = root / path
        if not file.is_file():
            if path == SECRETS:
                texts[path] = ""
                continue
            raise EditError(f"{path}: not found in the target repository")
        texts[path] = file.read_text(encoding="utf-8")
    return texts


@dataclass
class Plan:
    new: list[Endpoint] = field(default_factory=list)
    present: list[Endpoint] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (Galaxy username, reason)


def presence(texts: dict[str, str], endpoint: Endpoint, vault: Vault | None) -> dict[str, bool]:
    """Which target files already contain the endpoint (secrets only with a vault password)."""
    found = {path: check(texts[path], endpoint) for path, check in CHECKS.items()}
    if vault is not None:
        found[SECRETS] = _secrets_has(texts[SECRETS], endpoint, vault)
    return found


def make_plan(requests: list[ByocRequest], texts: dict[str, str], vault: Vault | None) -> Plan:
    plan = Plan()
    owners: dict[str, ByocRequest] = {}
    for request in requests:
        endpoint = Endpoint(request)
        who = request.galaxy_username
        if endpoint.id in owners:
            first = owners[endpoint.id].galaxy_username
            plan.skipped.append(
                (who, f"endpoint id {endpoint.id!r} already requested by {first!r}")
            )
            continue
        owners[endpoint.id] = request
        found = presence(texts, endpoint, vault)
        if all(found.values()):
            plan.present.append(endpoint)
        elif any(found.values()):
            where = ", ".join(path for path, hit in found.items() if hit)
            plan.skipped.append(
                (who, f"endpoint {endpoint.id!r} partly present ({where}); fix by hand")
            )
        else:
            plan.new.append(endpoint)
    return plan


def build_changes(
    texts: dict[str, str], endpoint: Endpoint, settings: Settings, vault: Vault | None
) -> dict[str, str]:
    """New contents of the changed files; the secrets file only with a vault password."""
    changes = {path: edit(texts[path], endpoint, settings) for path, edit in EDITS.items()}
    if vault is not None:
        changes[SECRETS] = secrets_add(texts[SECRETS], endpoint, vault)
    return {path: changes[path] for path in PATHS if path in changes}


def render_diff(texts: dict[str, str], changes: dict[str, str], endpoint: Endpoint) -> str:
    """Unified diff of the plain files; the vault file is only described."""
    out: list[str] = []
    for path in PATHS:
        if path == SECRETS:
            continue
        out.extend(
            difflib.unified_diff(
                texts[path].splitlines(keepends=True),
                changes[path].splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    if SECRETS in changes:
        note = f"re-encrypted with Ansible Vault, adds {endpoint.password_var} (value not shown)"
    else:
        note = f"not changed in this dry run (needs --vault-password-file): {endpoint.password_var}"
    out.append(f"~~~ {SECRETS}: {note}\n")
    return "".join(out)
