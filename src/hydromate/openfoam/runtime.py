"""Driving the OpenFOAM tools: ``checkMesh``, ``decomposePar``, ``interFoam``.

Mirrors :class:`hydromate.env.TelemacRuntime` and shares its
:class:`~hydromate.env.ShellRuntime` base: hydromate never imports OpenFOAM's
environment, it sources ``etc/bashrc`` in a subshell per command. That keeps
OpenFOAM's ``$PATH``, ``$LD_LIBRARY_PATH`` and MPI wrappers out of hydromate's own
process, where they would collide with TELEMAC's.

The run is streamed, not captured: an interFoam run on a million cells is hours of
work, and a silent terminal gives no way to tell a healthy run from one whose time
step has collapsed. :class:`OpenFoamProgress` renders that as a bar of simulated
time with the live Courant number and time step beside it - the two numbers that say
whether the air phase is behaving.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Callable

from hydromate.env import ShellRuntime
from hydromate.openfoam import dicts

log = logging.getLogger("hydromate")

# "Time = 12.5" at the head of each time step
_TIME_RE = re.compile(r"^Time = ([0-9.eE+-]+)\s*$")
# "Courant Number mean: 0.012 max: 0.43"
_COURANT_RE = re.compile(r"Courant Number mean: ([0-9.eE+-]+) max: ([0-9.eE+-]+)")
_INTERFACE_RE = re.compile(r"Interface Courant Number mean: ([0-9.eE+-]+) "
                           r"max: ([0-9.eE+-]+)")
_DELTAT_RE = re.compile(r"^deltaT = ([0-9.eE+-]+)")
_CONTINUITY_RE = re.compile(r"time step continuity errors : .*cumulative = "
                            r"([0-9.eE+-]+)")


class OpenFoamProgress:
    """Echo streamed OpenFOAM output with a simulated-time progress bar.

    Shares :class:`hydromate.progress.ProgressBar` with the TELEMAC renderer, so a
    3D OpenFOAM run and a TELEMAC run look the same in the terminal. The extra
    quantities shown - ``Co`` and ``dt`` - are the diagnostic that matters for this
    solver: a healthy interFoam run holds ``dt`` roughly constant near the Courant
    target, while a run whose air phase is misbehaving shows ``dt`` falling by
    orders of magnitude with the simulated clock barely advancing.
    """

    def __init__(self, end_time: float, *, start_time: float = 0.0, out=None):
        from hydromate.progress import ProgressBar

        self.bar = ProgressBar(total=max(end_time - start_time, 0.0), out=out)
        self.start_time = float(start_time)
        self.time = float(start_time)
        self.courant = 0.0
        self.delta_t = 0.0
        self.continuity = 0.0

    def feed(self, line: str) -> None:
        m = _TIME_RE.match(line)
        if m:
            self.time = float(m.group(1))
        m = _COURANT_RE.search(line)
        if m:
            self.courant = float(m.group(2))
        m = _DELTAT_RE.match(line)
        if m:
            self.delta_t = float(m.group(1))
        m = _CONTINUITY_RE.search(line)
        if m:
            self.continuity = float(m.group(1))
        self.bar.echo(line)
        self.bar.update(
            self.time - self.start_time,
            f"t={self.time:.2f}s  Co={self.courant:.2f}  dt={self.delta_t:.3g}s")

    def close(self) -> None:
        self.bar.close()


class OpenFoamRuntime(ShellRuntime):
    """Run OpenFOAM utilities and solvers for a case."""

    def __init__(self, of_config):
        if of_config.bashrc is None:
            raise ValueError(
                "openfoam.bashrc is not set. Point it at the etc/bashrc of your "
                "OpenFOAM install (e.g. "
                "/home/modelling/OpenFOAM/OpenFOAM-9/etc/bashrc) in case-config.yml."
            )
        super().__init__(of_config.bashrc)
        self.of = of_config

    # ------------------------------------------------------------------ checks
    def check_available(self) -> str:
        """Return the OpenFOAM version string, raising if the environment is broken."""
        proc = self.run('echo "$WM_PROJECT-$WM_PROJECT_VERSION"; '
                        'command -v ' + shlex.quote(self.of.solver), check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise RuntimeError(
                f"Could not initialise the OpenFOAM environment via {self.env_script}."
                f"\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return proc.stdout.strip().splitlines()[0]

    # checks whose failure genuinely stops a run, as opposed to the marginal-face
    # reports that a mesh cut from real terrain will almost always carry
    _FATAL_CHECKS = ("negative volume", "zero or negative", "open cells",
                     "not closed", "Cell to face addressing", "point usage",
                     "Upper triangular ordering", "incorrectly oriented")

    def check_mesh(self, case_dir: str | Path, *,
                   on_line: Callable[[str], None] | None = None
                   ) -> tuple[bool, list[str], str]:
        """Run ``checkMesh``; return ``(usable, failed_checks, output)``.

        ``checkMesh`` exits 0 even when it fails checks, so the verdict is read from
        its own report rather than from the exit status.

        *usable* is **not** "passed every check". A mesh cut from a real DEM
        routinely carries a few faces past a skewness or non-orthogonality limit
        where the survey has a step or a seam - the isar reach has exactly one in
        4.9 million, and it comes from the bathymetry, not from the meshing. Treating
        that as a blocking failure would mean refusing to model the terrain. Only the
        checks in :attr:`_FATAL_CHECKS` - a cell with no volume, an unclosed cell,
        broken addressing - actually prevent a run.
        """
        proc = self.run("checkMesh -constant", cwd=case_dir, check=False,
                        on_line=on_line)
        out = proc.stdout
        failed = [ln.strip() for ln in out.splitlines() if "***" in ln]
        fatal = [ln for ln in failed
                 if any(key.lower() in ln.lower() for key in self._FATAL_CHECKS)]
        if "Mesh OK" in out:
            log.info("checkMesh: Mesh OK")
        elif fatal:
            log.error("checkMesh found problems that prevent a run:\n  %s",
                      "\n  ".join(fatal))
        else:
            log.warning("checkMesh flagged marginal faces (the mesh is still "
                        "runnable):\n  %s", "\n  ".join(failed))
        return not fatal, failed, out

    # ------------------------------------------------------------- decomposition
    def decompose(self, case_dir: str | Path, *, force: bool = True):
        flag = " -force" if force else ""
        return self.run(f"decomposePar{flag}", cwd=case_dir, check=True)

    def reconstruct(self, case_dir: str | Path, *, latest: bool = False):
        flag = " -latestTime" if latest else ""
        return self.run(f"reconstructPar{flag}", cwd=case_dir, check=False)

    # -------------------------------------------------------------------- solve
    def run_stage(self, case_dir: str | Path, stage: str, *, end_time: float,
                  start_time: float = 0.0, n_processors: int | None = None,
                  cfg=None, show_progress: bool = True):
        """Activate *stage*'s dictionaries and run the solver over them.

        Pass *cfg* so the dictionaries are regenerated from the configuration this
        run was launched with; without it the build-time dictionaries are used and a
        changed ``end_time`` would be reported but not honoured (see
        :func:`hydromate.openfoam.dicts.activate`).

        Returns the ``CompletedProcess``; a non-zero exit is returned rather than
        raised, so a caller running both stages can report which one failed.
        """
        case_dir = Path(case_dir)
        dicts.activate(case_dir, stage, cfg)
        n = n_processors if n_processors is not None else self.of.n_processors
        if n and n > 1:
            command = f"mpirun -np {int(n)} {self.of.solver} -parallel"
        else:
            command = self.of.solver

        progress = OpenFoamProgress(end_time, start_time=start_time) \
            if show_progress else None
        on_line = progress.feed if progress else print
        log.info("running the %s stage: %s (to t = %g s)", stage, command, end_time)
        try:
            return self.run(command, cwd=case_dir, check=False, on_line=on_line)
        finally:
            if progress is not None:
                progress.close()

    def latest_time(self, case_dir: str | Path) -> float:
        """Largest numeric time directory in the case (0 when only ``0/`` exists)."""
        times = []
        for entry in Path(case_dir).iterdir():
            try:
                times.append(float(entry.name))
            except ValueError:
                continue
        return max(times) if times else 0.0
