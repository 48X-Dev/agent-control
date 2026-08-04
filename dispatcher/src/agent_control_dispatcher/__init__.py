"""Task dispatcher for Agent Control.

A YAML file of items, or the eligible issues of one Linear milestone, become
one agent session per item, one turn each, and the transcripts are what an
operator reads.

The claim now lives in the server's ``agent_tasks`` table (:mod:`.server_ledger`):
atomic, leased, and reclaimable from a dispatcher that died mid-task. The local
SQLite ledger (:mod:`.ledger`) is still here and is still honest about
coordinating nothing; ``--ledger`` is how you ask for it.

Two ways to run it. ``once`` reads a source and makes one pass, which is what
cron runs. ``serve`` (:mod:`.loop`) reads no source at all: it polls the queue
for rows the console's play button created and claims those, so a press runs
within seconds and nobody has to open a terminal. **The absence is the
authorization boundary**, not an unfinished feature - a milestone scope is
authorized by a human pressing play over the issues themselves, so a process a
scheduler can start must not be able to construct one.

What this package deliberately is not, restated here because the omissions are
the point and a reader who assumes otherwise will get hurt:

* There is **no budget the loop enforces**, and there must never be one. The
  namespace turn budget, the dispatch pause and the executor kill switch are
  refusals on the turn path inside the server. A ceiling checked by the process
  being budgeted is not a ceiling. ``serve`` reads them and backs off, which is
  an optimisation and not the ceiling.
* There is **no write of any kind**. Linear is read, never written: no comment,
  no state change, no label. Both sources raise from ``write_back`` rather than
  returning something a caller could record as a delivered report.
* ``dry_run`` is fixed on the row when the row is created and is an *assertion
  about the deployment*, not a proof. Section 12.3's canary is not in this
  slice, so nothing here verifies that the agent's tools are read-only, and
  nothing here can widen what a human already agreed to.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
