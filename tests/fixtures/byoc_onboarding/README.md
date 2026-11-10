# BYOC onboarding contract fixtures (issue #21)

Used by `tests/unit/test_byoc_onboarding.py`. Kept byte-for-byte (excluded from the
whitespace fixers, yamllint and ansible-lint).

* `eu-872-egi01/`, `eu-1384-eosc01/`: the lines usegalaxy-eu/infrastructure-playbook#872 and
  #1384 add to each file, copied verbatim from the PR diffs (`gh pr diff`), one file per
  target file (named after it). The tests insert them into copies of this repository's
  files above the anchors listed in `tests/helpers/onboarding.py` (`ANCHORS`), the place
  usegalaxy.eu's PRs insert them. One change: #872's option line is indented 18 spaces
  instead of 10, like usegalaxy.eu's current file and #1384 (a test shows that 10 spaces
  break the YAML).
* `vault-password.txt`: a **throwaway** Vault password, used only for these fixtures. The
  tests create `secret_group_vars/pulsar.yml` with it, holding throwaway passwords.
