"""Where hydromate keeps configuration, job state and scratch - platform-aware.

Four separate roles, kept apart because they have different backup, size and
portability characteristics:

``config_dir``
    Hand-edited settings that belong to the user: ``profiles.yml``. Small, worth
    backing up, never machine-generated in bulk.
``data_dir``
    Derived state hydromate owns: ``jobs.sqlite``. Reconstructible (see
    :mod:`hydromate.jobs.index`), so losing it costs a rescan and nothing else.
``state_dir`` / ``cache_dir``
    Runtime scraps and throwaway intermediates.

The **job root** is separate from all four and deliberately overridable: CFD output runs
to tens of gigabytes and belongs on a scratch volume, not under ``~``.

No ``platformdirs`` dependency. It would be ~40 lines saved against a new runtime
requirement on a package whose dependency policy (plan §28) is to stay light, and the
QGIS plugin never imports hydromate, so there is no second consumer to share it with.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "hydromate"

#: Environment overrides, checked before any platform convention. These exist so a test
#: suite (and a sysadmin) can relocate everything without patching code; the autouse
#: fixture in ``tests/conftest.py`` sets all of them into ``tmp_path``.
ENV_CONFIG = "HYDROMATE_CONFIG_DIR"
ENV_DATA = "HYDROMATE_DATA_DIR"
ENV_STATE = "HYDROMATE_STATE_DIR"
ENV_CACHE = "HYDROMATE_CACHE_DIR"
ENV_JOB_ROOT = "HYDROMATE_JOB_ROOT"


def _env(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _windows_base(var: str, fallback: str) -> Path:
    raw = os.environ.get(var)
    if raw:
        return Path(raw)
    return Path.home() / fallback


def config_dir() -> Path:
    """User configuration: ``profiles.yml`` lives here."""
    override = _env(ENV_CONFIG)
    if override:
        return override
    if os.name == "nt":
        return _windows_base("APPDATA", "AppData/Roaming") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP_NAME


def data_dir() -> Path:
    """Derived, reconstructible state: the job index."""
    override = _env(ENV_DATA)
    if override:
        return override
    if os.name == "nt":
        return _windows_base("LOCALAPPDATA", "AppData/Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME


def state_dir() -> Path:
    override = _env(ENV_STATE)
    if override:
        return override
    if os.name == "nt" or sys.platform == "darwin":
        return data_dir()
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / APP_NAME


def cache_dir() -> Path:
    override = _env(ENV_CACHE)
    if override:
        return override
    if os.name == "nt":
        return _windows_base("LOCALAPPDATA", "AppData/Local") / APP_NAME / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / APP_NAME


def job_root(*, explicit: str | os.PathLike | None = None,
             profile_root: str | os.PathLike | None = None,
             config_root: str | os.PathLike | None = None) -> Path:
    """Resolve the job root, first hit wins.

    ``--job-root`` beats ``$HYDROMATE_JOB_ROOT`` beats the solver profile's
    ``working_root`` beats the case config's ``project.job_root`` beats
    ``data_dir()/jobs``. The ordering runs most-specific to least: an explicit flag is
    about *this* submission, a profile is about this machine, and the config is about
    this case.
    """
    for candidate in (explicit, _env(ENV_JOB_ROOT), profile_root, config_root):
        if candidate:
            return Path(candidate).expanduser().resolve()
    return data_dir() / "jobs"


def index_path() -> Path:
    """The SQLite job index. Derived - deleting it is safe, ``list --rebuild`` restores."""
    return data_dir() / "jobs.sqlite"


def profiles_path() -> Path:
    """The solver-profile file. Machine-local by design: it names install paths."""
    return config_dir() / "profiles.yml"


class JobDir:
    """Every path inside one job directory, in one place.

    The per-job directory is **authoritative** (plan §5); the SQLite index is derived
    from it. So this class is the schema of a job on disk, and anything that needs a path
    asks here rather than joining strings.
    """

    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)

    def __fspath__(self) -> str:
        return str(self.root)

    def __repr__(self) -> str:
        return f"JobDir({str(self.root)!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, JobDir) and self.root == other.root

    @property
    def job_id(self) -> str:
        return self.root.name

    # -- the five files at the top level -----------------------------------------
    @property
    def spec(self) -> Path:
        """``job.json`` - the immutable submission record."""
        return self.root / "job.json"

    @property
    def status(self) -> Path:
        """``status.json`` - mutable, replaced atomically, runner-only."""
        return self.root / "status.json"

    @property
    def lock(self) -> Path:
        return self.root / "job.lock"

    @property
    def cancel_request(self) -> Path:
        """Its own file, so the one-writer-per-file rule survives cancellation."""
        return self.root / "cancel.request"

    @property
    def runner_log(self) -> Path:
        """hydromate's narration. The solver's own listing is a separate artifact."""
        return self.root / "runner.log"

    @property
    def launch_env(self) -> Path:
        """The captured solver environment, as systemd's ``EnvironmentFile``."""
        return self.root / "launch.env"

    @property
    def answers(self) -> Path:
        """Answers to questions a study asked while detached."""
        return self.root / "answers.json"

    # -- the directories ---------------------------------------------------------
    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def frozen_config(self) -> Path:
        return self.input / "case-config.yml"

    @property
    def preprocessing(self) -> Path:
        return self.input / "preprocessing"

    @property
    def simulation(self) -> Path:
        return self.input / "simulation-cases"

    @property
    def solver_logs(self) -> Path:
        return self.root / "solver"

    @property
    def calibration(self) -> Path:
        return self.root / "hydrobayescal"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def results_json(self) -> Path:
        return self.results / "results.json"

    @property
    def qgis(self) -> Path:
        """Where :meth:`SolverBackend.export_qgis_results` writes. Never PyQGIS output."""
        return self.results / "qgis"

    def exists(self) -> bool:
        return self.spec.is_file()

    def create(self) -> "JobDir":
        """Claim the directory. Raises ``FileExistsError`` if the id is taken.

        The claim is the collision check - ``mkdir(exist_ok=False)`` is atomic, so two
        concurrent submits cannot both win the same id no matter how the tails were
        drawn.
        """
        self.root.mkdir(parents=True, exist_ok=False)
        for sub in (self.input, self.solver_logs, self.results, self.qgis):
            sub.mkdir(parents=True, exist_ok=True)
        return self
