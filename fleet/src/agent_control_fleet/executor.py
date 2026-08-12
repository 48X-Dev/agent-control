"""Starting one container per group and proving each process serves the agent it claims."""

from __future__ import annotations

import time

import httpx

from .config import AgentSpec, GroupSpec
from .container import POSTGRES_CONTAINER, SERVER_CONTAINER, ContainerError, ContainerRuntime
from .settings import NetworkAddresses

__all__ = [
    "EXECUTOR_GID",
    "EXECUTOR_UID",
    "ExecutorError",
    "REGISTER_ARGUMENTS",
    "read_network_addresses",
    "start_executor",
    "wait_until_serving",
]

# Following the uid 10001 and 10002 precedent in the dispatcher and knowledge images.
EXECUTOR_UID = 10003
EXECUTOR_GID = 10003

AGENTS_TMPFS = "/agents"

# The image's one-shot mode: materialize the agent package, import it so bind()
# discovers the tools, sync the steps, exit. It must not start a server.
REGISTER_ARGUMENTS = ("register",)

_REQUEST_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL_SECONDS = 2.0


class ExecutorError(RuntimeError):
    """An executor that did not start, or did not become the agent it claims."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def read_network_addresses(runtime: ContainerRuntime) -> NetworkAddresses:
    """Read the live network once; none of these three is configurable."""

    try:
        return NetworkAddresses(
            server_ip=runtime.ipv4_address(SERVER_CONTAINER),
            postgres_ip=runtime.ipv4_address(POSTGRES_CONTAINER),
            gateway=runtime.ipv4_gateway(SERVER_CONTAINER),
        )
    except ContainerError as exc:
        raise ExecutorError(
            "stack_not_running",
            f"Could not read the stack's addresses: {exc}. The fleet joins a running "
            "stack; run scripts/apple-container-up.sh first.",
        ) from exc


def start_executor(
    runtime: ContainerRuntime,
    group: GroupSpec,
    *,
    image: str,
    environment: dict[str, str],
    memory: str,
    cpus: int,
) -> None:
    """Start the container unless it is already up, which is what makes ``up`` idempotent."""

    if runtime.is_running(group.container_name):
        return
    runtime.remove(group.container_name)
    runtime.run_detached(
        name=group.container_name,
        image=image,
        environment=environment,
        read_only=True,
        tmpfs=(AGENTS_TMPFS,),
        uid=EXECUTOR_UID,
        gid=EXECUTOR_GID,
        memory=memory,
        cpus=cpus,
    )


def wait_until_serving(address: str, spec: AgentSpec, *, timeout_seconds: float) -> None:
    """Poll one process's /list-apps until it returns exactly this agent, or refuse.

    Exactly this agent and no sibling: each process is given its own agents root
    holding one directory, and a root shared across the group would make every
    process in it advertise every name (section 3.4).
    """

    deadline = time.monotonic() + timeout_seconds
    last = "no response"
    while True:
        try:
            response = httpx.get(
                f"http://{address}:{spec.port}/list-apps",
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code == 200:
                served = response.json()
                if served == [spec.agent_name]:
                    return
                last = f"/list-apps returned {served!r}"
            else:
                last = f"HTTP {response.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            last = str(exc)
        if time.monotonic() >= deadline:
            raise ExecutorError(
                "executor_not_serving",
                f"{address}:{spec.port} did not serve exactly [{spec.agent_name!r}] within "
                f"{timeout_seconds:.0f}s ({last}). Binding it now would produce a row whose "
                "first turn returns 500 and reads as a broken executor.",
            )
        time.sleep(_POLL_INTERVAL_SECONDS)
