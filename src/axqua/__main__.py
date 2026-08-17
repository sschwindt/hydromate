"""``python -m axqua`` - the CLI, without needing the console script on ``PATH``.

A detached job is started with an environment captured from the solver's setup script,
and that environment's ``PATH`` is the solver's, not the user's shell's. The
``axqua`` console script is often absent from it, so the launcher falls back to
``[sys.executable, "-m", "axqua", ...]`` - which only works because this file exists.
"""

from __future__ import annotations

import sys

from axqua.cli import main

if __name__ == "__main__":
    sys.exit(main())
