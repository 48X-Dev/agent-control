"""Apple ``container``, the only runtime this package shells out to."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "NETWORK_NAME",
    "POSTGRES_CONTAINER",
    "SERVER_CONTAINER",
    "ContainerError",
    "ContainerRuntime",
]

NETWORK_NAME = "agent-control"
SERVER_CONTAINER = "ac-server"
POSTGRES_CONTAINER = "ac-postgres"

_ARCH = "arm64"
_COMMAND_TIMEOUT_SECONDS = 120.0


class ContainerError(RuntimeError):
    """A refused or failed ``container`` invocation, named by code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ContainerRuntime:
    """Every ``container`` call the fleet makes, in one place so tests can fake it."""

    executable: str = "container"

    def require_available(self) -> None:
        """Refuse early rather than at the first start, which is halfway through."""

        if shutil.which(self.executable) is None:
            raise ContainerError(
                "container_missing",
                f"{self.executable!r} is not on PATH. This package drives Apple's "
                "container runtime and has no Docker path.",
            )

    def require_network(self) -> None:
        try:
            self._run(["network", "inspect", NETWORK_NAME])
        except ContainerError as exc:
            raise ContainerError(
                "network_missing",
                f"The {NETWORK_NAME!r} network does not exist. The fleet joins the "
                "stack's network, it does not create it; run scripts/apple-container-up.sh.",
            ) from exc

    def is_running(self, name: str) -> bool:
        listed = self._run(["ls"])
        return any(line.split()[:1] == [name] for line in listed.splitlines() if line.strip())

    def ipv4_address(self, name: str) -> str:
        """The container's address on its first network, without the CIDR suffix."""

        address = self._first_network(name).get("ipv4Address", "")
        value = str(address).split("/")[0].strip()
        if not value:
            raise ContainerError(
                "address_unreadable", f"{name} is running but reports no IPv4 address."
            )
        return value

    def ipv4_gateway(self, name: str) -> str:
        """The gateway, which from inside these VMs is the host."""

        gateway = str(self._first_network(name).get("ipv4Gateway", "")).split("/")[0].strip()
        if not gateway:
            raise ContainerError(
                "gateway_unreadable",
                f"{name} reports no IPv4 gateway, so the host address cannot be computed.",
            )
        return gateway

    def remove(self, name: str) -> None:
        """Best effort: a name that is already gone is the state this wanted."""

        try:
            self._run(["rm", name])
        except ContainerError:
            return

    def run_detached(
        self,
        *,
        name: str,
        image: str,
        environment: Mapping[str, str],
        arguments: Sequence[str] = (),
        read_only: bool = False,
        tmpfs: Sequence[str] = (),
        uid: int | None = None,
        gid: int | None = None,
        memory: str | None = None,
        cpus: int | None = None,
    ) -> None:
        self._run(
            [
                "run",
                "-d",
                *self._run_flags(name, environment, read_only, tmpfs, uid, gid, memory, cpus),
                image,
                *arguments,
            ]
        )

    def run_to_completion(
        self,
        *,
        name: str,
        image: str,
        environment: Mapping[str, str],
        arguments: Sequence[str] = (),
        read_only: bool = False,
        tmpfs: Sequence[str] = (),
        uid: int | None = None,
        gid: int | None = None,
        memory: str | None = None,
        cpus: int | None = None,
    ) -> int:
        """Run a one-shot job and return its exit code, which the caller must check."""

        completed = subprocess.run(
            [
                self.executable,
                "run",
                "--rm",
                *self._run_flags(name, environment, read_only, tmpfs, uid, gid, memory, cpus),
                image,
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.stderr.strip():
            print(completed.stderr.rstrip())
        return completed.returncode

    def _run_flags(
        self,
        name: str,
        environment: Mapping[str, str],
        read_only: bool,
        tmpfs: Sequence[str],
        uid: int | None,
        gid: int | None,
        memory: str | None = None,
        cpus: int | None = None,
    ) -> list[str]:
        flags = ["--name", name, "--network", NETWORK_NAME, "-a", _ARCH]
        if memory is not None:
            flags += ["-m", memory]
        if cpus is not None:
            flags += ["-c", str(cpus)]
        if read_only:
            flags.append("--read-only")
        for mount in tmpfs:
            flags += ["--tmpfs", mount]
        if uid is not None:
            flags += ["--uid", str(uid)]
        if gid is not None:
            flags += ["--gid", str(gid)]
        for key, value in environment.items():
            flags += ["-e", f"{key}={value}"]
        return flags

    def _first_network(self, name: str) -> dict[str, object]:
        document = json.loads(self._run(["inspect", name]) or "null")
        record = document[0] if isinstance(document, list) and document else document
        if not isinstance(record, dict):
            raise ContainerError(
                "inspect_unreadable", f"container inspect {name} returned no record."
            )
        status = record.get("status")
        networks = status.get("networks") if isinstance(status, dict) else None
        if not isinstance(networks, list) or not networks or not isinstance(networks[0], dict):
            raise ContainerError("inspect_unreadable", f"{name} is on no network.")
        return networks[0]

    def _run(self, arguments: Sequence[str]) -> str:
        completed = subprocess.run(
            [self.executable, *arguments],
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            raise ContainerError(
                "container_failed",
                f"`{self.executable} {' '.join(arguments)}` exited "
                f"{completed.returncode}: {completed.stderr.strip()}",
            )
        return completed.stdout
