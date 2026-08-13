"""Import the file-output modules without importing the agent.

``my_agent/__init__.py`` imports ``agent.py``, which calls ``init`` and ``bind``
at module scope. A bare package here lets the submodules import without that.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_MY_AGENT = Path(__file__).resolve().parent.parent / "my_agent"

if "my_agent" not in sys.modules:
    package = types.ModuleType("my_agent")
    package.__path__ = [str(_MY_AGENT)]  # type: ignore[attr-defined]
    sys.modules["my_agent"] = package
