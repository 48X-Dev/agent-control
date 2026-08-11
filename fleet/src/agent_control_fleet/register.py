"""``fleet register``: one admin job, one subprocess per agent, then it exits."""

from __future__ import annotations

from .config import FleetConfig
from .container import ContainerRuntime
from .executor import REGISTER_ARGUMENTS
from .server import ServerClient
from .settings import FleetSettings, NetworkAddresses, register_environment

__all__ = ["RegisterError", "register_agents"]


class RegisterError(RuntimeError):
    """A registration that did not complete, named by code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def register_agents(
    config: FleetConfig,
    *,
    runtime: ContainerRuntime,
    client: ServerClient,
    settings: FleetSettings,
    addresses: NetworkAddresses,
    env: dict[str, str],
) -> None:
    """Register every agent and sync its steps, or raise before any executor starts."""

    client.refuse_when_credentials_are_off()
    for spec in config.agents:
        print(f"   register {spec.agent_name}")
        code = runtime.run_to_completion(
            name=f"ac-register-{spec.agent_name.replace('_', '-')}",
            image=config.image,
            environment=register_environment(
                spec, settings=settings, addresses=addresses, env=env
            ),
            arguments=REGISTER_ARGUMENTS,
        )
        if code != 0:
            raise RegisterError(
                "register_failed",
                f"Registering {spec.agent_name} exited {code}. Nothing downstream ran: "
                "an executor whose steps are unregistered 403s at import and, with no "
                "restart policy, stays exited. Re-running is safe once the cause is fixed.",
            )
