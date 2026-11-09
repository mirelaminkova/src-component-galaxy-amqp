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
* `src_galaxy_rabbitmq_port`: Integer (default: `5671`). TCP port of the AMQPS listener. Open it in the workspace's security group.

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
* **Users**: the default `guest` user is removed. A `galaxy` user without management tags is created. Each user's password is generated once and stored in `/etc/galaxy-rabbitmq/<user>.pass` (`root:root 0600`, directory `0700`), so re-runs keep the same passwords. Tasks that handle passwords use `no_log`.
* **Extending**: append vhosts to `_galaxy_rabbitmq_vhosts` (the `galaxy` user gets full permissions on each) and users as `{name, vhosts, tags}` to `_galaxy_rabbitmq_users` in [vars/rabbitmq_vars.yml](vars/rabbitmq_vars.yml). The playbook sets `_galaxy_rabbitmq_passwords` (user name → password) for building `amqp_url`s. This version creates no Pulsar vhosts yet.
* Galaxy's Celery keeps using Postgres. RabbitMQ is only for Pulsar.
* **Connection URLs** (one vhost per Pulsar server, and one Galaxy MQ runner per vhost):
  * Galaxy runner `amqp_url`: `pyamqp://galaxy:<password>@localhost:5672//pulsar/<name>` (plain AMQP over loopback).
  * Pulsar `message_queue_url`: `pyamqp://<pulsar user>:<password>@<workspace_fqdn>:<src_galaxy_rabbitmq_port>//pulsar/<name>?ssl=1`. The vhost's leading `/` follows the port's `/`, hence `//`.
  * `?ssl=1` alone encrypts the connection but does **not** verify the server certificate's hostname (py-amqp default). To verify it, set Pulsar's `amqp_connect_ssl_cert_reqs: required` and `amqp_connect_ssl_ca_certs`.
  * Generated passwords contain only letters and digits, so they need no URL escaping.
* **Ubuntu versions**: tested on Ubuntu 24.04 (noble: RabbitMQ 3.12.1, Erlang 25.3) and, in the molecule CI, on Ubuntu 22.04 (jammy: RabbitMQ 3.9, Erlang 24). The same configuration works on both.

The [`usegalaxy_eu.rabbitmqserver`](https://github.com/usegalaxy-eu/ansible-rabbitmq) role used in the GTN Pulsar tutorial is not used, because it runs RabbitMQ in Docker:
* the molecule tests cannot run Docker;
* the role passes passwords on `rabbitmqctl` command lines without `no_log`;
* it publishes the management port;
* Docker-published ports bypass the host firewall.

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
