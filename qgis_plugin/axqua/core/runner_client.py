"""Talking to axqua - over the command line, and only over the command line.

**The plugin never imports axqua.** That is not a stylistic preference; it is what
makes the whole thing work. QGIS ships its own Python, and axqua needs gmsh,
rasterio, geopandas and a solver's environment. Requiring those to coexist inside the
QGIS interpreter would make installation a support nightmare and would break the moment
QGIS updated. So the boundary is a subprocess and a JSON document, and either side can
be reinstalled without touching the other.

Everything here is therefore small and cheap: a job id, a state, a path. No field arrays
ever cross this line (plan §26).

**Discovery** follows the plan's order exactly, and fails loudly rather than falling back
to something that might be a different axqua:

1. the path configured in the plugin's settings;
2. ``$AXQUA_EXE``;
3. ``shutil.which("axqua")``;
4. otherwise an error naming all three.

The find is validated by running ``axqua --version`` **when it is configured**, not
when a job is submitted. A wrong or half-installed executable discovered at submit time
is a mystery; discovered in the settings dialog it is a sentence.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Sequence

ENV_VAR = "AXQUA_EXE"
#: The pre-rename spelling. Still honoured, because a shell profile written before the
#: rename is not something the plugin should silently ignore.
LEGACY_ENV_VAR = "HYDROMATE_EXE"
SETTINGS_KEY = "axqua/executable"

#: Short calls only. Nothing here ever runs a solver - that is the whole point of the
#: job runner - so a call that takes this long has gone wrong.
DEFAULT_TIMEOUT = 60.0


class RunnerError(RuntimeError):
    """A axqua call failed. Carries the structured error when there was one."""

    def __init__(self, message: str, *, code: str = "", remedy: str = "",
                 subject: str = "", returncode: int | None = None,
                 stderr: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.remedy = remedy
        self.subject = subject
        self.returncode = returncode
        self.stderr = stderr

    def user_text(self) -> str:
        parts = [self.message]
        if self.remedy:
            parts.append(self.remedy)
        return "\n".join(parts)


class RunnerNotFound(RunnerError):
    """axqua could not be located. The message names every place looked."""


@dataclass
class RunnerInfo:
    """What a validated executable turned out to be."""

    path: str
    version: str
    source: str          # settings | environment | PATH

    def describe(self) -> str:
        return f"axqua {self.version} at {self.path} (found via {self.source})"


@dataclass
class Result:
    """One CLI call's outcome."""

    ok: bool
    command: str
    data: Any = None
    error: dict[str, Any] = field(default_factory=dict)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def find_executable(configured: str | None = None) -> RunnerInfo:
    """Locate and validate axqua, or raise with an actionable message."""
    candidates: list[tuple[str, str]] = []
    if configured:
        candidates.append((str(configured), "settings"))
    from_env = os.environ.get(ENV_VAR) or os.environ.get(LEGACY_ENV_VAR)
    if from_env:
        candidates.append((from_env, "environment"))
    on_path = shutil.which("axqua")
    if on_path:
        candidates.append((on_path, "PATH"))

    problems: list[str] = []
    for path, source in candidates:
        try:
            version = _probe(path)
        except RunnerError as exc:
            problems.append(f"  {source}: {path} - {exc.message}")
            continue
        return RunnerInfo(path=path, version=version, source=source)

    # Never a silent fallback to some other interpreter: say what was tried.
    lines = ["axqua could not be found.", "", "Looked in:",
             f"  1. the plugin setting ({configured or 'not set'})",
             f"  2. ${ENV_VAR} ({from_env or 'not set'})",
             f"  3. PATH ({on_path or 'not found'})"]
    if problems:
        lines += ["", "What was tried:"] + problems
    lines += ["", "Install it with 'pip install axqua' in the environment that has "
              "your solver tooling, then set the path in aXqua > Settings."]
    raise RunnerNotFound("\n".join(lines))


def _probe(path: str) -> str:
    """Run ``--version`` and check it parses. A wrong binary is caught here."""
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True,
                              timeout=30, **_no_window())
    except FileNotFoundError:
        raise RunnerError("no such file") from None
    except PermissionError:
        raise RunnerError("not executable") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerError(str(exc)) from exc
    text = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RunnerError(f"'--version' exited with {proc.returncode}: {text[:200]}")
    if not text.lower().startswith("axqua"):
        # Something else answered. Reporting it as a wrong executable is far kinder than
        # letting it fail obscurely at submit time.
        raise RunnerError(f"this does not look like axqua (said {text[:60]!r})")
    return text.split()[-1]


def _no_window() -> dict:
    """Stop a console window flashing up on Windows for every call."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()          # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW   # type: ignore[attr-defined]
    return {"startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NO_WINDOW}    # type: ignore[attr-defined]


class RunnerClient:
    """A thin, synchronous wrapper around the axqua CLI.

    Synchronous on purpose: every call is short. Callers that must not block the GUI
    wrap *this* in a ``QgsTask`` (see :mod:`axqua_plugin.core.tasks`), which is the
    right place for the threading - a client that started threads itself could not be
    used from a Processing algorithm or a test.
    """

    def __init__(self, executable: str | None = None, *,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self._configured = executable
        self.timeout = timeout
        self._info: RunnerInfo | None = None

    # -- discovery ----------------------------------------------------------------
    @property
    def info(self) -> RunnerInfo:
        if self._info is None:
            self._info = find_executable(self._configured)
        return self._info

    @property
    def executable(self) -> str:
        return self.info.path

    def validate(self) -> RunnerInfo:
        """Re-probe, forgetting any cached answer. What the settings dialog calls."""
        self._info = None
        return self.info

    # -- the call -----------------------------------------------------------------
    def call(self, args: Sequence[str], *, timeout: float | None = None,
             expect_json: bool = True) -> Result:
        """Run one axqua command.

        The argument list is passed as a **list**, never joined into a shell string, so
        a path with a space or a quote in it cannot become two arguments or a second
        command (plan §7).
        """
        argv = [self.executable, *[str(a) for a in args]]
        if expect_json and "--json" not in argv:
            argv.append("--json")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout or self.timeout, **_no_window())
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(
                f"axqua did not answer within {timeout or self.timeout:.0f}s",
                remedy="The job root may be on a slow or unreachable filesystem.",
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError(f"could not run axqua: {exc}") from exc

        if not expect_json:
            return Result(ok=proc.returncode == 0, command=" ".join(args),
                          data=proc.stdout, returncode=proc.returncode,
                          stdout=proc.stdout, stderr=proc.stderr)

        payload = _parse(proc.stdout)
        if payload is None:
            raise RunnerError(
                "axqua returned something that is not JSON",
                remedy="Run the same command in a terminal to see what it printed.",
                returncode=proc.returncode,
                stderr=(proc.stderr or proc.stdout)[-2000:],
            )
        result = Result(ok=bool(payload.get("ok")), command=str(payload.get("command", "")),
                        data=payload.get("data"), error=payload.get("error") or {},
                        returncode=proc.returncode, stdout=proc.stdout,
                        stderr=proc.stderr)
        if not result.ok:
            error = result.error
            raise RunnerError(str(error.get("message") or "axqua reported a failure"),
                              code=str(error.get("code") or ""),
                              remedy=str(error.get("remedy") or ""),
                              subject=str(error.get("subject") or ""),
                              returncode=proc.returncode, stderr=proc.stderr)
        return result

    # -- the verbs the plugin uses ------------------------------------------------
    def case_status(self, config: str | os.PathLike, *, check_env: bool = False) -> dict:
        """The capability matrix. **This is what generates the tabs** (plan §17)."""
        args = ["case-status", str(config), "--no-write"]
        if check_env:
            args.append("--check-env")
        return self.call(args).data

    def list_jobs(self, *, job_root: str | None = None, state: str | None = None,
                  case: str | None = None, rebuild: bool = False) -> list[dict]:
        args = ["list"]
        if job_root:
            args += ["--job-root", str(job_root)]
        if state:
            args += ["--state", state]
        if case:
            args += ["--case", case]
        if rebuild:
            args.append("--rebuild")
        return list(self.call(args).data or [])

    def job_status(self, job_id: str, *, job_root: str | None = None) -> dict:
        args = ["status", job_id]
        if job_root:
            args += ["--job-root", str(job_root)]
        return self.call(args).data

    def submit(self, config: str | os.PathLike, kind: str, *,
               profile: str | None = None, job_root: str | None = None,
               processes: int | None = None, options: dict | None = None,
               launcher: str | None = None, dry_run: bool = False) -> dict:
        args = ["submit", str(config), "--kind", kind]
        if profile:
            args += ["--profile", profile]
        if job_root:
            args += ["--job-root", str(job_root)]
        if processes:
            args += ["--np", str(int(processes))]
        if launcher and launcher != "auto":
            args += ["--launcher", launcher]
        for key, value in (options or {}).items():
            args += ["--option", f"{key}={json.dumps(value)}"]
        if dry_run:
            args.append("--dry-run")
        return self.call(args, timeout=180.0).data

    def cancel(self, job_id: str, *, job_root: str | None = None,
               timeout: float = 20.0) -> dict:
        args = ["cancel", job_id, "--timeout", str(timeout)]
        if job_root:
            args += ["--job-root", str(job_root)]
        # The call must outlast the grace period it is asking for.
        return self.call(args, timeout=timeout + 30.0).data

    def log_path(self, job_id: str, *, solver: bool = False,
                 job_root: str | None = None) -> str | None:
        args = ["logs", job_id, "--path"]
        if solver:
            args.append("--solver")
        if job_root:
            args += ["--job-root", str(job_root)]
        try:
            return (self.call(args).data or {}).get("path")
        except RunnerError:
            # No log yet is a normal state early in a job, not something to raise about.
            return None

    def profiles(self) -> dict:
        return self.call(["profiles", "list"]).data or {}

    def kinds(self) -> list[dict]:
        return list(self.call(["submit", "x", "--help-kinds"]).data or [])


def _parse(text: str) -> dict | None:
    """Read the JSON document, tolerating anything printed before it.

    axqua sends narration to stderr precisely so this is unnecessary - but a stray
    warning from a library on stdout should degrade the message, not the feature.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            return None
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None
