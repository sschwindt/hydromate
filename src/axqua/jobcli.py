"""The job verbs: ``submit``, ``execute``, ``status``, ``cancel``, ``logs``, ``list``.

Kept out of ``cli.py`` so the build commands and the job commands stay legible apart, and
because a QGIS plugin's entire view of axqua is this file's output. Two properties
matter more than the exact spelling of any flag:

**Machine-readable output.** Every verb takes ``--json`` and emits one envelope shape:
``{"ok", "command", "axqua", "data", "error"}``. The document goes to **stdout** and
all narration to stderr, so a caller can parse stdout unconditionally. Plan §11 is
explicit that QGIS must never parse human-readable console logs to obtain state, and this
is what makes that possible.

**Exit codes that mean something.** A uniform ``1`` forces a GUI to parse text to find
out what went wrong. The ``AxquaError`` categories already carry stable codes, so they
map onto distinct exit codes here.

The ``status`` collision, resolved by a rule
--------------------------------------------
``axqua status <config>`` has always meant *case* status. The job verb wants the same
word. Rather than break the existing spelling or guess, the argument decides:

* an existing **path** -> case status (with a deprecation notice pointing at
  ``case-status``);
* something matching ``JOB_ID_RE`` -> job status.

A job id is never a path and a path never matches the job-id grammar, so the two cannot be
confused - and the rule is tested in both directions.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("axqua")

#: Exit codes by error category, so a caller can branch without parsing a message.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_GEODATA = 3
EXIT_ENVIRONMENT = 4
EXIT_SOLVER = 5
EXIT_MESH = 6
EXIT_CANCELLED = 130

_CODE_TO_EXIT = {
    "axqua.config": EXIT_CONFIG,
    "axqua.geodata": EXIT_GEODATA,
    "axqua.environment": EXIT_ENVIRONMENT,
    "axqua.solver": EXIT_SOLVER,
    "axqua.mesh": EXIT_MESH,
}


# --------------------------------------------------------------------------- output


def emit(command: str, data: Any, *, as_json: bool, lines: list[str] | None = None
         ) -> int:
    """Print a success result in whichever form was asked for."""
    if as_json:
        from axqua import __version__
        print(json.dumps({"ok": True, "command": command, "axqua": __version__,
                          "data": data, "error": None}, indent=2, default=str))
    else:
        for line in lines or []:
            print(line)
    return EXIT_OK


def fail(command: str, exc: BaseException, *, as_json: bool, verbose: bool = False
         ) -> int:
    """Report a failure and choose the exit code from its category."""
    from axqua import __version__
    from axqua.core.errors import ErrorRecord

    record = ErrorRecord.from_exception(exc)
    if as_json:
        print(json.dumps({"ok": False, "command": command, "axqua": __version__,
                          "data": None, "error": record.as_dict()}, indent=2,
                         default=str))
    else:
        log.error("%s", record.message)
        if record.subject:
            log.error("  at: %s", record.subject)
        if record.remedy:
            log.error("  %s", record.remedy)
    if verbose and not isinstance(exc, Exception):
        raise exc
    if verbose:
        log.debug("traceback", exc_info=(type(exc), exc, exc.__traceback__))
    return _CODE_TO_EXIT.get(record.code, EXIT_ERROR)


def _common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="emit a machine-readable result on stdout")
    return p


def _setup_logging(args) -> None:
    """Narration to stderr, always - stdout belongs to the JSON document."""
    from axqua.logsetup import setup_logging
    setup_logging(level=logging.DEBUG if args.verbose else logging.INFO)
    for handler in logging.getLogger("axqua").handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.stream = sys.stderr


# --------------------------------------------------------------------------- submit


def _submit_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua submit",
        description="Create a persistent job and start it detached. Returns the job "
                    "id immediately; the solver keeps running when this shell, and "
                    "QGIS, are gone.")
    p.add_argument("target", type=Path,
                   help="a case configuration (with --kind), or an existing job.json")
    p.add_argument("--kind", help="what to run; see 'axqua submit --help-kinds'")
    p.add_argument("--help-kinds", action="store_true",
                   help="list the job kinds and exit")
    p.add_argument("--profile", help="solver profile from profiles.yml")
    p.add_argument("--job-root", type=Path, help="where the job directory is created")
    p.add_argument("--np", type=int, help="MPI processes (overrides profile and config)")
    p.add_argument("--launcher", default="auto",
                   choices=["auto", "systemd", "posix", "windows", "wsl"])
    p.add_argument("--workspace", choices=["job", "case"],
                   help="run inside the job directory, or in the case's own folders")
    p.add_argument("--from-job", dest="source_job",
                   help="reuse a previous job's build without copying it")
    p.add_argument("--option", action="append", default=[], metavar="KEY=VALUE",
                   help="a per-kind option; repeatable")
    p.add_argument("--no-validate-env", action="store_true",
                   help="skip probing the solver environment before submitting")
    p.add_argument("--dry-run", action="store_true",
                   help="print the job that would be submitted, and create nothing")
    return _common(p)


def run_submit(argv: list[str]) -> int:
    args = _submit_parser().parse_args(argv)
    _setup_logging(args)
    from axqua.jobs.model import KIND_META

    if args.help_kinds:
        return emit("submit.kinds",
                    [{"kind": k.value, "solver": m.solver, "title": m.title}
                     for k, m in KIND_META.items()],
                    as_json=args.as_json,
                    lines=[f"{k.value:<24}{m.solver:<10}{m.title}"
                           for k, m in KIND_META.items()])
    try:
        from axqua.config import load_config
        from axqua.jobs import submit as submit_mod
        from axqua.jobs.profiles import resolve as resolve_profile

        if args.target.suffix == ".json":
            return _submit_existing_spec(args)
        if not args.kind:
            from axqua.core.errors import ConfigError
            raise ConfigError(
                "submitting a case configuration needs --kind",
                subject="kind",
                remedy="Run 'axqua submit --help-kinds' to see them.",
            )
        cfg = load_config(args.target)
        profile = resolve_profile(args.profile, solver=None, cfg=cfg) \
            if args.profile else None
        result = submit_mod.submit_job(
            cfg, args.kind, profile=profile, options=_options(args.option),
            job_root=args.job_root, np=args.np, workspace=args.workspace,
            source_job=args.source_job, launcher=args.launcher,
            source_config=args.target, validate_env=not args.no_validate_env,
            dry_run=args.dry_run)
        if args.dry_run:
            return emit("submit.dry-run", result.as_dict(), as_json=args.as_json,
                        lines=[json.dumps(result.as_dict(), indent=2, default=str)])
        # The job id on its own line: the plan's contract is that `submit` returns at
        # minimum a JOB_ID, and a caller that does not want JSON should be able to use
        # `$(axqua submit ...)` directly.
        print(result.job_id)
        return emit("submit", {"job_id": result.job_id, "job_dir": str(result.root)},
                    as_json=args.as_json) if args.as_json else EXIT_OK
    except BaseException as exc:  # noqa: BLE001
        return fail("submit", exc, as_json=args.as_json, verbose=args.verbose)


def _submit_existing_spec(args) -> int:
    """``axqua submit job.json`` - the plan's literal form."""
    from axqua.jobs.paths import JobDir
    payload = json.loads(args.target.read_text(encoding="utf-8"))
    jd = JobDir(args.target.parent)
    if not jd.spec.exists():
        from axqua.core.errors import ConfigError
        raise ConfigError(
            f"{args.target} is not inside a prepared job directory",
            subject=str(args.target),
            remedy="Submit a case configuration with --kind instead, which creates one.",
        )
    from axqua.jobs import executor
    code = executor.execute(jd)
    return emit("submit", {"job_id": payload.get("job_id"), "exit_code": code},
                as_json=args.as_json, lines=[str(payload.get("job_id"))])


def _options(pairs: list[str]) -> dict[str, Any]:
    """Parse ``--option k=v``, guessing JSON first so numbers and lists work."""
    out: dict[str, Any] = {}
    for item in pairs:
        key, sep, value = str(item).partition("=")
        if not sep:
            from axqua.core.errors import ConfigError
            raise ConfigError(f"--option needs KEY=VALUE, got {item!r}",
                              subject="option")
        try:
            out[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            out[key.strip()] = value
    return out


# -------------------------------------------------------------------------- execute


def _execute_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua execute",
        description="Run a job synchronously in this shell. The debugging path, and "
                    "what every detached launcher ultimately starts - so a failure "
                    "here reproduces exactly what a submitted job does.")
    p.add_argument("target", help="a job id, a job directory, or a job.json")
    p.add_argument("--job-root", type=Path)
    return _common(p)


def run_execute(argv: list[str]) -> int:
    args = _execute_parser().parse_args(argv)
    _setup_logging(args)
    try:
        from axqua.jobs import executor
        jd = _resolve_job(args.target, job_root=args.job_root)
        # console=False: _setup_logging already attached a console handler, so a second
        # one would print every line twice.
        code = executor.execute(jd, console=False)
        from axqua.jobs.store import read_status
        status = read_status(jd)
        return emit("execute",
                    {"job_id": jd.job_id, "exit_code": code,
                     "state": status.state.value if status else None},
                    as_json=args.as_json) if args.as_json else code
    except BaseException as exc:  # noqa: BLE001
        return fail("execute", exc, as_json=args.as_json, verbose=args.verbose)


# --------------------------------------------------------------------------- status


def _job_status_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua status",
        description="Report one job's current state. Reconciles a recorded process "
                    "that is no longer there, so a job never sticks in RUNNING.")
    p.add_argument("job", help="job id or job directory")
    p.add_argument("--job-root", type=Path)
    p.add_argument("--watch", type=float, nargs="?", const=2.0, metavar="SECONDS",
                   help="re-read until the job reaches a terminal state")
    return _common(p)


def run_job_status(argv: list[str]) -> int:
    args = _job_status_parser().parse_args(argv)
    _setup_logging(args)
    try:
        from axqua.jobs.reaper import reconcile
        jd = _resolve_job(args.job, job_root=args.job_root)
        while True:
            status = reconcile(jd)
            if status is None:
                from axqua.core.errors import ConfigError
                raise ConfigError(f"job {jd.job_id} has no status yet",
                                  subject=jd.job_id)
            if not args.watch or status.state.is_terminal:
                break
            _print_status(status, jd)
            time.sleep(max(0.5, float(args.watch)))
        return emit("job.status", status.as_dict(), as_json=args.as_json,
                    lines=_status_lines(status, jd))
    except BaseException as exc:  # noqa: BLE001
        return fail("job.status", exc, as_json=args.as_json, verbose=args.verbose)


def _print_status(status, jd) -> None:
    for line in _status_lines(status, jd):
        print(line)


def _status_lines(status, jd) -> list[str]:
    lines = [f"{status.job_id}  {status.state}"]
    if status.kind:
        lines.append(f"  kind      {status.kind.value} ({status.solver})")
    if status.phase:
        lines.append(f"  phase     {status.phase}")
    progress = status.progress
    if progress is not None:
        payload = progress.as_dict() if hasattr(progress, "as_dict") else dict(progress)
        shown = {k: v for k, v in payload.items()
                 if k != "kind" and v not in (None, "", 0)}
        if shown:
            lines.append("  progress  "
                         + "  ".join(f"{k}={v}" for k, v in shown.items()))
    for label, value in (("started", status.started), ("updated", status.updated),
                         ("finished", status.finished)):
        if value:
            lines.append(f"  {label:<9} {value}")
    if status.error:
        lines.append(f"  ERROR     [{status.error.get('code')}] "
                     f"{status.error.get('message')}")
        if status.error.get("remedy"):
            lines.append(f"            {status.error['remedy']}")
    lines.append(f"  directory {jd.root}")
    return lines


# --------------------------------------------------------------------------- cancel


def _cancel_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua cancel",
        description="Stop a job and its entire process tree, MPI ranks included.")
    p.add_argument("job", help="job id or job directory")
    p.add_argument("--job-root", type=Path)
    p.add_argument("--timeout", type=float, default=20.0,
                   help="seconds to wait for a clean stop before killing (default 20)")
    return _common(p)


def run_cancel(argv: list[str]) -> int:
    args = _cancel_parser().parse_args(argv)
    _setup_logging(args)
    try:
        from axqua.jobs.launcher import LaunchHandle, launcher_for
        from axqua.jobs.model import JobState
        from axqua.jobs.reaper import reconcile
        from axqua.jobs.store import read_status

        jd = _resolve_job(args.job, job_root=args.job_root)
        status = read_status(jd)
        if status is None:
            from axqua.core.errors import ConfigError
            raise ConfigError(f"job {jd.job_id} has no status", subject=jd.job_id)
        if status.state.is_terminal:
            return emit("cancel", {"job_id": jd.job_id, "state": status.state.value,
                                   "changed": False}, as_json=args.as_json,
                        lines=[f"{jd.job_id} is already {status.state}"])

        # The request file first: the running executor notices it and tears itself down
        # cleanly, writing CANCELLED itself. That is much better than a kill, because it
        # lets the job close its files and record where it got to.
        jd.cancel_request.write_text(
            json.dumps({"requested": _now(), "by": "axqua cancel"}) + "\n",
            encoding="utf-8")
        log.info("cancellation requested for %s", jd.job_id)

        deadline = time.monotonic() + max(1.0, args.timeout)
        while time.monotonic() < deadline:
            current = read_status(jd)
            if current and current.state.is_terminal:
                return emit("cancel", {"job_id": jd.job_id,
                                       "state": current.state.value, "changed": True},
                            as_json=args.as_json,
                            lines=[f"{jd.job_id} stopped cleanly: {current.state}"])
            time.sleep(0.5)

        # It did not notice - too busy inside the solver, or already wedged. Now the
        # launcher tears down the whole tree.
        if status.launcher:
            handle = LaunchHandle.from_dict(status.launcher)
            log.info("tree-killing via the %s launcher", handle.backend)
            launcher_for(handle).cancel(handle, grace=args.timeout)
        final = reconcile(jd) or status
        if final.state is not JobState.CANCELLED and not final.state.is_terminal:
            final.transition(JobState.CANCELLED)
            from axqua.jobs.store import write_status
            write_status(jd, final)
        return emit("cancel", {"job_id": jd.job_id, "state": final.state.value,
                               "changed": True}, as_json=args.as_json,
                    lines=[f"{jd.job_id} is now {final.state}"])
    except BaseException as exc:  # noqa: BLE001
        return fail("cancel", exc, as_json=args.as_json, verbose=args.verbose)


# ----------------------------------------------------------------------------- logs


def _logs_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua logs",
        description="Show or locate a job's log. Two streams: axqua's own "
                    "narration (default) and the solver's native listing (--solver).")
    p.add_argument("job", help="job id or job directory")
    p.add_argument("--job-root", type=Path)
    p.add_argument("--solver", action="store_true",
                   help="the solver's own output instead of runner.log")
    p.add_argument("--lines", type=int, default=50, help="tail this many lines")
    p.add_argument("--follow", "-f", action="store_true")
    p.add_argument("--path", action="store_true", help="print the path and nothing else")
    return _common(p)


def run_logs(argv: list[str]) -> int:
    args = _logs_parser().parse_args(argv)
    _setup_logging(args)
    try:
        jd = _resolve_job(args.job, job_root=args.job_root)
        path = _log_path(jd, solver=args.solver)
        if path is None:
            from axqua.core.errors import ConfigError
            raise ConfigError(
                f"no {'solver' if args.solver else 'runner'} log for {jd.job_id} yet",
                subject=str(jd.root))
        if args.path or args.as_json:
            return emit("logs", {"job_id": jd.job_id, "path": str(path)},
                        as_json=args.as_json, lines=[str(path)])
        _tail(path, args.lines, follow=args.follow)
        return EXIT_OK
    except BaseException as exc:  # noqa: BLE001
        return fail("logs", exc, as_json=args.as_json, verbose=args.verbose)


def _log_path(jd, *, solver: bool) -> Path | None:
    if not solver:
        return jd.runner_log if jd.runner_log.exists() else None
    if not jd.solver_logs.is_dir():
        return None
    # The most recently touched, which is the one a user asking "what is it doing" means.
    candidates = sorted(jd.solver_logs.glob("*.log"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _tail(path: Path, lines: int, *, follow: bool) -> None:
    with open(path, encoding="utf-8", errors="replace") as fh:
        buffered = fh.readlines()
        for line in buffered[-max(1, lines):]:
            sys.stdout.write(line)
        sys.stdout.flush()
        if not follow:
            return
        try:
            while True:
                chunk = fh.readline()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass


# ----------------------------------------------------------------------------- list


def _list_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua list",
        description="List known jobs. The job directories are authoritative; the "
                    "SQLite index is a cache, and --rebuild reconstructs it by "
                    "scanning them.")
    p.add_argument("--job-root", type=Path)
    p.add_argument("--state", help="filter by state (RUNNING, COMPLETED, ...)")
    p.add_argument("--kind")
    p.add_argument("--solver")
    p.add_argument("--case")
    p.add_argument("--since", help="ISO date; only jobs created on or after it")
    p.add_argument("--limit", type=int)
    p.add_argument("--rebuild", action="store_true",
                   help="rebuild the index from the job directories first")
    return _common(p)


def run_list(argv: list[str]) -> int:
    args = _list_parser().parse_args(argv)
    _setup_logging(args)
    try:
        from axqua.jobs.index import list_jobs
        rows = list_jobs(job_root=args.job_root, rebuild=args.rebuild,
                         state=args.state, kind=args.kind, solver=args.solver,
                         case=args.case, since=args.since, limit=args.limit)
        return emit("list", [r.as_dict() for r in rows], as_json=args.as_json,
                    lines=_list_lines(rows))
    except BaseException as exc:  # noqa: BLE001
        return fail("list", exc, as_json=args.as_json, verbose=args.verbose)


def _list_lines(rows) -> list[str]:
    if not rows:
        return ["no jobs"]
    header = f"{'JOB ID':<40}{'SOLVER':<10}{'KIND':<24}{'STATE':<18}CREATED"
    out = [header, "-" * len(header)]
    for row in rows:
        out.append(f"{row.job_id:<40}{(row.solver or ''):<10}{(row.kind or ''):<24}"
                   f"{(row.state or ''):<18}{row.created or ''}")
    return out


# ------------------------------------------------------------------------- profiles


def _profiles_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axqua profiles",
        description="Inspect the machine-local solver profiles. 'validate' probes "
                    "whether a profile can actually reach its solver - do this when "
                    "the profile is written, not when a multi-hour job is submitted.")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "show", "validate", "path"])
    p.add_argument("name", nargs="?")
    return _common(p)


def run_profiles(argv: list[str]) -> int:
    args = _profiles_parser().parse_args(argv)
    _setup_logging(args)
    try:
        from axqua.jobs.paths import profiles_path
        from axqua.jobs.profiles import load_profiles

        if args.action == "path":
            return emit("profiles.path", {"path": str(profiles_path())},
                        as_json=args.as_json, lines=[str(profiles_path())])
        found = load_profiles()
        if args.action == "list":
            return emit("profiles.list",
                        {name: p.as_dict() for name, p in found.items()},
                        as_json=args.as_json,
                        lines=([p.describe() for p in found.values()]
                               or [f"no profiles in {profiles_path()}"]))
        if args.action == "show":
            profile = _need_profile(found, args.name)
            return emit("profiles.show", profile.as_dict(), as_json=args.as_json,
                        lines=[profile.describe()]
                        + [f"  {k}: {v}" for k, v in profile.as_dict().items()])
        targets = ([_need_profile(found, args.name)] if args.name
                   else list(found.values()))
        report, lines, ok = {}, [], True
        for profile in targets:
            status = profile.validate()
            ok = ok and status.ok
            report[profile.name] = {"ok": status.ok, "detail": status.detail,
                                    "missing": list(status.missing),
                                    "ambient": list(status.ambient)}
            lines.append(f"{profile.name}: {'ok' if status.ok else 'UNUSABLE'} "
                         f"- {status.detail}")
            if status.ambient:
                lines.append("  warning: these came from the ambient shell, not the "
                             "setup script, so a detached job will not inherit them: "
                             + ", ".join(status.ambient))
        if not targets:
            lines = [f"no profiles in {profiles_path()}"]
        emit("profiles.validate", report, as_json=args.as_json, lines=lines)
        return EXIT_OK if ok else EXIT_ENVIRONMENT
    except BaseException as exc:  # noqa: BLE001
        return fail("profiles", exc, as_json=args.as_json, verbose=args.verbose)


def _need_profile(found: dict, name: str | None):
    from axqua.core.errors import ConfigError
    if not name:
        raise ConfigError("this action needs a profile name", subject="profile")
    if name not in found:
        raise ConfigError(f"no profile named {name!r}", subject=f"profiles.{name}",
                          remedy="Known: " + (", ".join(sorted(found)) or "none"))
    return found[name]


# -------------------------------------------------------------------------- helpers


def _resolve_job(target: str | Path, *, job_root: Path | None = None):
    """Accept a job id, a job directory, or a ``job.json`` inside one."""
    from axqua.core.errors import ConfigError
    from axqua.jobs import ids, paths
    from axqua.jobs.paths import JobDir

    text = str(target)
    path = Path(text)
    if path.name == "job.json" and path.is_file():
        return JobDir(path.parent)
    if path.is_dir():
        return JobDir(path)
    if ids.is_job_id(text):
        jd = JobDir(paths.job_root(explicit=job_root) / text)
        if jd.exists():
            return jd
        raise ConfigError(
            f"no job {text} under {paths.job_root(explicit=job_root)}",
            subject=text,
            remedy="Run 'axqua list' to see the jobs that exist, or pass "
                   "--job-root if it lives elsewhere.",
        )
    raise ConfigError(
        f"{text!r} is neither a job id nor a job directory",
        subject=text,
        remedy="Job ids look like 2026-08-14-isar-2025-steady-a3f19c.",
    )


def _now() -> str:
    from axqua.jobs.model import utc_now
    return utc_now()
