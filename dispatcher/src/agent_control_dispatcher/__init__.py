"""Task dispatcher for Agent Control.

A YAML file of items, or the eligible issues of one Linear milestone, become
one agent session per item, one turn each, and the transcripts are what an
operator reads.

The claim now lives in the server's ``agent_tasks`` table (:mod:`.server_ledger`):
atomic, leased, and reclaimable from a dispatcher that died mid-task. The local
SQLite ledger (:mod:`.ledger`) is still here and is still honest about
coordinating nothing; ``--ledger`` is how you ask for it.

What this package deliberately is not, restated here because the omissions are
the point and a reader who assumes otherwise will get hurt:

* There is **no budget the loop enforces**, and there must never be one. The
  namespace turn budget, the dispatch pause and the executor kill switch are
  refusals on the turn path inside the server. A ceiling checked by the process
  being budgeted is not a ceiling.
* There is **no fleet stop here**, and no play button.
* There is **no write of any kind**. Linear is read, never written: no comment,
  no state change, no label. Both sources raise from ``write_back`` rather than
  returning something a caller could record as a delivered report.
* ``--dry-run`` is the default and it is an *assertion about the deployment*,
  not a proof. Section 12.3's canary is not in this slice, so nothing here
  verifies that the agent's tools are read-only.

Every one of those is required before this runs unattended, and it does not run
unattended: an operator starts it and watches the terminal. That operator is the
human in the loop.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
