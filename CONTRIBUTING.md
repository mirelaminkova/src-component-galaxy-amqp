# Contributing

## Running the checks

All checks run locally through [pre-commit](https://pre-commit.com/) and again in CI
(`.github/workflows/checks.yml`) on every pull request to `main`.

### One-time setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pre-commit install --hook-type pre-commit --hook-type pre-push
```

The hooks then run automatically on `git commit` and `git push`.

### What runs when

| Stage        | Hooks                                                                                                   |
|--------------|---------------------------------------------------------------------------------------------------------|
| `pre-commit` | trailing whitespace / end-of-file fixers, yamllint, ruff (lint + format), ansible-lint, pytest, ast-grep rule tests |
| `pre-push`   | `semgrep --config p/default --config p/secrets --error`, `ast-grep scan`; any finding blocks the push   |

Run them by hand on the whole repository:

```bash
.venv/bin/pre-commit run --all-files
.venv/bin/pre-commit run --hook-stage pre-push --all-files
```

### Tests

```bash
.venv/bin/pytest
```

pytest enforces 100% branch coverage (`--cov-fail-under=100`, configured in
`pyproject.toml`). Tests live in `tests/unit/`:

- `tests/helpers/render.py` renders repo templates with plain Jinja2 set up like
  Ansible (strict undefined variables, `trim_blocks`, Ansible's filter plugins).
- `render_module()` renders a Python template (e.g. `templates/default_dispatch.py.j2`)
  into `tests/rendered/` and imports it, so Galaxy dynamic job rules can be unit
  tested. Rendered modules count towards coverage, so every branch of a rule needs a test.
- `render_yaml()` renders a YAML template and parses it.
  `tests/unit/test_config_templates.py` renders every `templates/galaxy/config/*.j2`
  and fails if a new config template has no rendering case.

### Linters

- yamllint uses `.yamllint`, which is compatible with ansible-lint's `production` profile.
- ansible-lint uses `.ansible-lint`; `.ansible-lint-ignore` lists accepted findings.
  ansible-lint installs the roles and collections from `requirements.yml` into
  `.ansible/` (git-ignored), so the first run needs network access.
- ast-grep rules live in `ast-grep/rules/` with tests in `ast-grep/rule-tests/`
  (`.venv/bin/ast-grep test --skip-snapshot-tests`).

### Versions

Tool versions are pinned in `requirements-dev.txt`. The local hooks in
`.pre-commit-config.yaml` (pytest, ansible-lint, semgrep, ast-grep) repeat those pins; update both
together. The Molecule scenario (`molecule/`) needs podman and runs in CI only.
