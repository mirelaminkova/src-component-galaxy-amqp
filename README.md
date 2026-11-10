# Galaxy Server on SURF ResearchCloud

This repo provides the Ansible playbook for a [Galaxy](https://galaxyproject.org/) server component on SURF ResearchCloud (SRC) 🏄‍♀️.

Also see the [official Galaxy training manual for SRC](https://training.galaxyproject.org/training-material/topics/admin/tutorials/surf-research-cloud-galaxy/tutorial.html).

### Prerequisites

* Assumes Nginx is already installed on the workspace via the SURF [Nginx component](https://gitlab.com/rsc-surf-nl/plugins/plugin-nginx).
* The official [Galaxy Ansible role](https://github.com/galaxyproject/ansible-galaxy)
* The official [Galaxy postgres](https://github.com/galaxyproject/ansible-postgresql) and [postgres_objects](https://github.com/galaxyproject/ansible-postgresql-objects) roles for database management
* The official [Galaxy CVMFS role](https://training.galaxyproject.org/training-material/topics/admin/tutorials/cvmfs/tutorial.html) and [Apptainer](https://training.galaxyproject.org/training-material/topics/admin/tutorials/apptainer/tutorial.html) provided by the Galaxy community
* Assumes Nginx is already installed on the workspace (via the SURF Nginx component)

## Overview

### Services

* Postgres is installed on the same host as Galaxy.
* Galaxy is started the first time via `/usr/local/bin/galaxyctl start`. After that, it can be managed using `systemctl`, e.g. with `systemctl status galaxy.target`.
  * Galaxy runs on `localhost:8080`.
* An Nginx reverse proxy is started serving the Galaxy application on `https://<workspace_fqdn>`, providing SRAM authentication (see below).
  * Nginx proxies port 443 to localhost:8080
* If `src_galaxy_interactive_tools` is enabled, the [gx-it-proxy](https://github.com/galaxyproject/gx-it-proxy) application is started on port `8001`.
* If `src_galaxy_enable_tus`, [`tusd`](https://training.galaxyproject.org/training-material/topics/admin/tutorials/tus/tutorial.html) is started on port `8002`.
* If `src_galaxy_rabbitmq` is enabled, RabbitMQ listens for AMQPS on `src_galaxy_rabbitmq_port` (default `5671`) and for plain AMQP on `127.0.0.1:5672` only. See [RabbitMQ](#rabbitmq-for-remote-pulsar-servers).

### Authentication

Galaxy is configured to let the webserver (Nginx) handle authentication. The webserver uses OpenID connect to authenticate against SRAM.

Any members of the workspace's Collaborative Organisation (CO) will be able to authenticate using the authentication mechanism of their institution (Single Sign-On).

Galaxy is configured such that members of the CO that are in the SRAM workspace admin group (`src_co_admin`) will be Galaxy administrators.

Note: only members of the `src_co_admin` group are given permission to install tools on the Galaxy instance.

### Logging in via SSH

The Galaxy VM will be accessible via SSH for users in the Collaborative Organization. However, only users in the CO admin group
(configured by the `src_galaxy_co_admin_group` parameter, default: `src_co_admin`) will be able to `sudo` (this will require entering the user's SRAM TOTP). Other users are normal users.

The Galaxy application listening on localhost will expect a secret key in the request header that Nginx is configured to pass on when reverse proxying; as non-admin users
on the Galaxy machine won't have access to this secret key, they cannot directly query Galaxy on localhost.

### Connecting to Pulsar

A Galaxy machine can be connected to Pulsar inside the SRC, following the [SRC component Pulsar](https://github.com/ErasmusMC-Bioinformatics/src-component-pulsar) instructions.

When at least one Pulsar destination is configured (`src_galaxy_pulsar_configs`), users get a **Use distributed compute resources** select in their preferences (User → Preferences → Manage information). It lists "default - Galaxy will decide where to put your job" plus one entry per destination, labelled with the config's `label` key or else the host of its `url`. Jobs of a user who picked a destination are sent there by the `default_dispatcher` job rule, which is then `execution.default` in `job_conf.yml`. Some jobs always keep their normal destination: jobs of anonymous users, interactive tools (Docker), uploads (`upload1`, `__DATA_FETCH__`), data sources, data managers, user-defined tools, expression and other Galaxy-internal tools, and tools pinned by `src_galaxy_pulsar_tool_mappings`. A choice whose destination no longer exists falls back to the default.

Remote Pulsar servers can also be connected over a message queue (`src_galaxy_pulsar_amqp_configs`, Galaxy's `PulsarMQJobRunner`), so that the Pulsar server needs no inbound port; see [Pulsar over AMQP](#pulsar-over-amqp). These destinations appear in the same select.

### Usability of external storage

If you attach additional networked storage to the workspace, you can set `src_galaxy_storage_path` to a path on that storage volume. If your storage is e.g. called "galaxy storage", set the parameter to: `/data/galaxy_storage/datadir`. In theory, this should allow you to re-use datasets, tools, etc. from previous Galaxy workspaces.

## ResearchCloud parameters

The component takes the following parameters:

* `src_galaxy_version`: String. Set to e.g. `23.2` (default) to control the version of Galaxy that will be installed.
* `src_galaxy_api_exposed`: Boolean. if `true` (default), the `/api` route does not require authentication via Single Sign-On.
* `src_ibridges`: Boolean (default: `true`). Whether to enable support for the [iBridges](https://github.com/UtrechtUniversity/galaxy-tools-ibridges) tool for connecting to Yoda and iRODS instances. Implies `src_galaxy_bootstrap`, and adds iBridges to the list of tools to be installed automatically. To use the interactive iBridges tool, don't forget to enable interactive tools as well.
* `src_galaxy_jobs_default`: String. What runner to use for jobs by default. Valid values: `singularity`, `docker`, `local`. Default: `singularity`.
* `src_galaxy_interactive_tools`: if `true` (default), support for [interactive tools](https://docs.galaxyproject.org/en/master/admin/special_topics/interactivetools.html) is enabled.
* `src_galaxy_co_admin_group`: String group corresponding to an SRAM group. Members of this SRAM group will be made Galaxy admin users.
* `src_galaxy_bootstrap`: Boolean. Also see [below](#bootstrapping). If `true` (default), will attempt to install workflows, dataproviders and tools as configured by the following options:
    * `src_galaxy_tool_files`: String of comma-separated paths to YAML files (in this repo) containing tool specifications.
    * `src_galaxy_workflow_files`: String of comma-separated paths to `.ga` files (in this repo) containing workflow specifications.
    * `src_galaxy_custom_repo`: String URL to a git repo containing workflow files (in the `workflows` dir of the root of the repo) and tool specification files (in the `tools` dir). Example repo:
    * `src_galaxy_custom_repo_branch`: String branch of the custom repo to be used.
* `src_galaxy_storage_path`: String filepath. Default: `/srv/galaxy/datadir`. Path where Galaxy's "mutable data directory" will be located. If you attach additional networked storage to the workspace, you can set `src_galaxy_storage_path` to a path on that storage volume. If your storage is e.g. called "galaxy storage", set the parameter to: `/data/galaxy_storage/datadir`. In theory, this should allow you to re-use datasets, tools, etc. from previous Galaxy workspaces.
* `src_galaxy_enable_tus`: Boolean. If true, enable support for performant and resumable uploads using [tusd](https://training.galaxyproject.org/training-material/topics/admin/tutorials/tus/tutorial.html).
* `src_galaxy_cvmfs`: Boolean. If true, loads references data from [CVMFS](https://training.galaxyproject.org/training-material/topics/admin/tutorials/cvmfs/tutorial.html).
* `src_galaxy_brand`: String. Brand name to display in the Galaxy webinterface.
* `src_galaxy_byoc_pulsar`: Boolean (default: `false`). **Experimental.** Show the "Bring your own Pulsar" form (RabbitMQ username/password, maximum cores and memory, minimum/maximum GPUs) in the user preferences. The values are only stored for now; nothing uses them yet.
* `src_galaxy_rabbitmq`: Boolean (default: `false`). Install a RabbitMQ message broker on the Galaxy host, so remote Pulsar servers can connect to Galaxy over AMQPS. See [RabbitMQ](#rabbitmq-for-remote-pulsar-servers).
* `src_galaxy_rabbitmq_port`: Integer (default: `5671`). TCP port of the AMQPS listener. Open it in the workspace's security group. Must be 1–65535 and not `5672`, `4369`, `25672`, `80`, `443` or `8080`; the playbook fails otherwise.
* `src_galaxy_pulsar_amqp_configs`: String (default: empty). Remote Pulsar servers that connect to Galaxy's RabbitMQ, in the same format as `src_galaxy_pulsar_configs`, e.g. `[{name: hpc, label: Our HPC cluster, docker_enabled: true}]`. Requires `src_galaxy_rabbitmq: true`. See [Pulsar over AMQP](#pulsar-over-amqp) for the keys.

## Bootstrapping

The `src_galaxy_bootstrap` parameter determines whether the Galaxy instance should be bootstrapped: that is, whether various tools, workflows, and datamanagers should be installed.
These can be installed from various sources:

* a git repo containing `.yml` files descibing which tools to install, and `.ga` workflows files
  * see the example repo [here](https://github.com/dometto/galaxy-custom-tools-test.git)
  * put tool files in the `tools` subdirectory (as defined by the `_galaxy_custom_repo_tool_location` internal variable).
  * put workflow files in the `workflows` subdirectory (as defined by the `_galaxy_custom_repo_workflow_location` internal variable).
  * use `src_galaxy_custom_repo` and `src_galaxy_custom_repo_branch` to define your repository.
* any number of `.yml` tool files found in this repository
  * use `src_galaxy_tool_files` to provide these. For instance, you may set this parameter to `tools/ibridges.yml,tools/foo.yml` to install the tools defined in those locations.
  * see [here](tools/ibridges.yml) for an example
* any number of `.ga` workflow files found in this repository
  * use `src_galaxy_workflow_files` to provide these. For instance, you may set this parameter to `tools/bar.ga` to install that workflow.

By customizing the `src_galaxy_custom_repo`, `src_galaxy_tool_files` and `src_galaxy_workflow_files` parameters, you can thus create different 'flavours' for your Galaxy Catalog Item
that each contain different tools and workflows out of the box.

__Note__: workflow files installed using the bootstrapping procedure will be installed as `public` workflows available to all users on the Galaxy instance!
You can find these under `Data > Workflows` in Galaxy's main menu.

### Bootstrapping: how does it work?

If boostrapping is enabled, this Ansible playbook will:

1. Configure Galaxy to use a bootstrapping API key in `galaxy.conf`.
1. Restart Galaxy.

...and then invoke the [ansible-galaxy-tools role](https://github.com/UtrechtUniversity/ansible-galaxy-tools/) to do the following:

1. Use the bootstrapping API key to import tools.
1. Use the bootstrapping API key to create an admin user.
  * Use an API key belonging to the admin user to import workflows.

If needed Galaxy, is restarted. Finally, the playbook will __always__ remove the bootstrapping API key.

## Interactive tools

Galaxy has support for so-called [interactive tools](https://docs.galaxyproject.org/en/master/admin/special_topics/interactivetools.html), but it needs to be enabled. The `src_galaxy_interactive_tools` parameter controls whether they should be activated. At the moment, Galaxy interactive tools have to be run in Docker containers (AppTainer is not supported). So if `src_galaxy_interactive_tools` is set to `true`, the playbook does the following:

* Docker is installed
* Docker tools are enabled in `job_config.yml`
* A special Python script is used to determine whether a particular tool should be run as a Docker job.
  * any tools that are of the kind `'interactive'` are sent to Docker
  * __note__: this means that if an admin installs an interactive tool, it will automatically be configured *as* an interactive tool -- so admins should make sure that tools they install can securely be run as interactive tools.
  * other tools are sent to the default job destination (configured by `src_galaxy_jobs_default`)
  * see [templates/default_dispatch.py.j2]

__Note__: Galaxy supporst two kinds of interactive tools:

* subdomain-based: the interactive tool is made accessible on an ad-hoc created subdomain of the FQDN at which galaxy runs, e.g. `myinteractivetool12345.mygalaxy.com`
* [path-based](https://docs.galaxyproject.org/en/master/admin/special_topics/interactivetools.html#path-based-interactivetools): the interactive tool is made accessible on a sub-path of the FQDN at which Galaxy runs, e.g. `mygalaxy.com/myinteractivetool12345`

Because we cannot acquire SSL certificates for a wildcard subdomain on ResearchCloud, this component only supports path-based interactive tools.

## RabbitMQ for remote Pulsar servers

With `src_galaxy_rabbitmq: true` the playbook installs RabbitMQ from Ubuntu's `rabbitmq-server` package (see [tasks/rabbitmq.yml](tasks/rabbitmq.yml)). Remote Pulsar servers then connect *to* Galaxy's broker, so they do not need an open inbound port themselves.

* **Firewall**: open TCP `src_galaxy_rabbitmq_port` (default `5671`) in the SRC workspace's security group, ideally only for the Pulsar servers' addresses. No other RabbitMQ port has to be opened.
* **Listeners**: AMQPS on `src_galaxy_rabbitmq_port` (TLS 1.2/1.3, no client certificates). Plain AMQP on `127.0.0.1:5672` only, for Galaxy itself. The Erlang port mapper (`epmd`, 4369) and the Erlang distribution port (25672) are bound to `127.0.0.1`, and the node is named `rabbit@localhost`. The management plugin is disabled.
* **TLS certificate**: the certificate of the SRC Nginx plugin (acme.sh, `/etc/ssl/acme/<fqdn>/`, or certbot, `/etc/letsencrypt/live/<fqdn>/`; see [tasks/tls_facts.yml](tasks/tls_facts.yml)) is copied to `/etc/rabbitmq/tls/` with the key readable by the `rabbitmq` group only (`root:rabbitmq 0640`). The original key's permissions are not changed. The playbook fails if no certificate is found. Renewed certificates are **not** picked up automatically: re-run the playbook (it copies the new files and restarts RabbitMQ), or restart `rabbitmq-server` after copying them.
* **Users**: the default `guest` user is removed. A `galaxy` user without management tags is created. Each user's password is generated once and stored in `/etc/galaxy-rabbitmq/<user>.pass` (`root:root 0600`, directory `0700`), so re-runs keep the same passwords. Passwords are never put on a command line (which every local account can read with `ps`): they are passed to `rabbitmqctl add_user`/`change_password` on stdin, and only when the user is missing or its `.pass` file was just created. To rotate a password, delete its `.pass` file and re-run the playbook. A password changed by hand in RabbitMQ is not detected. Tasks that read or set passwords also use `no_log`, which keeps them out of Ansible's output.
* **Extending**: append vhosts to `_galaxy_rabbitmq_vhosts` (the `galaxy` user gets full permissions on each) and users as `{name, vhosts, tags}` to `_galaxy_rabbitmq_users` in [vars/rabbitmq_vars.yml](vars/rabbitmq_vars.yml). The playbook sets `_galaxy_rabbitmq_passwords` (user name → password) for building `amqp_url`s. Each entry of `src_galaxy_pulsar_amqp_configs` adds a vhost and a user (see [Pulsar over AMQP](#pulsar-over-amqp)).
* **`galaxy` user**: its password is in Galaxy's `job_conf.yml`, so RabbitMQ only accepts it from the host itself (`loopback_users.galaxy = true`); remote Pulsars use their own users.
* **Ordering**: RabbitMQ is set up in the playbook's `pre_tasks`, before the Galaxy role writes `job_conf.yml` (which needs the passwords) and starts Galaxy.
* Galaxy's Celery keeps using Postgres. RabbitMQ is only for Pulsar.
* **Connection URLs** (one vhost per Pulsar server, and one Galaxy MQ runner per vhost):
  * Galaxy runner `amqp_url`: `pyamqp://galaxy:<password>@127.0.0.1:5672//pulsar/<name>` (plain AMQP over loopback; `127.0.0.1` rather than `localhost`, which may resolve to `::1` where nothing listens).
  * Pulsar `message_queue_url`: `pyamqp://<pulsar user>:<password>@<workspace_fqdn>:<src_galaxy_rabbitmq_port>//pulsar/<name>?ssl=1`. The vhost's leading `/` follows the port's `/`, hence `//`.
  * `?ssl=1` alone encrypts the connection but does **not** verify the server certificate's hostname (py-amqp default). To verify it, set Pulsar's `amqp_connect_ssl_cert_reqs: required` and `amqp_connect_ssl_ca_certs`.
  * Generated passwords contain only letters and digits, and given ones only URL-unreserved characters, so they need no URL escaping.
* **Ubuntu versions**: tested end to end on Ubuntu 24.04 (noble: RabbitMQ 3.12.1, Erlang 25.3). Ubuntu 22.04 (jammy) ships older RabbitMQ (3.9) and Erlang (24) that support the same settings (TLS 1.3, `distribution.listener.interface`), but the configuration has not been verified there; the molecule scenario (jammy image) prints the installed versions in `verify.yml`.

The [`usegalaxy_eu.rabbitmqserver`](https://github.com/usegalaxy-eu/ansible-rabbitmq) role used in the GTN Pulsar tutorial is not used, because it runs RabbitMQ in Docker:
* the molecule tests cannot run Docker;
* the role passes passwords on `rabbitmqctl` command lines without `no_log`;
* it publishes the management port;
* Docker-published ports bypass the host firewall.

### Pulsar over AMQP

`src_galaxy_pulsar_amqp_configs` connects remote Pulsar servers through Galaxy's RabbitMQ (`src_galaxy_rabbitmq: true` is required; the playbook fails otherwise). The parameter uses the same format as `src_galaxy_pulsar_configs` and is parsed by the same code ([filter_plugins/src_galaxy.py](filter_plugins/src_galaxy.py)). Keys of each entry:

| Key | Default | Meaning |
|-----|---------|---------|
| `name` | required | `[a-z0-9_-]+`, at most 64 characters, unique, not `galaxy` or `guest`. Names the RabbitMQ user and vhost and the job environment. |
| `label` | `name` | Shown to users in **Use distributed compute resources**. |
| `password` | generated | Password of the Pulsar's RabbitMQ user: at least 12 characters from `A-Z a-z 0-9 . _ ~ -`. Leave it out to have one generated (32 letters/digits, kept across re-runs). Setting or changing it updates the user. |
| `docker_enabled` | `false` | Run the Pulsar's jobs in Docker containers. |
| `singularity_enabled` | `false` | Run the Pulsar's jobs in Singularity/Apptainer containers. |
| `dependency_resolution` | `remote` | `remote` (Pulsar resolves tool dependencies), `local` or `none`. |
| `manager` | `_default_` | Pulsar job manager the Galaxy runner talks to; the Pulsar must define it under `managers:`. |
| `jobs_directory` | `/mnt/pulsar/files/staging` | Pulsar's `staging_directory`. |

Invalid entries (bad or duplicate names, unknown keys, bad values) fail the playbook before anything is installed, with one message per problem; passwords are never echoed.

For each entry `<name>` the playbook creates:

* RabbitMQ vhost `/pulsar/<name>` and user `<name>` with permissions on that vhost only; the `galaxy` user gets permissions on it too.
* Job runner `pulsar_mq_<name>` (`PulsarMQJobRunner`, `amqp_url` with the `galaxy` user over `127.0.0.1:5672`, `galaxy_url` = the workspace URL, `manager`, message acknowledgement with republishing after 1200 s, `amqp_consumer_timeout: 2.0`, publish retries) and job environment `amqp_<name>` (`remote_transfer` file staging, `rewrite_parameters`, no remote metadata). One runner per Pulsar is needed because a runner serves exactly one `amqp_url` (vhost).
* An entry `amqp_<name>` in the users' **Use distributed compute resources** select. `src_galaxy_pulsar_tool_mappings` may also map tools to `amqp_<name>`.
* `/etc/galaxy-rabbitmq/pulsar_<name>.txt` (`root:root 0600`): the settings to paste into that Pulsar's `app.yml`, including its password.

The Pulsar server needs network access to `<workspace_fqdn>:<src_galaxy_rabbitmq_port>` (AMQPS) and to Galaxy's HTTPS URL (it fetches inputs and uploads outputs there), and the `kombu` Python package. Its `app.yml` then contains, for example:

```yaml
message_queue_url: "pyamqp://hpc:<password>@galaxy.example.org:5671//pulsar/hpc?ssl=1"
staging_directory: /mnt/pulsar/files/staging   # = jobs_directory
managers:
  _default_:                                    # = manager
    type: queued_python
    num_concurrent_jobs: 1
    min_polling_interval: 0.5
amqp_acknowledge: true
amqp_ack_republish_time: 1200
amqp_publish_retry: true
amqp_publish_retry_max_retries: 60
```

Removing an entry removes its runner, environment and select option, but not its RabbitMQ user, vhost or files in `/etc/galaxy-rabbitmq/`; delete those by hand (`rabbitmqctl delete_user <name>`, `rabbitmqctl delete_vhost /pulsar/<name>`). `job_conf.yml` holds the `galaxy` RabbitMQ password and is deployed `0640` (group `galaxy`), like `galaxy.yml`.

## BYOC PR generator (`scripts/byoc_pr`)

[`scripts/byoc_pr`](scripts/byoc_pr) is an operator script, not part of the playbook. It reads the **Bring your own Pulsar endpoint** (`byoc_pulsar`) form of all Galaxy users through the Galaxy API and adds each new endpoint to an [infrastructure-playbook](https://github.com/usegalaxy-eu/infrastructure-playbook)-style repository. The changes look like the hand-written usegalaxy.eu PRs ([#1384](https://github.com/usegalaxy-eu/infrastructure-playbook/pull/1384), [#872](https://github.com/usegalaxy-eu/infrastructure-playbook/pull/872)).

By default it only edits the files in a checkout (`--repo-path`). A CI/CD pipeline commits the result and opens the pull request. Opening the PRs from the script itself (`--open-pr`) is optional.

For a form with RabbitMQ username `<name>`, `<id>` is `<name>` with `-` replaced by `_`. Each endpoint changes five files:

| File | Change |
|------|--------|
| `files/galaxy/config/user_preferences_extra_conf.yml` | `["BYOC Pulsar - <name>", <name>-pulsar]` appended to the `distributed_compute` options |
| `files/galaxy/tpv/destinations.yml.j2` | destination `pulsar_<id>_tpv` (`inherits: pulsar_default`, `runner: pulsar_eu_<id>`, the form's core/memory/GPU limits, `scheduling.require: [<name>-pulsar]`) at the end of the `PULSAR DESTINATIONS` section |
| `group_vars/mq.yml` | `rabbitmq_users` entry `galaxy_<id>`, vhost `/pulsar/galaxy_<id>` |
| `secret_group_vars/pulsar.yml` | `rabbitmq_password_galaxy_<id>` (the form's password), encrypted again with Ansible Vault |
| `templates/galaxy/config/job_conf.yml` | `PulsarMQJobRunner` plugin `pulsar_eu_<id>` |

New entries are inserted as text after the last existing one, so the rest of each file stays unchanged. For the YAML files, `ruamel.yaml` (round-trip mode) finds the place and checks the result.

**Setup** (from the repository root):

```bash
python3 -m venv .venv
.venv/bin/pip install -r scripts/byoc_pr/requirements.txt
```

**Secrets** are never accepted on the command line:

* Galaxy **admin** API key: `--api-key-file <file>` or `$GALAXY_API_KEY`.
* Vault password of `secret_group_vars/pulsar.yml`: `--vault-password-file <file>` or `$ANSIBLE_VAULT_PASSWORD_FILE`. Use a plain file (first line, whitespace stripped); vault-id scripts are not supported. It is needed to write the files. A dry run without it does not check or change the vault file.

**Files only** (default, no git commands): add all new endpoints to an existing checkout:

```bash
GALAXY_API_KEY=... ANSIBLE_VAULT_PASSWORD_FILE=~/.vault_pass \
  .venv/bin/python -m scripts.byoc_pr --galaxy-url https://galaxy.example.org \
  --repo-path ../infrastructure-playbook --summary-json byoc-summary.json
```

Add `--dry-run` to print the diff and write nothing.

| Exit code | Meaning |
|-----------|---------|
| `0` | New endpoints were written (with `--dry-run`, would be written; with `--open-pr`, PRs opened or would be opened). |
| `3` | Nothing new: every valid request is already in the files (or has an open PR). |
| `1` | Error (Galaxy, vault, file layout, git/gh). With several endpoints the others are still processed, and the summary shows which one failed. |
| `2` | Bad command line. |

`--summary-json <path>` writes a summary with no passwords or email addresses:

* `new`: per endpoint, `id`, `galaxy_username`, `status` (`written`, `would_write`, `opened`, `would_open`, `pr_reused`, `branch_exists` or `failed` with `error`), `files` changed, `tag`, `rabbitmq_user`, `vhost`, `runner` and `destination`.
* `present`, `skipped` (`galaxy_username`, `reason`) and `invalid` (`galaxy_username`, `errors`).
* `pr`: a suggested `title` and `body` for the written endpoints, or `null` if there are none.
* `exit_code` and `error`.

**Running from a CI pipeline.** The pipeline owns the PR. A scheduled job:

1. checks out the infrastructure-playbook repository and creates a branch, e.g. `byoc/$(date +%Y%m%d-%H%M)`;
2. writes the CI secrets to files readable only by the job, and exports their paths:

   ```bash
   umask 077
   printf '%s' "$CI_VAULT_PASSWORD" > "$RUNNER_TEMP/vault_pass"
   export ANSIBLE_VAULT_PASSWORD_FILE="$RUNNER_TEMP/vault_pass"
   export GALAXY_API_KEY="$CI_GALAXY_API_KEY"   # masked CI variable, not on a command line
   ```

3. runs the script on the checkout:

   ```bash
   python -m scripts.byoc_pr --galaxy-url "$GALAXY_URL" \
     --repo-path infrastructure-playbook --summary-json summary.json || status=$?
   ```

4. on exit code `3`, stops without a PR. On `1`, fails the job (and still reads `summary.json` to see which endpoint failed). On `0`, commits the changed files and opens a PR with `jq -r .pr.title summary.json` as the title and `jq -r .pr.body summary.json` as the body;
5. deletes the secret files.

A request whose PR is still open is reported as `new` again on the next run, because the base branch does not have it yet. The pipeline should therefore reuse its open BYOC PR (for example a fixed branch name updated with a force push) instead of opening one per run.

**Optional: open the PRs from the script** (`--open-pr`). This needs `git`, and the [`gh`](https://cli.github.com/) CLI logged in with push access to the target repository. The script clones `--target-repo` (or reuses `--workdir`) and makes one branch `byoc/<id>` from `--base-branch` per endpoint. It is a dry run unless `--apply` is given:

```bash
.venv/bin/python -m scripts.byoc_pr --galaxy-url https://galaxy.example.org --open-pr \
  --api-key-file /etc/galaxy/automation_api_key --vault-password-file ~/.vault_pass --apply
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--galaxy-url` | required | Galaxy to read the forms from. |
| `--repo-path` | required unless `--open-pr` | Checkout to edit in place. |
| `--dry-run` | off | Print the diff and write nothing (files mode). |
| `--summary-json` | none | Machine-readable summary, see above. |
| `--open-pr` | off | Open one PR per endpoint instead of editing a checkout. |
| `--apply` | off | With `--open-pr`: push and open the PRs (otherwise a dry run). |
| `--target-repo` | `mirelaminkova/infrastructure-playbook` | `--open-pr`: GitHub repository for the PRs. Pass `usegalaxy-eu/infrastructure-playbook` only on purpose. |
| `--base-branch` | `master` | `--open-pr`: branch the PRs start from and target. |
| `--workdir` | temporary | `--open-pr`: clone of the target repository. An existing clone is reused (`git fetch`). |
| `--mq-host`, `--mq-port`, `--pulsar-galaxy-url` | `mq.galaxyproject.eu`, `5671`, `https://usegalaxy.eu` | Broker and `galaxy_url` in the new runners' `amqp_url`/`galaxy_url`. |

Behavior:

* **Validation**: RabbitMQ username `^[a-z0-9_-]{3,32}$`, password alphanumeric (10-128 characters), cores 1-1024, memory 1-16384 GB, GPUs 0-64 with min ≤ max. Invalid forms are skipped and reported by field and rule. The submitted values are never shown, and empty forms are ignored.
* **Idempotent**: an endpoint already in all target files is reported as `present`, and a second run on the same checkout changes nothing (exit code `3`). An endpoint found in only some of them (or whose `<name>-pulsar` tag is taken) is skipped for manual review. If two Galaxy users ask for the same `<id>`, the first one in Galaxy's user list wins. With `--open-pr`, an open PR for `byoc/<id>` is reused (nothing is pushed), and a `byoc/<id>` branch without an open PR is left alone (delete it to retry).
* **Secrets**: the RabbitMQ password is only ever written into the vault-encrypted `secret_group_vars/pulsar.yml`, in memory. It never appears in command lines, logs, the summary, the PR description or unencrypted files. The PR description names the requesting Galaxy username (not the email). If `secret_group_vars/pulsar.yml` is not vault-encrypted, the script refuses to write it.
* **Reading passwords**: `GET /api/users/{id}/information/inputs` returns `type: password` fields in clear but masks `type: secret` fields, and this repository's form uses `type: secret`. For a masked password, the script reads the raw value from the admin view of `GET /api/users/{id}` (`preferences.extra_user_preferences`). A password kept in Galaxy's vault (`store: vault`) cannot be read, and such a form is reported as invalid.
* An optional read-only check against a deployed Galaxy runs when `GALAXY_URL` and `GALAXY_API_KEY` are set: `.venv/bin/pytest tests/unit/byoc_pr/test_live.py`. It needs a user who filled in the form.

## Maintenance

You can stop/restart Galaxy using systemctl, e.g.: `<sudo> systemctl restart galaxy.target`. Use e.g. `<sudo> galaxyctl follow` to follow the logs of all Galaxy services.

## Testing

This repo contains [Molecule](https://ansible.readthedocs.io/projects/molecule/) tests for the playbook. To run them, you'll need:

1. [Molecule](https://github.com/ansible/molecule/) installed
1. [Podman](https://podman.io/docs/installation) installed
2. Access to the `ghcr.io/utrechtuniversity/src-test-workspace:ubuntu_jammy-nginx` container image
  * see: https://github.com/UtrechtUniversity/SRC-test-workspace/pkgs/container/src-test-workspace

To run the tests, just run the following command from the root of this repository: `molecule test`.

### Linting

`ansible-lint` is configured for this repository. Just install `ansible-lint` and run: `ansible-lint . galaxysrv.yml`

# License

[GNU LGPL](LICENSE)
