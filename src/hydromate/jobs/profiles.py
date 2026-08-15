"""Solver profiles: how to reach TELEMAC or OpenFOAM *on this machine*.

A profile is deliberately **machine-local** and lives in the user's config directory,
never in the project file. It names install paths, core counts and scratch volumes -
exactly the things that differ between the workstation, the cluster and a colleague's
laptop. Putting them in a shared ``.hydromate-prj`` would destroy its portability the
first time it was committed (plan §3, §6).

The schema is the plan's, verbatim, so the example in §6 parses unchanged::

    profiles:
      telemac_linux:
        solver: telemac
        environment: posix
        setup_script: /home/user/telemac/v9.1/configs/pysource.sh
        mpi_launcher: mpirun
        mpi_processes: 16
        working_root: /scratch/hydromate

      openfoam_windows:            # Foundation OpenFOAM: WSL is the route
        solver: openfoam
        environment: wsl
        distro: Ubuntu-22.04
        setup_script: /opt/openfoam9/etc/bashrc     # a path INSIDE the distro
        working_root: /scratch/hydromate            # inside the distro

``validate()`` is the pre-submission check plan §6 asks for, and it is
:meth:`SolverEnvironment.validate` unchanged - including its *provenance* warning about
sentinels that came from the ambient shell rather than the setup script. That warning is
the "works in my terminal, fails in the runner" trap, and a detached job is precisely
where it bites.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from hydromate.core.environment import EnvStatus, SolverEnvironment
from hydromate.core.errors import ConfigError
from hydromate.jobs import paths as jobpaths

__all__ = ["Profile", "load_profiles", "resolve", "save_profiles"]

#: TELEMAC's ``systel`` configuration name has no home in ``SolverEnvironment`` and does
#: not deserve one - it is a TELEMAC concept, and widening a solver-neutral class for it
#: would be the wrong trade. It travels as an environment override instead.
_CONFIG_NAME_VAR = "USETELCFG"


@dataclass
class Profile:
    """One way of reaching one solver."""

    name: str
    solver: str = "telemac"
    environment: str = "posix"             # posix | windows | wsl
    setup_script: Path | None = None
    shell: str | None = None
    distro: str | None = None
    config_name: str | None = None         # TELEMAC systel config
    mpi_launcher: str | None = None
    mpi_processes: int = 1
    working_root: Path | None = None
    launcher: str = "auto"                 # auto | systemd | posix | windows | wsl
    postprocess: list[str] = field(default_factory=list)
    overrides: dict[str, str] = field(default_factory=dict)

    # -- construction -------------------------------------------------------------
    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any], *, base: Path | None = None
                  ) -> "Profile":
        data = dict(data or {})
        known = {f.name for f in fields(cls)} - {"name"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(
                f"profile {name!r} has unknown key" + ("s" if len(unknown) > 1 else "")
                + ": " + ", ".join(unknown),
                subject=f"profiles.{name}.{unknown[0]}",
                remedy="Known keys: " + ", ".join(sorted(known)),
            )
        script = data.get("setup_script")
        root = data.get("working_root")
        return cls(
            name=name,
            solver=str(data.get("solver") or "telemac"),
            environment=str(data.get("environment") or "posix"),
            # A WSL path is a *Linux* path and must not be resolved against a Windows
            # base, so relative resolution is skipped for that kind entirely.
            setup_script=_path(script, base, absolute=data.get("environment") == "wsl"),
            shell=data.get("shell"),
            distro=data.get("distro"),
            config_name=data.get("config_name"),
            mpi_launcher=data.get("mpi_launcher"),
            mpi_processes=int(data.get("mpi_processes") or 1),
            working_root=_path(root, base, absolute=data.get("environment") == "wsl"),
            launcher=str(data.get("launcher") or "auto"),
            postprocess=list(data.get("postprocess") or []),
            overrides=dict(data.get("overrides") or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"solver": self.solver, "environment": self.environment}
        for key in ("setup_script", "shell", "distro", "config_name", "mpi_launcher",
                    "working_root"):
            value = getattr(self, key)
            if value is not None:
                out[key] = str(value)
        out["mpi_processes"] = self.mpi_processes
        if self.launcher != "auto":
            out["launcher"] = self.launcher
        if self.postprocess:
            out["postprocess"] = list(self.postprocess)
        if self.overrides:
            out["overrides"] = dict(self.overrides)
        return out

    # -- use ----------------------------------------------------------------------
    def solver_environment(self) -> SolverEnvironment:
        overrides = dict(self.overrides)
        if self.config_name:
            overrides.setdefault(_CONFIG_NAME_VAR, self.config_name)
        return SolverEnvironment(
            kind=self.environment,
            setup_script=self.setup_script,
            shell=self.shell,
            distro=self.distro,
            mpi_launcher=self.mpi_launcher,
            overrides=overrides,
        )

    def validate(self, *, timeout: float = 120.0) -> EnvStatus:
        """Can this profile actually reach its solver? Never raises."""
        return self.solver_environment().validate(self.solver, timeout=timeout)

    def describe(self) -> str:
        parts = [f"{self.name} ({self.solver})", self.solver_environment().describe()]
        if self.mpi_processes > 1:
            parts.append(f"{self.mpi_processes} processes")
        if self.working_root:
            parts.append(f"scratch {self.working_root}")
        return " / ".join(p for p in parts if p)


def _path(value: Any, base: Path | None, *, absolute: bool = False) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(str(value)).expanduser()
    if absolute or path.is_absolute() or base is None:
        return path
    return (base / path).resolve()


# ------------------------------------------------------------------------ file I/O


def load_profiles(path: str | os.PathLike | None = None) -> dict[str, Profile]:
    """Read ``profiles.yml``. A missing file is an empty mapping, not an error.

    A fresh install has no profiles and must still be able to run ``hydromate profiles``
    to be told so.
    """
    target = Path(path) if path else jobpaths.profiles_path()
    if not target.is_file():
        return {}
    import yaml
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{target} is not valid YAML", subject=str(target),
                          remedy="Fix the syntax, or delete the file to start over.",
                          cause=str(exc)) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} must be a mapping", subject=str(target))
    # Accept both the nested `profiles:` form the plan writes and a bare top-level
    # mapping, because both are obvious spellings and rejecting one helps nobody.
    block = raw.get("profiles", raw)
    if not isinstance(block, dict):
        raise ConfigError(f"{target}: 'profiles' must be a mapping",
                          subject="profiles")
    return {name: Profile.from_dict(name, data, base=target.parent)
            for name, data in block.items()}


def save_profiles(profiles: dict[str, Profile],
                  path: str | os.PathLike | None = None) -> Path:
    """Write ``profiles.yml``, keeping the ``profiles:`` wrapper."""
    import yaml
    target = Path(path) if path else jobpaths.profiles_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"profiles": {name: p.as_dict() for name, p in profiles.items()}}
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return target


def resolve(name: str | None, *, solver: str | None = None, cfg: Any = None,
            path: str | os.PathLike | None = None) -> Profile:
    """Find the profile to use, or synthesise one from the case config.

    The fallback matters: every existing case already carries ``telemac.pysource`` (or
    ``openfoam.bashrc``), so a user who has never written a ``profiles.yml`` can still
    submit a job. The profile system is an addition, not a new prerequisite.
    """
    profiles = load_profiles(path)
    if name:
        if name not in profiles:
            raise ConfigError(
                f"no solver profile named {name!r}",
                subject=f"profiles.{name}",
                remedy=("Known profiles: " + ", ".join(sorted(profiles)))
                       if profiles else f"Create one in {jobpaths.profiles_path()}.",
            )
        return profiles[name]
    if solver:
        matching = [p for p in profiles.values() if p.solver == solver]
        if len(matching) == 1:
            return matching[0]
        if len(matching) > 1:
            raise ConfigError(
                f"several profiles define {solver}; say which one",
                subject="profile",
                remedy="Use --profile with one of: "
                       + ", ".join(sorted(p.name for p in matching)),
            )
    if cfg is not None:
        return from_config(cfg, solver or "telemac")
    raise ConfigError(
        "no solver profile and no case configuration to derive one from",
        subject="profile",
        remedy=f"Create a profile in {jobpaths.profiles_path()} or pass a case config.",
    )


def from_config(cfg: Any, solver: str = "telemac") -> Profile:
    """Derive an implicit profile from a case config's own solver block."""
    if solver == "openfoam":
        block = getattr(cfg, "openfoam", None)
        env = getattr(block, "environment", None)
        script = getattr(block, "bashrc", None)
        processes = getattr(block, "n_processors", None)
    else:
        block = getattr(cfg, "telemac", None)
        env = getattr(block, "environment", None)
        script = getattr(block, "pysource", None)
        processes = getattr(block, "n_processors", None)
    kind = getattr(env, "kind", None) or "posix"
    return Profile(
        name=f"{solver}-from-config",
        solver=solver,
        environment=str(kind),
        setup_script=Path(getattr(env, "setup_script", None) or script)
        if (getattr(env, "setup_script", None) or script) else None,
        shell=getattr(env, "shell", None),
        distro=getattr(env, "distro", None),
        mpi_launcher=getattr(env, "mpi_launcher", None),
        mpi_processes=int(processes or 1),
        overrides=dict(getattr(env, "overrides", None) or {}),
    )
