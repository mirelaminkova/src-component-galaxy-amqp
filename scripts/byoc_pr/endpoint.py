"""Names derived from a BYOC request, following the usegalaxy.eu infrastructure-playbook."""

from __future__ import annotations

from dataclasses import dataclass

from scripts.byoc_pr.validation import ByocRequest


@dataclass(frozen=True)
class Settings:
    """Values every new ``PulsarMQJobRunner`` shares (usegalaxy.eu defaults)."""

    mq_host: str = "mq.galaxyproject.eu"
    mq_port: int = 5671
    galaxy_url: str = "https://usegalaxy.eu"
    manager: str = "production"


@dataclass(frozen=True)
class Endpoint:
    request: ByocRequest

    @property
    def id(self) -> str:
        """``<id>`` in all names; Ansible variable names can not contain ``-``."""
        return self.request.username.replace("-", "_")

    @property
    def tag(self) -> str:
        """TPV scheduling tag and ``distributed_compute`` option value."""
        return f"{self.request.username}-pulsar"

    @property
    def label(self) -> str:
        return f"BYOC Pulsar - {self.request.username}"

    @property
    def mq_user(self) -> str:
        return f"galaxy_{self.id}"

    @property
    def vhost(self) -> str:
        return f"/pulsar/galaxy_{self.id}"

    @property
    def password_var(self) -> str:
        return f"rabbitmq_password_galaxy_{self.id}"

    @property
    def runner(self) -> str:
        return f"pulsar_eu_{self.id}"

    @property
    def destination(self) -> str:
        return f"pulsar_{self.id}_tpv"

    @property
    def branch(self) -> str:
        return f"byoc/{self.id}"

    def amqp_url(self, settings: Settings) -> str:
        return (
            f"pyamqp://{self.mq_user}:{{{{ {self.password_var} }}}}"
            f"@{settings.mq_host}:{settings.mq_port}/{self.vhost}?ssl=1"
        )
