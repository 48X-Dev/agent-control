"""Prometheus instrumentation for turns and the executors that run them.

Everything here is hand-rolled rather than derived from the HTTP middleware, and
that is not a stylistic preference. ``starlette_exporter`` stamps a request's end
on its last response body chunk and buckets ``request_duration_seconds`` up to
sixty, so a three-minute turn lands in ``+Inf`` and drags any p95 computed over
that histogram into what looks like a server-wide latency regression. A turn is
a different unit of work from a request and needs its own buckets.

Metric names carry the ``agent_control_server_`` prefix every other metric this
process exports already uses (``db.py``, ``auth_framework/providers/
http_upstream.py``). The plan writes them without the ``server`` segment; one
namespace with two spellings in it is worse than either spelling.

Definitions live in this module rather than beside their call sites because
``prometheus_client`` registers by name into a process-global registry, and a
duplicate definition is an exception at import time rather than a warning.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

TURN_DURATION = Histogram(
    "agent_control_server_turn_duration_seconds",
    "Wall-clock duration of one blocking turn, by how it ended.",
    ("outcome",),
    buckets=(1, 2, 5, 10, 20, 30, 60, 120, 180, 300, 600),
)
"""Buckets are turn-shaped: the interesting boundaries are "felt instant",
"felt slow", and "hit the timeout", not the sub-second ones an HTTP histogram
cares about. ``outcome`` is one of the ``TURN_OUTCOME_*`` constants below."""

TURN_OUTCOME_COMPLETED = "completed"
TURN_OUTCOME_TIMEOUT = "timeout"
TURN_OUTCOME_ABANDONED = "abandoned"
TURN_OUTCOME_EXECUTOR_ERROR = "executor_error"

TURNS_REJECTED = Counter(
    "agent_control_server_turns_rejected_total",
    "Turns refused before any executor call, by reason.",
    ("reason",),
)
"""A refusal is invisible in the duration histogram, because no turn ran. Without
this counter a deployment whose quota is set too low and a deployment nobody is
using produce identical graphs."""

TURN_REJECT_IN_FLIGHT = "in_flight"
TURN_REJECT_QUOTA = "quota"

TURN_STALE_RECLAIMS = Counter(
    "agent_control_server_turn_stale_reclaims_total",
    "Turns that started by taking over a lock left behind by a lost handler.",
)
"""The staleness predicate in the acquire is a heuristic, and a heuristic that
fires is worth knowing about: a steady trickle means handlers are dying, and a
step change means a deployment just lost a replica."""

SESSIONS_STUCK_IN_FLIGHT = Gauge(
    "agent_control_server_sessions_stuck_in_flight",
    "Sessions holding a turn lock older than the staleness window.",
    multiprocess_mode="livemax",
)
"""Refreshed by the executor-health probe rather than by a sweeper, because
there is no sweeper and inventing one to move a gauge would be a background job
with a database query in it justified by a dashboard. The health route is what a
monitor already polls, so it is the honest place to compute this."""

EXECUTOR_PROBES = Counter(
    "agent_control_server_executor_probes_total",
    "Executor health probes, by whether the executor answered.",
    ("result",),
)
"""Deliberately carries no namespace key and no agent name.

``/metrics`` is served by a bare route with no credential dependency, unlike
every ``/api/v1`` router, so a label here is published to anyone who can reach
the port. Every other metric this process exports labels on a closed vocabulary
- a path template, a failure kind, an outcome - and a gauge keyed on
``(namespace_key, agent_name)`` would have been the first to hand tenant names
and agent inventory to an unauthenticated caller, one series per agent,
retained for the life of the process after the binding is deleted.

A counter rather than a gauge for a second reason: the probe runs per
namespace, so a gauge would be overwritten by whichever namespace polled last
and would describe that one alone. ``rate(...{result="unreachable"})`` answers
"is an executor down" without either flaw; which one is down is a question for
``GET /api/v1/agent-sessions/executor-health``, which is authorized and
namespace-scoped."""

EXECUTOR_PROBE_REACHABLE = "reachable"
EXECUTOR_PROBE_UNREACHABLE = "unreachable"

EXECUTOR_REQUEST_FAILURES = Counter(
    "agent_control_server_executor_request_failures_total",
    "Executor HTTP calls that failed, by kind of failure.",
    ("kind",),
)
"""``kind`` is a small closed set - see the ``EXECUTOR_FAILURE_*`` constants -
and never carries anything an executor said. Status codes and exception class
names are the most an executor is allowed to contribute to a label, and even
those are mapped through a fixed vocabulary first, because a label value derived
from a remote body is a cardinality bomb with a stranger's finger on it."""

EXECUTOR_FAILURE_TIMEOUT = "timeout"
EXECUTOR_FAILURE_TURN_TIMEOUT = "turn_timeout"
EXECUTOR_FAILURE_UNREACHABLE = "unreachable"
EXECUTOR_FAILURE_UPSTREAM_ERROR = "upstream_error"
EXECUTOR_FAILURE_REJECTED = "rejected"
EXECUTOR_FAILURE_UNAUTHORIZED = "unauthorized"
EXECUTOR_FAILURE_SESSION_MISSING = "session_missing"
EXECUTOR_FAILURE_UNREADABLE = "unreadable"
EXECUTOR_FAILURE_MODEL_UNAVAILABLE = "model_unavailable"


def record_executor_failure(kind: str) -> None:
    """Count one failed executor call."""
    EXECUTOR_REQUEST_FAILURES.labels(kind=kind).inc()


# =============================================================================
# Nudges and halts
# =============================================================================

NUDGE_CLAIMS = Counter(
    "agent_control_server_nudge_claims_total",
    "Nudge claims at a model boundary, by whether anything was claimed.",
    ("result",),
)
"""An empty claim is the overwhelmingly common case and is the number that says
whether the claim path is running at all. Without splitting it from the
claimed case, a deployment whose executors stopped claiming and a deployment
nobody is nudging produce the same flat graph."""

NUDGE_CLAIM_EMPTY = "empty"
NUDGE_CLAIM_CLAIMED = "claimed"

NUDGE_DELIVERY_LAG = Histogram(
    "agent_control_server_nudge_delivery_lag_seconds",
    "Seconds between a nudge being queued and a model being shown it.",
    buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300, 900),
)
"""Queued to applied. The one number answering "how long before the agent hears
me", and the one that degrades invisibly: a nudge that is never claimed just
sits, and nothing else in this process would notice."""

HALTS_TOTAL = Counter(
    "agent_control_server_halts_total",
    "Halts that landed, by how they were carried out and where.",
    ("mode", "boundary"),
)

HALT_DELIVERY_LAG = Histogram(
    "agent_control_server_halt_delivery_lag_seconds",
    "Seconds between stop being pressed and the executor blocking.",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300),
)
"""Graceful halts only. A restart's row is inserted already applied and would
observe near zero, dragging every percentile down and hiding exactly the
graceful regressions this histogram exists to catch."""

HALTS_EXPIRED = Counter(
    "agent_control_server_halts_expired_total",
    "Halts whose turn ended before the stop reached a boundary.",
)

HALTS_REJECTED = Counter(
    "agent_control_server_halts_rejected_total",
    "Halt requests refused before a row was written, by reason.",
    ("reason",),
)
"""Quota and 409 refusals are invisible in every other series here, because no
halt exists to count. A stop button that is quietly refusing is worse than one
that visibly fails."""

HALT_REJECT_NOT_IN_FLIGHT = "not_in_flight"
HALT_REJECT_QUOTA = "quota"

HALTS_APPLIED_STILL_IN_FLIGHT = Counter(
    "agent_control_server_halts_applied_still_in_flight_total",
    "Halts acknowledged as applied while the turn had not yet ended.",
)
"""The gap between "the executor says it blocked" and "the turn actually
ended". Acknowledged by the party being stopped, so it is attested rather than
observed; this counts how often the two disagree."""
