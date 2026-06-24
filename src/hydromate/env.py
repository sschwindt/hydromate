"""Bridge to the TELEMAC runtime environment.

The hydromate pipeline runs in its own (``telemac-inn``) Python environment and
does *not* import TELEMAC's Python directly. Instead, whenever a TELEMAC tool or
TELEMAC's SELAFIN API is needed, we spawn a subshell that first *sources* the
user-configured ``pysource.*.sh`` (exporting HOMETEL + PYTHONPATH + the right
``python``) and then runs the requested command. This keeps the geospatial stack
(gmsh, rasterio, geopandas) cleanly decoupled from TELEMAC's interpreter.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from hydromate.config import TelemacEnv


class TelemacRuntime:
    def __init__(self, env: TelemacEnv):
        self.env = env
        self.pysource = Path(env.pysource)

    def _wrap(self, command: str) -> list[str]:
        """Wrap *command* so it runs after sourcing the pysource script."""
        # `set -e` so a failing source aborts before the command runs.
        script = f"set -e; source {shlex.quote(str(self.pysource))}; {command}"
        return ["bash", "-lc", script]

    def run(self, command: str, cwd: str | Path | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        """Run *command* inside the sourced TELEMAC environment."""
        return subprocess.run(
            self._wrap(command),
            cwd=str(cwd) if cwd else None,
            check=check,
            text=True,
            capture_output=True,
        )

    def python(self, code: str, cwd: str | Path | None = None,
               check: bool = True) -> subprocess.CompletedProcess:
        """Execute a Python snippet using TELEMAC's interpreter (has data_manip)."""
        wrapped = "python - <<'__TMSETUP_PY__'\n" + code + "\n__TMSETUP_PY__"
        return self.run(wrapped, cwd=cwd, check=check)

    def run_solver(self, cas_file: str | Path, cwd: str | Path,
                   ncsize: int | None = None) -> subprocess.CompletedProcess:
        """Launch the configured solver on *cas_file* (used for the dry test run)."""
        ncsize = ncsize or self.env.n_processors
        parallel = f" --ncsize={ncsize}" if ncsize and ncsize > 1 else ""
        cmd = f"{self.env.solver}.py {shlex.quote(str(cas_file))}{parallel}"
        return self.run(cmd, cwd=cwd)

    def check_available(self) -> str:
        """Return the TELEMAC python version string, raising if the env is broken."""
        proc = self.python(
            "import sys; "
            "from data_manip.formats.selafin import Selafin; "
            "print(sys.version.split()[0])",
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "Could not initialise the TELEMAC environment via "
                f"{self.pysource}.\nstderr:\n{proc.stderr}"
            )
        return proc.stdout.strip()
