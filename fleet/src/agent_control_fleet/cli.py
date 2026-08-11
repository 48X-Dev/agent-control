"""``agent-control-fleet``: up, register, bind, doctor."""

from __future__ import annotations

import argparse
import os
import sys

from .bind import BindError, bind_runtimes
from .config import FleetConfig, FleetConfigError, load_fleet_config
from .container import ContainerError, ContainerRuntime
from .doctor import diagnose, render
from .executor import ExecutorError, read_network_addresses
from .register import RegisterError, register_agents
from .server import ServerClient, ServerError
from .settings import FleetSettings, SettingsError
from .up import bring_up

_REFUSALS = (
    BindError,
    ContainerError,
    ExecutorError,
    FleetConfigError,
    RegisterError,
    ServerError,
    SettingsError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-control-fleet",
        description=(
            "Run one executor container per agent named in fleet.yaml, under Apple's "
            "container runtime. `up` does the whole ordered sequence; `register` and "
            "`bind` are the two admin jobs it runs, both of which exit; `doctor` "
            "reports how intent and fact differ and fixes nothing."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    up = subparsers.add_parser(
        "up",
        help="Bring the fleet up, in order, and bind what it started.",
        description=(
            "Waits for the server, refuses an executor credential that cannot claim "
            "halts, registers every agent in its own subprocess and checks the exit "
            "code, starts one container per agent with no published ports, waits for "
            "each to serve exactly its own name, then writes the runtime rows. "
            "Idempotent: a re-run restarts what is missing. Nothing here restarts a "
            "container that crashes later."
        ),
    )
    up.add_argument(
        "--adopt",
        action="store_true",
        help="Rewrite bindings this fleet did not write. A one-time gesture.",
    )
    subparsers.add_parser(
        "register",
        help="Register every agent and sync its steps, then exit.",
        description=(
            "One subprocess per agent, each with the environment its executor would "
            "get, because sys.modules caches the agent module and a second import in "
            "one process registers the first agent again. Refuses to proceed when the "
            "server accepts an uncredentialed read."
        ),
    )
    bind = subparsers.add_parser(
        "bind",
        help="Point each agent at the container now serving it, then exit.",
        description=(
            "Writes PUT /agent-runtimes/{agent_name} with the observed container IP "
            "and executor_app_name equal to the agent name. A row this fleet did not "
            "write aborts the run and names the row, unless --adopt."
        ),
    )
    bind.add_argument(
        "--adopt",
        action="store_true",
        help="Rewrite bindings this fleet did not write. A one-time gesture.",
    )
    subparsers.add_parser(
        "doctor",
        help="Report how fleet.yaml, agent_runtimes and the containers differ.",
        description=(
            "Read-only. Exit 1 means it found something an operator should look at, "
            "exit 0 that it did not, exit 2 that it could not read enough to say."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = dict(os.environ)
    runtime = ContainerRuntime()
    try:
        settings = FleetSettings.from_env(env, require_credentials=args.command != "doctor")
        config = load_fleet_config(settings.config_path)
        client = ServerClient(base_url=settings.server_url, api_key=settings.register_api_key)
        return _dispatch(args, config, runtime=runtime, client=client, settings=settings, env=env)
    except _REFUSALS as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted. Whatever started stays started; re-run to finish.")
        return 130


def _dispatch(
    args: argparse.Namespace,
    config: FleetConfig,
    *,
    runtime: ContainerRuntime,
    client: ServerClient,
    settings: FleetSettings,
    env: dict[str, str],
) -> int:
    if args.command == "up":
        bring_up(
            config,
            runtime=runtime,
            client=client,
            settings=settings,
            env=env,
            adopt=args.adopt,
        )
        return 0
    if args.command == "register":
        register_agents(
            config,
            runtime=runtime,
            client=client,
            settings=settings,
            addresses=read_network_addresses(runtime),
            env=env,
        )
        return 0
    if args.command == "bind":
        bind_runtimes(config, runtime=runtime, client=client, adopt=args.adopt)
        return 0
    findings = diagnose(config, runtime=runtime, client=client)
    print(render(findings))
    return 1 if any(not finding.informational for finding in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
