"""Entering a solver's environment, on Linux, on Windows, or through WSL.

Both TELEMAC and OpenFOAM are entered by *sourcing a setup script* that exports a
large environment - ``HOMETEL`` and a Fortran toolchain for one, ``WM_PROJECT_DIR``
and a long ``PATH``/``LD_LIBRARY_PATH`` for the other. axqua used to do that one
way only::

    ["bash", "-lc", f"set -e; source {script}; {command}"]

which is three Linux assumptions in one line: that ``source`` is a command, that
``bash`` is on ``PATH``, and that the setup script is a shell script. None holds on
Windows, where TELEMAC is configured through a ``.bat`` and MS-MPI provides
``mpiexec`` rather than ``mpirun``.

Three kinds, chosen per solver
------------------------------
``posix``
    ``bash -lc 'source <script>; <cmd>'``. Linux and macOS; the historic behaviour.

``windows``
    ``cmd /c 'call <script>.bat && <cmd>'`` for a native install, or the same shape as
    ``posix`` against a **bundled** shell - blueCFD-Core and the ESI OpenFOAM package
    both ship their own MSYS bash, and entering their environment means using *that*
    bash rather than the system one. ``shell`` selects it.

``wsl``
    ``wsl.exe -d <distro> -- bash -lc '...'``, with path translation across the
    boundary. This is the route for **Foundation** OpenFOAM on Windows, which has no
    native build.

Capture, don't wrap
-------------------
The primary mechanism is :meth:`SolverEnvironment.capture`: run the setup script once,
read the resulting environment back as a dictionary, then launch the solver **directly**
in it. That is preferred over wrapping every command in a shell for three reasons:

* it is the same design on all three kinds (``env -0`` under bash, ``set`` under cmd),
  rather than three;
* it lets a detached launcher put the environment straight onto the process, so no
  intermediate shell sits inside the job's process tree - which is what makes
  process-tree cancellation clean, since there are fewer layers between the unit and the
  MPI ranks;
* it doubles as validation. A profile is correct when capturing it yields the variables
  the solver needs, and that can be checked when the profile is configured rather than
  when a multi-hour job is submitted.

:meth:`SolverEnvironment.command` remains for WSL - a captured Linux environment cannot
launch a Windows process, so those commands must go through ``wsl.exe`` regardless - and
for the streaming path, where a shell is wanted anyway.

Path translation lives here and nowhere else. Scattering ``wslpath`` calls through the
meshers and steering writers is how a codebase acquires a second, subtly different
notion of what a path is.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath

log = logging.getLogger("axqua")

# Variables a capture must produce for the environment to count as working.
# Chosen by *inspecting real installations*, not from documentation: the v9.1.1
# pysource exports HOMETEL and SOURCEFILE but NOT SYSTELCFG, so requiring the latter
# rejected a perfectly good TELEMAC.
SENTINELS = {
    "telemac": ("HOMETEL",),
    "openfoam": ("WM_PROJECT_DIR", "WM_PROJECT_VERSION", "FOAM_APPBIN"),
}

# Printed on its own before the environment dump, so anything the setup script says
# on the way (TELEMAC's pysource emits "[WARN] SALOME ..." and a "TELEMAC set: ..."
# banner) can be discarded rather than parsed as a variable. Without this the first
# captured "variable" is the script's own chatter, and a stray '=' in it would shadow
# a real entry.
_MARKER = "__AXQUA_ENV_BEGIN__"

#: Exported onto a detached job by the launcher. Its presence tells the child axqua
#: that the solver environment is already on the process, so it must not source the setup
#: script again and put a shell back inside the job's process tree.
ENTERED_MARKER = "AXQUA_ENV_CAPTURED"

# sentinel for "argument not given", so None can mean "deliberately no script"
_UNSET = object()


class EnvironmentKind(str, Enum):
    """How a solver's environment is entered on this machine."""

    POSIX = "posix"
    WINDOWS = "windows"
    WSL = "wsl"

    def __str__(self) -> str:
        return self.value


def default_kind() -> EnvironmentKind:
    """The kind that matches the host, used when a config does not say.

    Windows defaults to ``windows`` rather than ``wsl``: a native install is the
    simpler thing to get working, and a user who needs WSL (Foundation OpenFOAM) has
    to name a distro anyway, so it cannot be inferred.
    """
    return EnvironmentKind.WINDOWS if os.name == "nt" else EnvironmentKind.POSIX


def default_mpi_launcher(kind: EnvironmentKind) -> str:
    """``mpiexec`` on native Windows (MS-MPI), ``mpirun`` elsewhere."""
    return "mpiexec" if kind is EnvironmentKind.WINDOWS else "mpirun"


@dataclass
class EnvStatus:
    """The result of probing an environment. Never raises; reports."""

    ok: bool
    detail: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    # Sentinels that were already in the ambient environment before the setup script
    # ran. Present is not the same as provided: this machine has
    # /etc/profile.d/openfoam9.sh, so WM_PROJECT_VERSION is inherited by every process
    # and a validation would pass even against a script that does nothing. A detached
    # job does not inherit an interactive environment, so that is precisely the
    # "works in my terminal, fails in the runner" trap.
    ambient: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class SolverEnvironment:
    """How to reach one solver installation."""

    kind: EnvironmentKind = field(default_factory=default_kind)
    setup_script: Path | str | None = None
    shell: str | None = None        # bundled bash (blueCFD / ESI / TELEMAC-Windows)
    distro: str | None = None       # WSL only
    mpi_launcher: str | None = None
    # extra variables forced onto the captured environment (rarely needed)
    overrides: dict[str, str] = field(default_factory=dict)
    #: Tri-state. ``None`` defers to the ``AXQUA_ENV_CAPTURED`` marker, which is how
    #: a detached child inherits the answer; True/False override it explicitly.
    _assume_entered: bool | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.kind, str):
            self.kind = EnvironmentKind(self.kind)
        if self.mpi_launcher is None:
            self.mpi_launcher = default_mpi_launcher(self.kind)

    # ------------------------------------------------------------------ paths
    @property
    def script(self) -> str | None:
        """The setup script as the *target* environment spells it."""
        if self.setup_script is None:
            return None
        return self.translate(self.setup_script)

    def translate(self, path: Path | str) -> str:
        """A host path as the target environment spells it.

        Only the WSL boundary actually translates: ``C:\\scratch\\case`` becomes
        ``/mnt/c/scratch/case``. Everything else is the identity, which is why this is
        safe to call unconditionally at every boundary - and why it must exist in one
        place rather than being reinvented per call site.
        """
        text = str(path)
        if self.kind is not EnvironmentKind.WSL:
            return text
        return windows_to_wsl(text)

    def untranslate(self, path: str) -> str:
        """A target-environment path as the host spells it (the WSL inverse)."""
        if self.kind is not EnvironmentKind.WSL:
            return path
        return wsl_to_windows(path)

    # --------------------------------------------------------------- launching
    def command(self, command: str) -> list[str]:
        """The argv that runs *command* inside this environment.

        A list, never a shell string: the only place a shell parses anything is inside
        the quoted payload we build ourselves, so user-supplied paths cannot reach a
        command line unquoted.

        When the environment has **already been entered** (see :attr:`assume_entered`)
        the setup script is skipped. That is not an optimisation: a detached job is
        launched with a captured environment set directly on the process, so re-sourcing
        would both be redundant and put a shell back inside the job's process tree -
        which is exactly the layer that makes tree-kill unreliable.
        """
        if self.assume_entered:
            return self._bare_command(command)
        if self.kind is EnvironmentKind.WINDOWS and not self._uses_bash:
            return self._windows_command(command)
        return self._bash_command(command)

    @property
    def assume_entered(self) -> bool:
        """True when this process already carries the solver environment.

        Set explicitly on the dataclass, or inherited from the marker a detached
        launcher exports - so the child axqua discovers it without being told.
        WSL is excluded unconditionally: a captured *Linux* environment cannot launch a
        Windows process, so those commands must go through ``wsl.exe`` regardless.
        """
        if self.kind is EnvironmentKind.WSL:
            return False
        if self._assume_entered is not None:
            return bool(self._assume_entered)
        return os.environ.get(ENTERED_MARKER, "") == "1"

    @assume_entered.setter
    def assume_entered(self, value: bool | None) -> None:
        self._assume_entered = value

    def _bare_command(self, command: str) -> list[str]:
        """Run *command* with no setup script and no login shell.

        A shell is still involved because the payload is a shell string (it uses
        ``command -v`` and quoting), but it is a plain non-login ``-c``: one thin layer,
        not a login shell re-reading profile scripts that could contradict the captured
        environment.
        """
        if os.name == "nt" and not self._uses_bash:  # pragma: no cover - Windows only
            return ["cmd", "/c", command]
        shell = str(self.shell) if self.shell else "bash"
        return [shell, "-c", command]

    @property
    def _uses_bash(self) -> bool:
        """A Windows install may ship its own bash (blueCFD, ESI, MSYS TELEMAC)."""
        return bool(self.shell) and "bash" in Path(str(self.shell)).name.lower()

    def _bash_command(self, command: str, script: str | None = _UNSET) -> list[str]:
        """*script* defaults to this environment's own; pass ``None`` for a baseline
        shell that sources nothing (see :meth:`capture`)."""
        shell = str(self.shell) if self.shell else "bash"
        if script is _UNSET:
            script = self.script
        if script:
            # `set -e` so a failing source aborts before the command runs
            payload = f"set -e; source {shlex.quote(script)}; {command}"
        else:
            payload = command
        argv = [shell, "-lc", payload]
        if self.kind is EnvironmentKind.WSL:
            return self._wsl_prefix() + argv
        return argv

    def _windows_command(self, command: str) -> list[str]:
        if self.script:
            payload = f'call "{self.script}" && {command}'
        else:
            payload = command
        return ["cmd", "/c", payload]

    def _wsl_prefix(self) -> list[str]:
        prefix = ["wsl.exe"]
        if self.distro:
            prefix += ["-d", self.distro]
        return prefix + ["--"]

    def mpi_command(self, command: str, processes: int) -> str:
        """Prefix *command* with this environment's MPI launcher.

        ``mpirun`` is not universal - MS-MPI provides ``mpiexec`` - so the launcher is
        a property of the installation rather than a constant in the solver code.
        """
        if not processes or processes <= 1:
            return command
        return f"{self.mpi_launcher} -np {int(processes)} {command}"

    # --------------------------------------------------------------- capturing
    def capture(self, *, timeout: float = 120.0,
                with_script: bool = True) -> dict[str, str]:
        """Source the setup script once and return the resulting environment.

        The dictionary can then be handed to a detached process directly, with no
        shell left inside its tree.

        *with_script* False runs the same shell **without** sourcing anything, giving
        the baseline the shell provides on its own. That is what makes provenance
        answerable: a login shell reads ``/etc/profile.d``, so a machine with a
        system-wide OpenFOAM hands every capture ``WM_PROJECT_VERSION`` whether the
        configured script did anything or not.
        """
        script = self.script if with_script else None
        if not script and not with_script and self.kind is EnvironmentKind.WINDOWS \
                and not self._uses_bash:
            argv = ["cmd", "/c", f"echo {_MARKER}&& set"]
            parse = _parse_set
        elif not script:
            if with_script:
                return dict(os.environ)
            argv = self._bash_command(f"printf '%s' {_MARKER}; env -0", script=None)
            parse = _parse_env0
        elif self.kind is EnvironmentKind.WINDOWS and not self._uses_bash:
            argv = ["cmd", "/c", f'call "{script}" && echo {_MARKER}&& set']
            parse = _parse_set
        else:
            # NUL-delimited: a value may legitimately contain newlines, and PATH-like
            # variables do often enough that line-splitting silently truncates them
            argv = self._bash_command(f"printf '%s' {_MARKER}; env -0")
            parse = _parse_env0
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise EnvironmentCaptureError(
                f"could not enter the {self.kind} environment via {self.setup_script}: "
                f"{(proc.stderr or proc.stdout or '').strip()[:400]}")
        captured = parse(_after_marker(proc.stdout))
        captured.update(self.overrides)
        return captured

    def validate(self, solver: str | None = None, *,
                 timeout: float = 120.0) -> EnvStatus:
        """Probe the environment. Never raises - a broken install is an answer.

        With *solver* (``"telemac"`` / ``"openfoam"``) the capture is additionally
        checked for that solver's sentinel variables, which is what turns "the script
        ran" into "this is a working installation".
        """
        if self.setup_script and not self._script_exists():
            return EnvStatus(False, f"setup script not found: {self.setup_script}")
        try:
            captured = self.capture(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - reporting, not control flow
            return EnvStatus(False, f"{type(exc).__name__}: {exc}")

        sentinels = SENTINELS.get(solver or "", ())
        missing = tuple(name for name in sentinels if not captured.get(name))
        if missing:
            return EnvStatus(
                False,
                f"entered the environment, but {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} unset - is "
                f"{self.setup_script} the right script for {solver}?",
                variables=captured, missing=missing)

        # Provenance. Comparing against os.environ is not enough: `bash -lc` is a
        # LOGIN shell and reads /etc/profile.d, so on a machine with a system-wide
        # OpenFOAM the sentinels arrive whether the configured script did anything or
        # not. The baseline therefore goes through the same shell, minus the source.
        try:
            baseline = self.capture(timeout=timeout, with_script=False)
        except Exception:  # noqa: BLE001 - provenance is a nicety, not a gate
            baseline = {}
        ambient = tuple(name for name in sentinels if baseline.get(name))
        detail = ""
        if solver == "openfoam":
            detail = f"OpenFOAM {captured.get('WM_PROJECT_VERSION', '?')}"
        elif solver == "telemac":
            detail = f"TELEMAC at {captured.get('HOMETEL', '?')}"
        if ambient and len(ambient) == len(sentinels):
            detail += (" - NOTE: already present in the ambient environment, so this "
                       "setup script may not be what provides it. A detached job does "
                       "not inherit your shell.")
        return EnvStatus(True, detail, variables=captured, ambient=ambient)

    def _script_exists(self) -> bool:
        """Whether the setup script is present, as the *host* can see it.

        Under WSL the script path is a Linux path the host cannot stat, so existence
        is left to the capture to discover - claiming "not found" for a path that is
        simply on the other side of the boundary would be worse than not checking.
        """
        if self.kind is EnvironmentKind.WSL:
            return True
        return Path(str(self.setup_script)).is_file()

    # ---------------------------------------------------------------- reporting
    def describe(self) -> str:
        """One line for a status report or a ``MODEL=`` marker."""
        parts = [str(self.kind)]
        if self.distro:
            parts.append(self.distro)
        if self.setup_script:
            parts.append(Path(str(self.setup_script)).name)
        return " / ".join(parts)

    @classmethod
    def from_config(cls, block, legacy_script=None) -> "SolverEnvironment":
        """Build from a config block, honouring the pre-environment spelling.

        A config that only carries ``telemac.pysource`` (every case written before this
        existed) resolves to a POSIX environment against that script, so nothing has to
        be migrated for it to keep working.
        """
        if block is None:
            return cls(kind=EnvironmentKind.POSIX, setup_script=legacy_script)
        as_dict = block if isinstance(block, dict) else block.__dict__
        return cls(
            kind=as_dict.get("kind") or as_dict.get("environment") or default_kind(),
            setup_script=as_dict.get("setup_script") or legacy_script,
            shell=as_dict.get("shell"),
            distro=as_dict.get("distro"),
            mpi_launcher=as_dict.get("mpi_launcher"),
            overrides=dict(as_dict.get("overrides") or {}),
        )


class EnvironmentCaptureError(RuntimeError):
    """Raised when a setup script cannot be entered."""


# --------------------------------------------------------------------------- #
# parsing captured environments
# --------------------------------------------------------------------------- #


def _after_marker(text: str) -> str:
    """Everything the setup script printed before the dump, discarded.

    A setup script is entitled to talk - TELEMAC's prints warnings and a banner - and
    that output is not environment. Splitting on the last occurrence is deliberate:
    the marker string could legitimately appear inside an exported value.
    """
    head, sep, tail = text.rpartition(_MARKER)
    return tail if sep else text


def _parse_env0(text: str) -> dict[str, str]:
    """Parse ``env -0`` output (NUL-separated ``NAME=value`` records)."""
    out: dict[str, str] = {}
    for record in text.split("\0"):
        name, sep, value = record.partition("=")
        if sep and name:
            out[name] = value
    return out


def _parse_set(text: str) -> dict[str, str]:
    """Parse ``cmd /c set`` output (``NAME=value`` per line).

    Multi-line values cannot be represented by ``set`` at all, so a line without an
    ``=`` is a continuation of the previous value rather than a new variable - which is
    how ``PATH`` survives on a machine with an unusual entry.
    """
    out: dict[str, str] = {}
    last: str | None = None
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name and not name.startswith(" "):
            out[name] = value
            last = name
        elif last is not None:
            out[last] += "\n" + line
    return out


# --------------------------------------------------------------------------- #
# WSL path translation
# --------------------------------------------------------------------------- #


def windows_to_wsl(path: str) -> str:
    """``C:\\scratch\\case`` -> ``/mnt/c/scratch/case``.

    Done arithmetically rather than by shelling out to ``wslpath``: this is called for
    every path crossing the boundary, and a subprocess per path would dominate the cost
    of building a case. A path that is already POSIX passes through unchanged, so the
    function is idempotent.
    """
    if not path or path.startswith("/"):
        return path.replace("\\", "/") if "\\" in path else path
    pure = PureWindowsPath(path)
    drive = pure.drive.rstrip(":").lower()
    if not drive:
        return path.replace("\\", "/")
    rest = "/".join(pure.parts[1:])
    return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"


def wsl_to_windows(path: str) -> str:
    """``/mnt/c/scratch/case`` -> ``C:\\scratch\\case`` (the inverse)."""
    pure = PurePosixPath(path)
    parts = pure.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "mnt" and len(parts[2]) == 1:
        drive = parts[2].upper()
        rest = "\\".join(parts[3:])
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    return path


def wsl_available() -> bool:
    """Whether ``wsl.exe`` can be found (Windows only, and cheap to ask)."""
    return os.name == "nt" and shutil.which("wsl.exe") is not None


def host_summary() -> str:
    """One line describing the host, for logs and status reports."""
    return f"{sys.platform} ({os.name}), default environment kind: {default_kind()}"
