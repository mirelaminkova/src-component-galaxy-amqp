# BYOC onboarding contract fixtures (issue #21)

Used by `tests/unit/test_byoc_onboarding.py`. Kept byte-for-byte (excluded from the
whitespace fixers, yamllint and ansible-lint).

* `base/`: the onboarding files in this repository's layout, with one endpoint (`sk01`)
  already onboarded. `base/secret_group_vars/pulsar.yml` is encrypted with
  `vault-password.txt`.
* `onboard-egi01.diff`: modelled on usegalaxy-eu/infrastructure-playbook#872 (literal
  workspace name in `amqp_url` and `galaxy_url`).
* `onboard-eosc01.diff`: modelled on usegalaxy-eu/infrastructure-playbook#1384
  (`{{ _galaxy_server_fqdn }}` / `{{ _galaxy_rabbitmq_port }}` in `amqp_url`).
* `vault-password.txt`: a **throwaway** Vault password, used only for these fixtures.
  The passwords inside the fixture Vault files are throwaway test values too.

The diffs apply in this order with `git apply`. To change them, edit a copy of `base/`
(decrypt/re-encrypt the Vault file with `ansible-vault` and `vault-password.txt`) and
regenerate them with `git diff --no-index`.
