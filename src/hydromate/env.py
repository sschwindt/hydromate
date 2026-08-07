"""Bridge to the TELEMAC runtime environment.

The hydromate pipeline runs in its own (``hydromate-env``) Python environment and
does *not* import TELEMAC's Python directly. Instead, whenever a TELEMAC tool or
TELEMAC's SELAFIN API is needed, we spawn a subshell that first *sources* the
user-configured ``pysource.*.sh`` (exporting HOMETEL + PYTHONPATH + the right
``python``) and then runs the requested command. This keeps the geospatial stack
(gmsh, rasterio, geopandas) cleanly decoupled from TELEMAC's interpreter.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

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
            check: bool = True,
            on_line: Callable[[str], None] | None = None) -> subprocess.CompletedProcess:
        """Run *command* inside the sourced TELEMAC environment.

        With *on_line* the command's combined stdout+stderr is streamed line by
        line (each passed to the callback as it arrives, so a long solver run is
        not silent) instead of being buffered until the end; the full output is
        still returned in ``CompletedProcess.stdout``. Without it, output is
        captured quietly as before.
        """
        args = self._wrap(command)
        cwd = str(cwd) if cwd else None
        if on_line is None:
            return subprocess.run(
                args, cwd=cwd, check=check, text=True, capture_output=True,
            )
        proc = subprocess.Popen(
            args, cwd=cwd, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        captured: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            captured.append(line)
            on_line(line.rstrip("\n"))
        proc.stdout.close()
        returncode = proc.wait()
        result = subprocess.CompletedProcess(
            args, returncode, stdout="".join(captured), stderr="",
        )
        if check and returncode != 0:
            raise subprocess.CalledProcessError(
                returncode, args, output=result.stdout,
            )
        return result

    def python(self, code: str, cwd: str | Path | None = None,
               check: bool = True) -> subprocess.CompletedProcess:
        """Execute a Python snippet using TELEMAC's interpreter (has data_manip)."""
        wrapped = "python - <<'__TMSETUP_PY__'\n" + code + "\n__TMSETUP_PY__"
        return self.run(wrapped, cwd=cwd, check=check)

    def run_solver(self, cas_file: str | Path, cwd: str | Path,
                   ncsize: int | None = None,
                   sortie: bool = True,
                   solver: str | None = None,
                   check: bool = True,
                   on_line: Callable[[str], None] | None = None) -> subprocess.CompletedProcess:
        """Launch a TELEMAC solver on *cas_file* (used for the dry test run).

        *solver* overrides the configured solver launcher (default
        ``self.env.solver``, e.g. ``telemac2d``); pass ``"telemac3d"`` to run the
        3D case written by ``add3d.py``. With ``check=False`` a non-zero exit is
        returned (in ``returncode``) instead of raising, so a caller running several
        cases (e.g. the vertical convergence study) can report which one failed.

        *sortie* (default True) adds ``-s --nozip`` so TELEMAC writes a plain-text
        ``<cas>_<timestamp>.sortie`` listing next to the case. That listing carries
        the per-boundary cumulated flowrates and volume balance that the flux-
        convergence analysis (hydromate.sortie) reads; ``--nozip`` keeps it unzipped in
        parallel runs so the parser can find it.

        *on_line* is forwarded to :meth:`run`: pass a callback (e.g.
        ``SolverProgress.feed``) to stream the solver's listing live instead of
        running silently.
        """
        ncsize = ncsize or self.env.n_processors
        parallel = f" --ncsize={ncsize}" if ncsize and ncsize > 1 else ""
        sortie_flags = " -s --nozip" if sortie else ""
        launcher = solver or self.env.solver
        # The TELEMAC launcher (telemac2d/3d.py) imports numpy, which the bare
        # pysource shell does not necessarily provide. Run it with an interpreter
        # that has it - by default the one hydromate itself runs in, which has numpy
        # by construction - while the sourced pysource supplies HOMETEL and the
        # TELEMAC PYTHONPATH. `command -v` resolves the launcher inside that shell,
        # so nothing about the install layout is assumed here.
        #
        # Deliberately NOT a named conda/mamba environment: that hard-codes a machine
        # assumption into the package (that mamba is installed, that the env is called
        # X, and that mamba's root prefix resolves), and it fails on a machine whose
        # MAMBA_ROOT_PREFIX points somewhere else. An interpreter path needs none of
        # that, and pointing `telemac.solver_python` at another env's python covers
        # the case where a *different* interpreter is genuinely wanted.
        python = (getattr(self.env, "solver_python", None)
                  or os.environ.get("HYDROMATE_TELEMAC_PYTHON")
                  or sys.executable)
        cmd = (f'{shlex.quote(str(python))} "$(command -v {launcher}.py)" '
               f"{shlex.quote(str(cas_file))}{parallel}{sortie_flags}")
        return self.run(cmd, cwd=cwd, check=check, on_line=on_line)

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
