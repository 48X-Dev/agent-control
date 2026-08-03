"""Task dispatcher for Agent Control.

This package is section 14's slice 1 of ``docs/plans/task-dispatcher.md`` and
nothing else. A YAML file of items becomes one agent session per item, one turn
each, and the transcripts are what an operator reads.

What it deliberately is not, restated here because the omissions are the point
and a reader who assumes otherwise will get hurt:

* There is **no claim that survives two processes**. The ledger is a local
  SQLite file (:mod:`.ledger`). Two dispatchers pointed at one file will both
  claim the same item.
* There is **no budget the server enforces**, no fleet stop, no write-back and
  no Linear.
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
