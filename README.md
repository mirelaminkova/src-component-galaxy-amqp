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

Jobs are routed by [TPV (total-perspective-vortex)](https://total-perspective-vortex.readthedocs.io/), in the same layout and with the same tag conventions as [usegalaxy.eu's infrastructure-playbook](https://github.com/usegalaxy-eu/infrastructure-playbook): `files/galaxy/tpv/tool_defaults.yml.j2` (rules) and `files/galaxy/tpv/destinations.yml.j2` (destinations) are rendered to `{{ galaxy_config_dir }}/tpv/`, and `job_conf.yml` sends every job to the `tpv_dispatcher`. Without Pulsar, every job goes to the default destination (`src_galaxy_jobs_default`), except interactive tools, which run in Docker.

When at least one Pulsar destination is configured (`src_galaxy_pulsar_configs` or `src_galaxy_pulsar_amqp_configs`), users get a **Use distributed compute resources** select in their preferences (User → Preferences → Manage information). It lists "default - Galaxy will decide where to put your job" plus one entry per destination, labelled with the config's `label` key or else the host of its `url` (REST) or its `name` (AMQP). The stored value is the destination's TPV scheduling tag, `<name>-pulsar`, and the TPV rule `remote_resources` requires that tag for the user's jobs. Some jobs always keep their normal destination: jobs of anonymous users, interactive tools (Docker), uploads (`upload1`, `__DATA_FETCH__`), set metadata, data sources, data managers, user-defined tools, expression and other Galaxy-internal tools (tag `internal`), and tools pinned by `src_galaxy_pulsar_tool_mappings` (their `environment` names a TPV destination: `local`, `docker`, `singularity`, `remote_<name>_basic`, `remote_<name>` or `amqp_<name>`).

A choice that no destination offers any more (a removed or unknown Pulsar) fails the user's jobs with a message asking them to pick another one in their preferences, as on usegalaxy.eu, rather than silently running the job somewhere the user did not choose. When the Galaxy has no Pulsar destinations at all, the choice is ignored, because the select is not shown then and users could not change it.

BYOC onboarding pull requests (usegalaxy.eu format) add `pulsar_<id>_tpv` destinations (`inherits: pulsar_default`, `scheduling.require: [<id>-pulsar]`) in the marked section at the end of `files/galaxy/tpv/destinations.yml.j2`.

Remote Pulsar servers can also be connected over a message queue (`src_galaxy_pulsar_amqp_configs`, Galaxy's `PulsarMQJobRunner`), so that the Pulsar server needs no inbound port; see [Pulsar over AMQP](#pulsar-over-amqp). These destinations appear in the same select.

### Usability of external storage

If you attach additional networked storage to the workspace, you can set `src_galaxy_storage_path` to a path on that storage volume. If your storage is e.g. called "galaxy storage", set the parameter to: `/data/galaxy_storage/datadir`. In theory, this should allow you to re-use datasets, tools, etc. from previous Galaxy workspaces.

## SURF ResearchCloud catalog item

What a ResearchCloud (SRC) component and catalog item for this playbook need, so a new one works the first time.

### Component

* **Repository**: `https://github.com/mirelaminkova/src-component-galaxy-amqp`, branch `main`.
* **Playbook**: `galaxysrv.yml`.
* **External plugin** `remote_ansible_version`: `14.6.0`. This Ansible release ships ansible-core 2.21.6, the oldest ansible-core the playbook supports (CI runs the tests with it in the job `pytest (SRC Ansible 14.6.0: ansible-core 2.21, Python 3.12)`). Older releases such as `9.1.0` (ansible-core 2.16) are not supported.
* **Base OS**: Ubuntu 24.04. ansible-core 2.21 needs Python ≥ 3.12 on the workspace, and Ubuntu 24.04's `python3` is 3.12 (Ubuntu 22.04 has 3.10). The RabbitMQ setup is also tested end to end on 24.04 only (see [RabbitMQ](#rabbitmq-for-remote-pulsar-servers)).
* The workspace also needs the SURF Nginx component (see [Prerequisites](#prerequisites)), which provides `workspace_fqdn` and the TLS certificate.

### Access rules

| Protocol | Direction | Port | Source | Mutable | Why |
|----------|-----------|------|--------|---------|-----|
| tcp | in | 22 | as today | as today | SSH |
| tcp | in | 80 | as today | as today | HTTP (certificate requests, redirect to HTTPS) |
| tcp | in | 443 | as today | as today | Galaxy (HTTPS) |
| tcp | in | 5671 (= `src_galaxy_rabbitmq_port`) | `0.0.0.0/0` | no | AMQPS for remote Pulsar servers |

Port 5671 is needed only with `src_galaxy_rabbitmq: true`. The SRC catalog item form cannot mark this rule mutable, so it is fixed at `0.0.0.0/0`. That is acceptable for BYOC: Pulsars connect from addresses that are not known when the catalog item is made, and access is protected by TLS plus a separate RabbitMQ user and vhost per Pulsar. In detail: it only speaks TLS (1.2/1.3), the `guest` user is removed, the `galaxy` user is accepted from the host itself only, each Pulsar has its own user and password with permissions on its own vhost only, and the management plugin, Erlang port mapper and distribution port are not reachable from outside.

### `co_passwordless_sudo`

Recommended: `co_passwordless_sudo: false`. The playbook already gives the members of `src_galaxy_co_admin_group` sudo with their SRAM TOTP (see [Logging in via SSH](#logging-in-via-ssh)). With `true`, every member of the CO gets passwordless root, and with it the RabbitMQ passwords (`/etc/galaxy-rabbitmq/`), `job_conf.yml` (which holds the `galaxy` RabbitMQ password), the Vault password (`/etc/galaxy/vault_password`) and the saved CD secrets.

### Component parameters

SRC passes every parameter as a string; booleans are `true`/`false`. An empty initial value means the playbook's default (given in the description) applies.

| Parameter key | (Initial) value | Overwritable | Required | Description |
|---------------|-----------------|--------------|----------|-------------|
| `src_galaxy_version` | `26.1` | yes | no | Galaxy release to install; deploys the `release_<version>` branch. Default `26.1`; `26.0` also works. |
| `src_galaxy_brand` | `SURF ResearchCloud` | yes | no | Brand name shown in the Galaxy web interface. Default `SURF ResearchCloud`. |
| `src_galaxy_api_exposed` | `true` | yes | no | If `true` (default), the `/api` route does not require SRAM Single Sign-On, so API keys work from outside (needed by the onboarding pipeline). With `false`, `/api` is behind SRAM like the rest of Galaxy. |
| `src_galaxy_co_admin_group` | `src_co_admin` | no | no | SRAM group whose members become Galaxy admins and may `sudo` (with TOTP). Default `src_co_admin`. |
| `src_galaxy_jobs_default` | `singularity` | yes | no | Default job destination: `singularity` (default), `docker` or `local`. Other values fall back to `singularity`. |
| `src_galaxy_interactive_tools` | `true` | yes | no | Enable path-based [interactive tools](#interactive-tools) (installs Docker). Default `false`. |
| `src_galaxy_enable_tus` | `true` | yes | no | Resumable uploads with [tusd](https://training.galaxyproject.org/training-material/topics/admin/tutorials/tus/tutorial.html). Default `true`. |
| `src_galaxy_cvmfs` | `true` | yes | no | Reference data from [CVMFS](https://training.galaxyproject.org/training-material/topics/admin/tutorials/cvmfs/tutorial.html). Default `true`. |
| `src_galaxy_storage_path` | (empty) | yes | no | Galaxy's mutable data directory. Default `/srv/galaxy/datadir`. On attached storage called "galaxy storage", e.g. `/data/galaxy_storage/datadir`; see [external storage](#usability-of-external-storage). |
| `src_galaxy_bootstrap` | `false` | yes | no | Install the tools and workflows below at deployment (see [Bootstrapping](#bootstrapping)). Default `false`. |
| `src_ibridges` | `true` | yes | no | Add the [iBridges](https://github.com/UtrechtUniversity/galaxy-tools-ibridges) tool (`tools/ibridges.yml`) to the tools installed by bootstrapping. Default `true`. |
| `src_galaxy_tool_files` | (empty) | yes | no | Comma-separated tool files in this repository, e.g. `tools/ibridges.yml,tools/foo.yml`. Used when bootstrapping. |
| `src_galaxy_workflow_files` | (empty) | yes | no | Comma-separated `.ga` workflow files in this repository. Used when bootstrapping; installed as public workflows. |
| `src_galaxy_custom_repo` | (empty) | yes | no | Git repository with `tools/` and `workflows/` to install when bootstrapping, e.g. `https://github.com/dometto/galaxy-custom-tools-test.git`. |
| `src_galaxy_custom_repo_branch` | `main` | yes | no | Branch of `src_galaxy_custom_repo`. Default `main`. |
| `src_galaxy_pulsar_configs` | (empty) | yes | no | Remote Pulsar servers reached over REST, as a flow-style list, e.g. `[{name: hpc, url: https://pulsar.example.org:8913/, private_token: <token>, label: Our HPC, docker_enabled: true}]`. Keys include `url` (required), `private_token`, `name` (default derived from the URL), `label`, `docker_enabled`, `singularity_enabled`, `dependency_resolution`, `manager`, `jobs_directory`. Leave empty for no REST Pulsar: any placeholder creates a fake Pulsar. A `private_token` makes CD store it encrypted, which needs `src_galaxy_vault_password`. |
| `src_galaxy_pulsar_tool_mappings` | (empty) | yes | no | Tools that always run on one TPV destination, e.g. `[{tool_id: bowtie2, environment: remote_hpc_basic}]`. `environment` is a TPV destination name: `local`, `docker`, `singularity`, `remote_<name>_basic`, `remote_<name>` (Docker, REST Pulsar with `docker_enabled`) or `amqp_<name>`. Applied only when a Pulsar is configured; an unknown `environment` fails the deployment at "Invalid Pulsar configuration". Leave empty for none. |
| `src_galaxy_pulsar_amqp_configs` | (empty) | yes | no | Remote Pulsar servers that connect to Galaxy's RabbitMQ, e.g. `[{name: hpc, label: Our HPC cluster, docker_enabled: true}]`. Needs `src_galaxy_rabbitmq: true`. Keys: see [Pulsar over AMQP](#pulsar-over-amqp). |
| `src_galaxy_byoc_pulsar` | `true` | no | no | Show the "Bring your own Pulsar" form in the user preferences (see [Onboarding a BYOC Pulsar via PR](#onboarding-a-byoc-pulsar-via-pr)). Default `false`. Experimental. |
| `src_galaxy_rabbitmq` | `true` | no | no | Install RabbitMQ (AMQPS) for remote Pulsars and load the BYOC onboarding files. Default `false`. See [RabbitMQ](#rabbitmq-for-remote-pulsar-servers). |
| `src_galaxy_rabbitmq_port` | `5671` | no | no | AMQPS port; must match the access rule. 1–65535, not `5672`, `4369`, `25672`, `80`, `443` or `8080`. Default `5671`. |
| `src_galaxy_vault_password` | (empty, secret) | no | no | Secret parameter: Ansible Vault password of `secret_group_vars/*.yml`, written to `/etc/galaxy/vault_password` (`root:root 0600`). Required once `secret_group_vars/` holds files and `src_galaxy_rabbitmq` is `true` (after the first BYOC onboarding), and for CD when a parameter holds a secret. Never committed. |
| `src_galaxy_cd` | `true` | yes | no | Apply changes merged to `src_galaxy_cd_branch` (BYOC onboarding PRs) to the running workspace with an `ansible-pull` timer. Default `false`. Whoever can merge to that branch has root on the workspace; see [Continuous deployment](#continuous-deployment). |
| `src_galaxy_cd_repo` | `https://github.com/mirelaminkova/src-component-galaxy-amqp.git` | no | no | Repository CD pulls (default as shown). A private repository needs an SSH URL and a deploy key. |
| `src_galaxy_cd_branch` | `main` | no | no | Branch CD applies. Default `main`. |
| `src_galaxy_cd_interval` | `15` | yes | no | Minutes, or a systemd time span such as `30min`, between the end of one CD run and the start of the next. Default `15`. |

Keep `workspace_fqdn` as SRC (the Nginx component) provides it; the playbook uses it for Galaxy's URL and the RabbitMQ certificate.

### Changes from the old component

* Remove `src_galaxy_pulsar_relay_configs`: the playbook does not read it. Pulsars that cannot accept inbound connections use `src_galaxy_pulsar_amqp_configs` or a BYOC onboarding PR.
* `src_galaxy_pulsar_tool_mappings`: `environment` values are now TPV destination names (`local`, `docker`, `singularity`, `remote_<name>_basic`, `remote_<name>`, `amqp_<name>`).
* `src_galaxy_pulsar_configs` and `src_galaxy_pulsar_tool_mappings` start empty. Placeholders such as `https://REPLACE` or `your_pulsar_env` create a fake Pulsar and fail the deployment at "Invalid Pulsar configuration".
* The default `src_galaxy_version` is `26.1`.
* New parameters: `src_galaxy_pulsar_amqp_configs`, `src_galaxy_vault_password` (secret), `src_galaxy_cd`, `src_galaxy_cd_repo`, `src_galaxy_cd_branch` and `src_galaxy_cd_interval`.
* `remote_ansible_version` `14.6.0` (was `9.1.0`), base OS Ubuntu 24.04, access rule tcp 5671 in from `0.0.0.0/0` (not mutable), and `co_passwordless_sudo: false`.

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
* The TPV rule `interactive_tool` (`files/galaxy/tpv/tool_defaults.yml.j2`) sends tools to the Docker destination:
  * any tools that are of the kind `'interactive'` are sent to Docker
  * __note__: this means that if an admin installs an interactive tool, it will automatically be configured *as* an interactive tool -- so admins should make sure that tools they install can securely be run as interactive tools.
  * other tools are sent to the default job destination (configured by `src_galaxy_jobs_default`)

__Note__: Galaxy supporst two kinds of interactive tools:

* subdomain-based: the interactive tool is made accessible on an ad-hoc created subdomain of the FQDN at which galaxy runs, e.g. `myinteractivetool12345.mygalaxy.com`
* [path-based](https://docs.galaxyproject.org/en/master/admin/special_topics/interactivetools.html#path-based-interactivetools): the interactive tool is made accessible on a sub-path of the FQDN at which Galaxy runs, e.g. `mygalaxy.com/myinteractivetool12345`

Because we cannot acquire SSL certificates for a wildcard subdomain on ResearchCloud, this component only supports path-based interactive tools.

## RabbitMQ for remote Pulsar servers

With `src_galaxy_rabbitmq: true` the playbook installs RabbitMQ from Ubuntu's `rabbitmq-server` package (see [tasks/rabbitmq.yml](tasks/rabbitmq.yml)). Remote Pulsar servers then connect *to* Galaxy's broker, so they do not need an open inbound port themselves.

* **Firewall**: open TCP `src_galaxy_rabbitmq_port` (default `5671`) in the SRC workspace's security group (see [Access rules](#access-rules): from `0.0.0.0/0` in the catalog item). No other RabbitMQ port has to be opened.
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
* Job runner `pulsar_mq_<name>` (`PulsarMQJobRunner`, `amqp_url` with the `galaxy` user over `127.0.0.1:5672`, `galaxy_url` = the workspace URL, `manager`, message acknowledgement with republishing after 1200 s, `amqp_consumer_timeout: 2.0`, publish retries) and TPV destination `amqp_<name>` (`remote_transfer` file staging, `rewrite_parameters`, no remote metadata; scheduling tag `<name>-pulsar`). One runner per Pulsar is needed because a runner serves exactly one `amqp_url` (vhost).
* An entry `<name>-pulsar` in the users' **Use distributed compute resources** select. `src_galaxy_pulsar_tool_mappings` may also map tools to `amqp_<name>`. A REST Pulsar (`src_galaxy_pulsar_configs`) and an AMQP Pulsar must not share a name (both would get the tag `<name>-pulsar`).
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

### Onboarding a BYOC Pulsar via PR

A "bring your own compute" (BYOC) Pulsar can be added with a pull request that makes the same changes as the usegalaxy.eu onboarding PRs ([usegalaxy-eu/infrastructure-playbook#1384](https://github.com/usegalaxy-eu/infrastructure-playbook/pull/1384), [#872](https://github.com/usegalaxy-eu/infrastructure-playbook/pull/872)). The lines those PRs add can be used verbatim; only the endpoint's names change. Once merged and deployed, the endpoint works. An endpoint `<id>` (`[a-z0-9_]+`, because it becomes part of variable names) changes five files. Each file has an anchor at the place usegalaxy.eu's file has the line its PRs insert above, and new entries go after the last entry above that anchor:

| File | Change | Insert above |
|------|--------|--------------|
| [`files/galaxy/config/user_preferences_extra_conf.yml`](files/galaxy/config/user_preferences_extra_conf.yml) | An option `["<label>", <tag>]` at the end of `preferences.distributed_compute` `remote_resources`, indented 18 spaces like the default option (as in usegalaxy.eu's current file; #872 predates it and used 10, which breaks the YAML here). The playbook appends these options to the **Use distributed compute resources** select, after the Pulsars from the component parameters (an option whose tag is already listed is skipped). Only this part of the file is used. | `# --- end of remote_resources options` (usegalaxy.eu: `ega_account:`) |
| [`files/galaxy/tpv/destinations.yml.j2`](files/galaxy/tpv/destinations.yml.j2) | A TPV destination `pulsar_<id>_tpv` (`inherits: pulsar_default`, `runner: pulsar_eu_<id>`, the form limits, `scheduling.require: [<tag>]`) at the end of the `PULSAR DESTINATIONS` section. A user who picks `<tag>` in the select gets their jobs routed there by TPV. | the `# LOCAL CONDOR DESTINATIONS #` banner, as on usegalaxy.eu (there is no HTCondor here; the banner only marks the end of the section) |
| [`group_vars/mq.yml`](group_vars/mq.yml) | A `rabbitmq_users` entry `{user: galaxy_<id>, password: "{{ rabbitmq_password_galaxy_<id> }}", vhost: /pulsar/galaxy_<id>}`. `rabbitmq_users:` is empty (no value) until the first entry. The playbook creates the vhost and the user and limits the user to that vhost. It also gives the `galaxy` user permissions on the vhost. The password is set on `rabbitmqctl`'s stdin, as for all users (see [RabbitMQ](#rabbitmq-for-remote-pulsar-servers)). | `# --- end of rabbitmq_users` (usegalaxy.eu: `rabbitmq_plugins:`) |
| `secret_group_vars/pulsar.yml` | Ansible Vault file with `rabbitmq_password_galaxy_<id>` (at least 10 characters from `A-Z a-z 0-9 . _ ~ -`). It must contain only these variables. | (Vault file, re-encrypted) |
| [`templates/galaxy/config/job_conf.yml`](templates/galaxy/config/job_conf.yml) | A `galaxy_jobconf.plugins` entry `pulsar_eu_<id>` (`galaxy.jobs.runners.pulsar:PulsarMQJobRunner`) with `params`. `plugins:` is empty (no value) until the first entry. `amqp_url` must use user `galaxy_<id>`, its password variable and vhost `/pulsar/galaxy_<id>`. Its host may be usegalaxy.eu's: an `amqp_url` that does not point at this Galaxy's RabbitMQ (`<workspace_fqdn>:<src_galaxy_rabbitmq_port>` or `127.0.0.1:5672`) is rebuilt as `pyamqp://galaxy_<id>:<password>@<workspace_fqdn>:<src_galaxy_rabbitmq_port>//pulsar/galaxy_<id>?ssl=1`, and `galaxy_url` is always set to this Galaxy's URL, so a runner copied from usegalaxy.eu (`mq.galaxyproject.eu`, `https://usegalaxy.eu`) works unchanged. The other params are added to the runner as they are; their keys must be lowercase identifiers other than `id` and `load`. Only `PulsarMQJobRunner` entries are accepted. | `default_destination:` (as on usegalaxy.eu) |

How the files are used:

* SRC runs the playbook with `-i localhost,`, so `group_vars/` is not loaded through the inventory. [tasks/byoc_onboarding.yml](tasks/byoc_onboarding.yml) loads the files explicitly, and only when `src_galaxy_rabbitmq` is `true`. Without RabbitMQ they are ignored.
* **Vault password**: SRC runs `ansible-playbook` without `--vault-password-file`. Set the secret component parameter `src_galaxy_vault_password` instead. The playbook writes it to `/etc/galaxy/vault_password` (`root:root 0600`, `no_log`). When the parameter is empty, as in the CD runs of issue #22, the playbook uses that file if it exists. It then decrypts each `secret_group_vars/*.yml` in memory with `ansible-vault view` (`no_log`) and defines its variables. The playbook fails with a clear message if `secret_group_vars/` holds files but there is no password, or if a file cannot be decrypted (wrong password, or not a Vault file); the message names the file only. The vault password is never committed.
* **Creating `secret_group_vars/pulsar.yml`**: the repository has no Vault file yet, because only the holder of the Vault password can create it. The first onboarding PR creates it with `ansible-vault create --vault-password-file <file> secret_group_vars/pulsar.yml`. Later PRs edit it with `ansible-vault edit`. Use the same password as `src_galaxy_vault_password`.
* **Checks at deploy time**: before RabbitMQ is configured, the playbook checks that every runner has its RabbitMQ user and vault password, and that the names, vhosts and params keys are valid. It also checks that the passwords are URL-safe, and that no user takes the name of `galaxy`, `guest` or a `src_galaxy_pulsar_amqp_configs` entry. If any check fails, it stops with one message per problem. Messages never include passwords or URLs.
* **Checks in CI**: on every pull request, pytest runs the same checks on the committed files ([tests/unit/test_byoc_onboarding.py](tests/unit/test_byoc_onboarding.py)). In CI, passwords in `group_vars/mq.yml` and `amqp_url` must be `{{ rabbitmq_password_galaxy_<id> }}`, not values, and labels and names must not contain Jinja (`{{`, `{%`). The tests fail if any file in `secret_group_vars/` is not an Ansible Vault file, if a TPV destination is defined twice, if a destination names a missing `pulsar_eu_*` runner, or if an option's tag is not required by any destination.
* **Contract tests**: they insert the lines #872 and #1384 add, verbatim ([tests/fixtures/byoc_onboarding/](tests/fixtures/byoc_onboarding/)), into copies of this repository's own files at the anchors above, and add the passwords to a Vault file with a throwaway password. They check both the first onboarding on the empty files and a second one. The tests check that the playbook variables resolve, that RabbitMQ gets the users and vhosts, and that the job configuration has the runners pointed at this Galaxy (loaded by Galaxy's own job configuration code). They also check that the select shows the options, that the TPV configuration lints, and that a TPV dry run through `job_conf.yml` sends the job of a user who picked an endpoint's tag to that endpoint's destination and runner. `tasks/byoc_onboarding.yml` itself runs with `ansible-playbook` for the Vault password file, a missing password and a wrong password.
* **For the Pulsar admin**: the Pulsar connects with the RabbitMQ user from `group_vars/mq.yml` (`galaxy_<id>`) and the password the admin chose in the **Bring your own Pulsar** form (the value in `secret_group_vars/pulsar.yml`). Its `app.yml` gets `message_queue_url: "pyamqp://galaxy_<id>:<password>@<workspace_fqdn>:<src_galaxy_rabbitmq_port>//pulsar/galaxy_<id>?ssl=1"`, the `manager` named in the runner's params under `managers:`, and the `amqp_*` settings shown in [Pulsar over AMQP](#pulsar-over-amqp). The Galaxy URL and the AMQPS port are in the workspace's details on SRC.
* Removing an endpoint from the files removes its runner and select option. Its RabbitMQ user, vhost and `/etc/galaxy-rabbitmq/galaxy_<id>.pass` stay, as for `src_galaxy_pulsar_amqp_configs`.

## Continuous deployment

With `src_galaxy_cd: true`, merged changes of the onboarding files (RabbitMQ users and vhosts, job config, TPV destinations, user preferences; see #18) reach the running workspace without recreating it.

**Trust boundary.** With CD on, whatever lands on `src_galaxy_cd_branch` of `src_galaxy_cd_repo` runs **as root** on every workspace that uses it, within one interval. Anyone who can push or merge to that branch therefore has root on those workspaces. Protect the branch: on GitHub, a branch protection rule or ruleset on `main` with required pull request reviews, required status checks (pre-commit, pytest, Ansible Lint), and no direct or force pushes. If you want to control when changes arrive, point a deployment at its own branch or fork and merge into it deliberately. For signed commits only, `ansible-pull` has `--verify-commit` (add it to `/usr/local/sbin/galaxy-cd`; root's GnuPG keyring must hold the signers' keys).

How it works:

* At deployment, the playbook saves the component parameters the workspace was created with to `/etc/galaxy-cd/params.yml` (root, `0600`). Parameters holding secrets (a `password`, `secret`, `token`, ... in the name or in a key of the value, e.g. `private_token` in `src_galaxy_pulsar_configs`) go to `/etc/galaxy-cd/secret_params.yml` instead, encrypted with the Ansible Vault password `/etc/galaxy/vault_password` (from `src_galaxy_vault_password`); without that password the deployment fails rather than store them in plain text. `src_galaxy_vault_password` itself is never saved again.
* It installs Ansible in `/opt/galaxy-cd/venv`, the playbook's roles and collections in `/var/lib/galaxy-cd/ansible`, the script `/usr/local/sbin/galaxy-cd`, and `galaxy-cd.service` with `galaxy-cd.timer`. The timer starts as the last step of the deployment; the first run is one interval later.
* Every `src_galaxy_cd_interval` after the previous run ends, `galaxy-cd` runs `ansible-pull` as root: it updates `/var/lib/galaxy-cd/checkout` to `src_galaxy_cd_branch` and runs `galaxysrv.yml` with the saved parameters, the vault password file `/etc/galaxy/vault_password` (`ANSIBLE_VAULT_PASSWORD_FILE`), `src_galaxy_bootstrap=false` and `--tags rabbitmq,galaxy_config_files`. Besides the always-run tasks (variables, validation, admin facts) that is only `tasks/rabbitmq.yml` and the Galaxy role's config files (`galaxy.yml`, `job_conf.yml`, TPV, `user_preferences_extra_conf.yml`). Packages, Nginx, the database and the Galaxy code are left alone. A test (`tests/unit/test_cd.py`) fails if a task touching the onboarding files is not covered by these tags.
* When the branch has no new commits the playbook is skipped (`--only-if-changed`), unless the previous run failed or was interrupted (timeout, reboot, `systemctl stop`): the marker `/var/lib/galaxy-cd/last-run-failed` is set before each run and only removed after a successful one.
* Galaxy is restarted gracefully (`galaxyctl graceful`) by the playbook's handlers, and only when a config file changed. Handlers also run when a later task fails (`ANSIBLE_FORCE_HANDLERS`), so a changed config is never left unapplied.
* `requirements.yml`: when the checkout's `requirements.yml` differs from the installed one (`/var/lib/galaxy-cd/requirements.sha256`), its roles and collections are reinstalled with `ansible-galaxy install --force` (without `--force`, a role already installed at another version is kept). If the pull itself brings a new `requirements.yml`, that run still uses the previous roles; the new ones are installed after it and the next run applies the playbook again with them.
* A lock (`/var/lib/galaxy-cd/lock`, in the root-only state directory) keeps runs from overlapping; a run that finds it held by another run exits without doing anything. Any other lock problem fails the run.

Check the last run:

```bash
systemctl status galaxy-cd.service      # "failed" if the last run failed
journalctl -u galaxy-cd -n 100          # its output; ends in "galaxy-cd: OK: ..." or "galaxy-cd: FAILED: ..."
systemctl list-timers galaxy-cd.timer   # when it runs next
```

Run it now: `sudo systemctl start galaxy-cd.service`. Turn it off: `sudo systemctl disable --now galaxy-cd.timer` (turn it on again with `enable --now`).

**Private repository.** Create a read-only deploy key for the repository, set `src_galaxy_cd_repo` to the repository's SSH URL, and put the private key on the workspace, readable by root only:

```bash
sudo install -m 0600 -o root -g root /path/to/deploy_key /etc/galaxy-cd/deploy_key
```

galaxy-cd uses it when it exists (`--private-key`). Host keys are not trusted on first use: the deployment writes GitHub's published SSH host key (from `https://api.github.com/meta`) to `/root/.ssh/known_hosts` (`_galaxy_cd_known_hosts` in `vars/cd_vars.yml`). For another Git host, add its published key there, or on the workspace with `sudo ssh-keygen -F <host> -f /root/.ssh/known_hosts` to check and `sudo tee -a /root/.ssh/known_hosts` to add it, after verifying the fingerprint with the host's documentation.

**No rollback.** A merged change that breaks Galaxy (for example an invalid `job_conf.yml` or TPV file) is applied and Galaxy is restarted with it; previous config files are not kept (`galaxy_backup_configfiles: false`). Fix forward: revert or fix the change on the branch, and the next run (or `sudo systemctl start galaxy-cd.service`) applies the fix. The pull request checks (pytest renders the config templates and lints TPV) are the guard before merging.

Limits: the saved parameters are those of the deployment; changing a component parameter still needs a new workspace. Packages, including the TPV version in the Galaxy venv, are not updated by CD. Why not a GitHub Actions deploy job: it would need inbound SSH to the workspace and a deploy key with access to it on GitHub, which SRC workspaces do not offer by default.

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
