# Contributing

## Running the checks

All checks run locally through [pre-commit](https://pre-commit.com/) and again in CI
(`.github/workflows/checks.yml`) on every pull request to `main`.

### One-time setup

The dev tooling needs **Python >= 3.13** (ansible-core 2.22 requires it; the version CI
uses is in `.python-version`). On Ubuntu 24.04, whose `python3` is 3.12, get one with e.g.
`uv python install 3.13` or the deadsnakes PPA.

```bash
python3.13 -m venv .venv
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
- `render_template()` / `render_yaml()` load `templates/<name>` by its relative name and
  any other template (e.g. `files/galaxy/tpv/destinations.yml.j2`) by its repository path.
- `render_module()` renders a Python template into `tests/rendered/` and imports it, so
  Galaxy dynamic job rules can be unit tested. Rendered modules count towards coverage,
  so every branch of a rule needs a test.
- `tests/unit/test_tpv.py` renders the TPV config (`files/galaxy/tpv/`) and `job_conf.yml`,
  runs TPV's own linter (`tpv lint`) and routes jobs with TPV's mapper (what `tpv dry-run`
  does). TPV and Galaxy's packages (`total-perspective-vortex`, `galaxy-app`) are in
  `requirements-dev.txt`; their versions match what the playbook deploys.
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
`.pre-commit-config.yaml` (pytest, ansible-lint, ast-grep) repeat those pins; update both
together. semgrep is pinned only in `.pre-commit-config.yaml`: its dependencies conflict with
Galaxy's, so it runs from pre-commit's own environment. The Molecule scenario (`molecule/`) needs podman and runs in CI only.
