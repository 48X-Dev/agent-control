"""``fleet up``: the ordering this runtime has no ``depends_on`` to declare."""

from __future__ import annotations

from .bind import bind_runtimes
from .config import FleetConfig
from .container import ContainerRuntime
from .executor import read_network_addresses, start_executor, wait_until_serving
from .register import register_agents
from .server import ServerClient
from .settings import (
    HEALTH_TIMEOUT_SECONDS,
    READY_TIMEOUT_SECONDS,
    FleetSettings,
    executor_environment,
)

__all__ = ["bring_up"]


def bring_up(
    config: FleetConfig,
    *,
    runtime: ContainerRuntime,
    client: ServerClient,
    settings: FleetSettings,
    env: dict[str, str],
    adopt: bool,
) -> None:
    runtime.require_available()
    runtime.require_network()

    print("== server")
    client.wait_for_health(timeout_seconds=HEALTH_TIMEOUT_SECONDS)
    client.refuse_when_executor_credential_cannot_halt(settings.executor_api_key)
    addresses = read_network_addresses(runtime)
    print(f"   healthy; executors will reach it at {addresses.server_ip}")

    print("== register")
    register_agents(
        config,
        runtime=runtime,
        client=client,
        settings=settings,
        addresses=addresses,
        env=env,
    )

    print("== executors")
    for spec in config.agents:
        start_executor(
            runtime,
            spec,
            image=config.image,
            environment=executor_environment(
                spec, settings=settings, addresses=addresses, env=env
            ),
            memory=settings.executor_memory,
            cpus=settings.executor_cpus,
        )
        address = runtime.ipv4_address(spec.container_name)
        wait_until_serving(address, spec, timeout_seconds=READY_TIMEOUT_SECONDS)
        print(f"   {spec.agent_name} serving at {address}")

    print("== bind")
    bind_runtimes(config, runtime=runtime, client=client, adopt=adopt)
