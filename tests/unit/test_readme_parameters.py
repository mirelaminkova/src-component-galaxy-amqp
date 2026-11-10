"""The README's SRC catalog item parameter table matches the playbook (issue #30).

Every ``src_galaxy_*`` component parameter the playbook reads (in the playbook, its vars,
tasks, templates, files, group_vars and filter plugins; not in tests or Molecule) must have a
row in the "Component parameters" table of README.md, and every row must name a parameter
the playbook reads, so the table cannot drift from the code.
"""

import compileall
import re

from tests.helpers.ansible_vars import REPO_ROOT

README = REPO_ROOT / "README.md"
PLAYBOOK_SOURCES = [
    "galaxysrv.yml",
    "vars",
    "tasks",
    "templates",
    "files",
    "group_vars",
    "filter_plugins",
]
HEADER = ["Parameter key", "(Initial) value", "Overwritable", "Required", "Description"]
# Text sources only: bytecode (filter_plugins/__pycache__/*.pyc) decodes to names with
# trailing marshal bytes, e.g. `src_galaxy_vault_passwordz`.
TEXT_SUFFIXES = {".yml", ".yaml", ".j2", ".py", ".cfg", ".conf"}
# A parameter name. `\b(?!\.(?:ya?ml|py|j2)\b)` skips file names such as
# vars/src_galaxy_vars.yml but keeps attribute access such as `src_galaxy_foo.key`.
PARAMETER = re.compile(r"\bsrc_(?:galaxy_[a-z0-9_]+|ibridges)\b(?!\.(?:ya?ml|py|j2)\b)")


def _playbook_files():
    for source in PLAYBOOK_SOURCES:
        path = REPO_ROOT / source
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        yield from (
            p
            for p in files
            if p.is_file() and p.suffix in TEXT_SUFFIXES and "__pycache__" not in p.parts
        )


def _parameters_read():
    names = set()
    for path in _playbook_files():
        names |= set(PARAMETER.findall(path.read_text(errors="ignore")))
    return names


def _table():
    """Rows of the table under '### Component parameters', as lists of cells."""
    section = README.read_text().split("### Component parameters", 1)[1]
    lines = section.split("\n\n|", 1)[1].split("\n\n", 1)[0]
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in ("|" + lines).splitlines()
    ]
    assert rows[0] == HEADER
    return {re.sub(r"[`*\\]", "", row[0]): row for row in rows[2:]}


def test_playbook_files_are_found():
    assert {
        "src_galaxy_version",
        "src_galaxy_vault_password",
        "src_galaxy_cd_interval",
    } <= _parameters_read()
    assert "src_galaxy_vars" not in _parameters_read()


def test_bytecode_is_not_scanned():
    # Regression: tests that import the filter plugins write __pycache__/*.pyc.
    assert compileall.compile_dir(REPO_ROOT / "filter_plugins", quiet=1)
    assert "src_galaxy_vault_passwordz" not in _parameters_read()
    assert not any("__pycache__" in p.parts for p in _playbook_files())


def test_attribute_access_and_file_names():
    assert PARAMETER.findall("{{ src_galaxy_foo.key }} vars/src_galaxy_vars.yml") == [
        "src_galaxy_foo"
    ]


def test_readme_table_lists_every_parameter_the_playbook_reads():
    missing = _parameters_read() - set(_table())
    assert not missing, f"README parameter table lacks: {sorted(missing)}"


def test_readme_table_lists_only_parameters_the_playbook_reads():
    rows = {name for name in _table() if PARAMETER.fullmatch(name)}
    unknown = rows - _parameters_read()
    assert not unknown, (
        f"README parameter table lists parameters the playbook does not read: {sorted(unknown)}"
    )
    assert "src_galaxy_pulsar_relay_configs" not in _table()


def test_readme_table_rows_are_complete():
    for name, row in _table().items():
        assert len(row) == len(HEADER), name
        assert row[2] in {"yes", "no"}, name
        assert row[3] in {"yes", "no"}, name
        assert row[4], name


def test_pulsar_parameters_start_empty():
    # Placeholders such as https://REPLACE / your_pulsar_env create a fake Pulsar and fail
    # the deployment at "Invalid Pulsar configuration".
    table = _table()
    for name in (
        "src_galaxy_pulsar_configs",
        "src_galaxy_pulsar_tool_mappings",
        "src_galaxy_pulsar_amqp_configs",
    ):
        assert table[name][1] in {"", "(empty)"}, name


def test_vault_password_is_a_secret_fixed_parameter():
    row = _table()["src_galaxy_vault_password"]
    assert row[2] == "no"
    assert "secret" in row[4].lower()
