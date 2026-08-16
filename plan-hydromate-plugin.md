# Task: Turn `hydromate` into a QGIS-integrated, persistent TELEMAC/OpenFOAM optimization system

The objective is to integrate hydromate as a backend into a QGIS plugin as a frontend and a robust external job execution architecture around `hydromate`.

`hydromate` repository is now public.

**Licence: no change needed.** `hydromate` is BSD-3-Clause, which is GPL-compatible - it is permissive and carries no advertising clause (unlike the old 4-clause BSD), so a GPLv2+ QGIS plugin may incorporate and link it. The plugin itself must be GPLv2+ because it links PyQGIS; `hydromate` stays BSD-3 and remains usable outside the plugin. Confirm with counsel if the project needs a formal opinion, but the expected action is none. Because no OpenFOAM or TELEMAC source is redistributed and this is pre-/post-processing only, licensing is independent of those.

The resulting system must support:

* QGIS **3.44+**
* QGIS **4.x**
* TELEMAC
* OpenFOAM
* long-running optimization jobs
* simulations that continue running after QGIS is closed
* persistent job state
* reconnecting to existing/running/completed jobs after QGIS restarts
* loading simulation and optimization results back into QGIS

**Linux and Windows are both supported targets**, with TELEMAC and OpenFOAM
installations on each. They are not the same path, because the two codes differ in what
they offer on Windows:

| platform | TELEMAC | OpenFOAM | how the environment is entered |
|---|---|---|---|
| Linux | supported, tested | supported, tested | `posix` |
| Windows | supported (native builds exist) | supported **through WSL** | `windows` or `wsl` |
| macOS | best-effort | best-effort | `posix` |

The asymmetry is not a preference. TELEMAC has real native Windows builds, configured
through a `.bat` rather than a sourced shell script. The **Foundation** OpenFOAM this
project targets (openfoam.org - the build hydromate's dictionary writer is verified
against) has **no official native Windows build**, so `openfoam` on Windows means
`environment: wsl`. Targeting ESI's Windows package instead would mean a second
dictionary writer: ESI renamed `momentumTransport` to `turbulenceProperties` and folded
`fvConstraints`/`fvModels` into `fvOptions`, and that variant could not be verified here,
so it would ship untested. One writer, one tested target.

Windows is therefore not "untested Linux", and must not be implemented as a fallback
that silently does the wrong thing:

* `source` is not a command and `bash` is not on PATH - see §7;
* neither `systemd-run` nor `start_new_session=True` exists - Windows needs its own
  launcher, and killing `mpirun` without a **Job Object** leaks orphan ranks, exactly the
  failure §9 forbids;
* MS-MPI ships `mpiexec`, not `mpirun` - the launcher command is a profile field, not a
  constant.

Plugin setups are defined in `<project-name.hydromate-prj` files that contain all user settings and simulation status.

# 1. Core architectural requirement

QGIS must not own the lifetime of solver processes. Do not run TELEMAC, OpenFOAM, MPI jobs, or the entire optimization lifecycle as a `QgsTask`, `QThread`, or subprocess whose lifetime depends on the QGIS process. The architecture must be:

```text
QGIS Plugin
    |
    | create job configuration / submit / monitor / cancel
    v
hydromate external runner
    |
    +--> TELEMAC or OpenFOAM
    |
    +--> preprocessing
    |
    +--> steady runs
    |
    +--> mesh-convergence
    |
    +--> BAL calibration + validation
    |
    +--> persistent job/result files
    |
    +--> postprocessing (visualization tools)
```

If QGIS is closed, crashes, or the plugin is unloaded:

```text
hydromate + TELEMAC/OpenFOAM shall keep running.
```

When QGIS is started again, the plugin must rediscover those jobs and display their current status.

The QGIS plugin is therefore a **frontend/job-control client**, while `hydromate` remains the external execution engine.

---

# 2. What already exists in hydromate (do not rebuild these)

This section answers, concretely, the "identify what exists" step. It is current as of
the solver-backend split (`hydromate.core` / `hydromate.solvers`).

| the plan needs | already in hydromate | notes |
|---|---|---|
| solver abstraction (§12) | `core/registry.py`: `BackendSpec` (metadata) + `SolverBackend` (protocol) | **Do not introduce a second `SolverAdapter`.** Extend this one. Backends are discovered through the `hydromate.solvers` entry-point group, so a new solver is a plug-in, not a fork. |
| capability / status model | `core/capabilities.py`: `Capability`, `Support`, `CaseStatus`, `MODEL=<SOLVER>_<ENABLED\|DISABLED>` markers | Three axes per capability: implemented / configured / built-run. **The plugin's tabs should be generated from this**, not hardcoded - see §17. |
| environment initialization (§7) | **`core/environment.py`: `SolverEnvironment`** (`posix` \| `windows` \| `wsl`), plus `core/env.py`'s `ShellRuntime` which delegates to it | **Done (phase 4a).** Captures the environment into a dict (`capture()`), validates a solver install by its sentinel variables (`validate()`), and translates paths for WSL. Argument-safe; no `shell=True` on user text. |
| configuration system | `config.py`: `load_config()` / **`dump_config()`**, `Config.declared_blocks`; **`core/schema.py`** classifies every setting as shared-intent vs solver detail | **Done (phase 4b/4c).** Round-trip is verified over all ten repo configs. `core/schema.classify_setting()` is what a form should page the fields by. `hydromate migrate` rewrites a config in the current schema. NOTE dumping **loses comments** by design. |
| MPI handling | `TelemacRuntime.run_solver(ncsize=...)`; OpenFOAM `mpirun -np N interFoam -parallel` | Both already build the command; neither currently owns the process tree. |
| working directories | per-phase dirs (`preprocessing/ simulation/ postprocessing/ calibration-validation/ openfoam/`) | **Not job-oriented.** §4 needs `<job_root>/<job_id>/`; that is a real change, see the re-planned phases. |
| logging | `logsetup.py`: per-phase compound logfile, `log_step()` timing | Missing: rotation. A multi-day MPI run will otherwise fill the scratch volume. |
| structured failures | **`core/errors.py`**: `HydromateError` + 5 categories, each with a stable `code`, optional `subject` (the config key at fault) and `remedy`; `ErrorRecord.from_exception` | **Done (phase 4d).** §5's `status.json` should store `ErrorRecord.as_dict()`; the plugin should show `remedy` and highlight `subject`. |
| restart / checkpoint | 2D hotstart (`r2d.slf`), `hotstart2d.cas`, BAL `--resume`, convergence-study resume via `level.json` | Per-workflow, not per-job. |
| geodata ingest | `core/geodata.py`: one cached `Dataset`, **accepts in-memory GeoDataFrames** | The plugin can hand over open QGIS layers without writing temp files. |
| structures (dams/walls) | `core/structures.py`, from ordinary vector layers | No STL needed anywhere - see the repo docs. |
| CLI | `hydromate <config>`, `status`, `openfoam`, `clip`, `rating`, `targets`, **`migrate`** | Missing the job verbs: `submit`, `execute`, `cancel`, `logs`, `list`. |

**Missing entirely, and therefore the real work**: job identity, the job registry,
detached launch, process-tree cancellation, `status.json`, and the job CLI verbs.

Rules that stay:

* prefer adapting existing code over adding parallel implementations;
* keep the calibration/optimization core independent from QGIS;
* there must be no `qgis`, `PyQt`, or `qgis.PyQt` import in the core/runner package;
* **the plugin must never `import hydromate`** - QGIS's Python is not the solver's
  Python. The plugin talks to hydromate over the CLI and over files, only.

---

# 2b. Status, and what a next session should pick up

*Updated 2026-08-15, after phases 5-6 and the plugin. Read this before §3.*

## Done and on `main`

| phase | what | where |
|---|---|---|
| 1 | capabilities, solver registry, `MODEL=` markers, `hydromate status` | `core/capabilities.py`, `core/registry.py`, `solvers/*/spec.py` |
| 2 | solver-agnostic input layer, cached geodata `Dataset` | `core/geodata.py`, `core/raster.py`, `core/boundaries.py` |
| 3 | backends split out; 16 `sys.modules` shims keep every old import path | `solvers/telemac/`, `solvers/openfoam/` |
| 4a | `SolverEnvironment` (`posix` \| `windows` \| `wsl`) | `core/environment.py` |
| 4b | shared-intent classification + legacy-key table | `core/schema.py` |
| 4c | `dump_config`, `hydromate migrate` | `config.py`, `cli.py` |
| 4d | typed errors with stable codes | `core/errors.py` |
| **5** | **the job model**: ids, paths, states/kinds, atomic store, lock + staleness, SQLite index, reaper, profiles, events, rotation | **`jobs/`** |
| **6** | **the runner**: executor, `submit/execute/status/cancel/logs/list/profiles`, four detached launchers, `--json` envelope, per-category exit codes | **`jobs/executor.py`, `jobs/launchers/`, `jobcli.py`** |
| **6b** | **solver backends** finally implementing `SolverBackend` (`spec.load()` was dead code) | **`solvers/*/backend.py`** |
| **7-11** | **the QGIS plugin**: compat layer, runner client, `.hydromate-prj`, capability-generated tabs, job dashboard, result loading, Processing provider, A3 layout | **`qgis_plugin/hydromate/`** |

596 tests, `ruff` clean, the import-weight contract intact.

## Remaining

Nothing in this plan is outstanding. What is worth doing next, on its own merits:

1. **Run the acceptance tests against real solvers.** A/C/D/F pass against the fake
   solvers and a real detached run; A and E want a real TELEMAC run and a real QGIS
   session, and B wants OpenFOAM.
2. **Windows and WSL.** Both launchers are implemented and unit-tested (argv, Job Object
   flags, stale-PID recovery) but have **never been executed** against a real install.
   They are documented as such.
3. **QGIS 4.x verification.** The plugin is written for it (`qgisMaximumVersion=4.99`, no
   `.qrc`, enums by name) and was verified against PyQGIS 3.22 here; it has not been run
   on a 4.x build.
4. The deferred split of `convergence`/`calibration` into core + adapters - leftover tidy-up
   from phase 3, blocking nothing.

## Decisions taken that the plan no longer describes correctly

* **Phase 4b was deliberately re-scoped.** The plan folded `openfoam.cell_size` into
  `mesh.channel_size`. Do **not** do this: `openfoam.cell_size_factor` now expresses
  the OpenFOAM lattice *relative to* the 2D channel size, which is the shared-intent
  link the fold was after, and it does so without pretending a per-zone anisotropic
  target edge length and a uniform Cartesian spacing are the same quantity. The
  remaining moves (Telemac2d keywords out of `hydrodynamics:` into `telemac.numerics`)
  are pure renaming: no behaviour change, every config touched, the artifact regression
  baseline invalidated. `core/schema.LEGACY_KEYS` is the mechanism for doing it later
  without breaking a single config. **The classification exists; the fields have not
  moved.**
* `project.results_dir` maps to **`calibration_dir`**, not `model_dir`.
* `dump_config` goes through the dataclass, so it **loses comments**. A form-based
  editor must therefore not be the only copy of a case's documentation - the annotated
  original lives in `cases/case-template/case-config.yml`.
* **§5's job tree does not fit every kind.** Steady/3D/unsteady/calibration run inside an
  existing `model_dir` and hotstart from an `r2d.slf` there; giving each its own `input/`
  would mean copying it, which §26 forbids. Hence `workspace.mode` (`job` | `case` |
  `link`), defaulted per kind - build kinds own their directory, run kinds do not.
* **§10's flat `progress` example is wrong**; the same section's prose is right. Progress
  is typed per kind.
* **§8's Windows Job Object must be named.** An unnamed one is reachable only through the
  creating handle, which dies with the submitter - so `cancel` from a fresh process could
  never open it, which is the exact failure the Job Object was introduced to prevent.
* **§13's "MPI through the profile's launcher" does not apply to TELEMAC.** Its own
  launcher spawns `mpirun` from the systel config, so prepending one would start the run
  twice. It applies to OpenFOAM, which now honours it instead of hard-coding `mpirun`.
* **§4's `status JOB_ID` collides with the existing case status.** Resolved by a rule
  rather than a rename: an existing path means the case, a job-id-shaped argument means
  the job, and `case-status` is the explicit spelling. Both keep working.
* **§24's `error` shape** is `ErrorRecord`'s (`code`/`message`/`subject`/`remedy`/
  `details`); `component` and `return_code` travel in `details` rather than widening the
  dataclass.

## Traps a next session will otherwise re-discover

* **The anisotropic TELEMAC mesh is not reproducible run to run** (BAMG retries at a
  coarser background metric). Regression-test on mesh *statistics* within ~1%, never
  byte-compare `geometry.slf`. The OpenFOAM mesh *is* bit-reproducible.
* **`import hydromate` must stay light.** `tests/test_capabilities.py` asserts from a
  subprocess that a capability listing pulls in no numpy/gmsh/geopandas. A QGIS plugin
  process depends on this; an eager import in a `spec.py` breaks it silently.
* **The two backends are siblings.** `solvers/openfoam/` may not reference
  `solvers/telemac/` - enforced by reading the source. Orchestration that drives both
  goes at the top level (`prerun.py` is the worked example).
* **TELEMAC-3D writes NPLAN in `IPARAM(7)`, not the dims record**, which stays at 1.
  Anything reading a 3D SELAFIN must not trust `dims[3]`.
* **A shallow reach cannot carry a 3D run.** Sigma planes are sized from depth against
  cell size and capped at 4x taller than wide, so isar-2025 (0.26 m deep, 0.62 m cells)
  gets 2 planes - one layer. `prerun.MIN_USEFUL_LEVELS` refuses before the solver runs.
* **TELEMAC-3D diverges from a 2D continuation on a braided bed** (dry columns onto the
  sigma mesh: `GRACJG ... NaN` on the first solve), and **Spalart-Allmaras diverges in
  3D** on a wetting/drying bed without a source-term patch. `prerun._make_3d_robust`
  works around both for the *seed* run; **`add3d.py` still has both problems** and will
  hit them on such a reach. That is the clearest outstanding TELEMAC-side bug.
* **A diverged solver can write a file of NaN rather than failing.** Validate a result
  before consuming it (`prerun._is_finite`).
* **A zombie process reads as alive.** `os.kill(pid, 0)` succeeds against one, because the
  table entry survives until the parent reaps it - so `cancel` reported failure against a
  job that had already stopped. `jobs.procs.pid_alive` treats state `Z` as dead.
* **A throttled status sink will skip the final write.** Fields assigned directly on the
  status object leave the sink believing it is clean, so a failed job kept its real error
  in `runner.log` while `status.json` still said RUNNING - and the reaper then relabelled
  it "abandoned", losing the actual cause. The terminal write bypasses the sink.
* **`QgsTask` does not take a Python reference.** A task whose only reference was a local
  is garbage collected before it runs, and the symptom is not a crash but a callback that
  never fires: no tabs, an empty dashboard, no error anywhere.
* On isar the OpenFOAM **rough wall function is inadmissible on ~100% of wetted bed
  faces** (ks is a large fraction of the depth). OpenFOAM clamps and carries on, so the
  run completes with unphysical near-bed velocity. Read such a result as bulk flow.

---

# 3. Configuration artifacts: what each one is, and who may write it

The plan names four artifacts - `case-config.yml`, `<project>.hydromate-prj`,
`job.json`, and solver profiles - with overlapping content. Left ambiguous, they will
fight over the same truth and be written concurrently by QGIS and the runner. The
governing rule is therefore:

> **Exactly one writer per file.**

| artifact | contains | written by | lifetime |
|---|---|---|---|
| `case-config.yml` | the **model**: geodata, boundaries, mesh intent, roughness, structures, ground truth, calibration ranges | user, or the plugin's form (via `dump_config`) | versioned with the case, git-friendly |
| `<project>.hydromate-prj` | a **thin project pointer**: which case configs belong to this project, which solver profile to use, where the job root is, plugin view state | the plugin only | user-owned, git-friendly |
| `~/.config/hydromate/profiles.yml` | **machine-local** solver profiles: setup scripts, MPI counts, scratch roots | user / plugin settings dialog | never in the project file - these are machine-specific paths and would destroy portability |
| `<job>/job.json` | the **immutable submission record**: what was asked for, resolved and frozen at submit time | the CLI at submit, then never again | immutable |
| `<job>/status.json` | the **mutable state**: state, progress, timing, error | **the runner only** | replaced atomically |

**`.hydromate-prj` must not contain simulation status.** The original draft has it hold
"all user settings and simulation status"; status belongs to the runner, which may be
writing while QGIS is closed. A project file that also carries status guarantees a
lost-update race the first time two QGIS windows are open.

`job.json` freezes the *resolved* configuration, not a reference to it. If the user
edits `case-config.yml` after submitting, the running job must not change underneath
itself, and a completed job must remain reproducible from its own directory.

---

# 4. External `hydromate` runner

Create or adapt a standalone CLI interface so jobs can be executed without QGIS.

The desired interface should conceptually support commands such as:

```bash
hydromate submit job.json
hydromate execute job.json
hydromate status JOB_ID
hydromate cancel JOB_ID
hydromate logs JOB_ID
hydromate list
```

Adapt these names to the existing CLI architecture if necessary, but preserve the functionality.

Required behavior:

## `execute`

Runs a job synchronously in the current shell:

```bash
hydromate execute job.json
```

Useful for debugging.

## `submit`

Creates/starts an externally managed long-running job and returns immediately:

```bash
hydromate submit job.json
```

It should return at minimum:

```text
JOB_ID
```

The actual optimization/simulation must continue independently of the shell that invoked `submit`, and independently of QGIS.

## `status`

Returns machine-readable job status.

Prefer JSON output.

## `cancel`

Must terminate the whole solver/optimization process tree safely, including MPI processes where applicable.

## `logs`

Returns or locates the relevant log file.

## `list`

Lists known jobs from the persistent job registry.

---

# 5. Persistent job model

Each submitted job must have a unique ID.

**Use a readable, sortable ID rather than a bare UUID**: the ID is a directory name and
a dashboard column, and `f835ac7d-...` is neither greppable nor meaningful six months
later. The draft's own dashboard example truncates it to `f835ac7`, which is the
problem showing through.

```text
<date>-<case-slug>-<6 hex>      e.g.  2026-08-13-isar-steady-a3f19c
```

Sorts chronologically, says what it is, and the random tail keeps it collision-safe.

Revise the `hydromate` directory structure robustly so that each case approximately write like:

```text
<job_root>/
└── <job_id>/
    ├── job.json
    ├── status.json
    ├── simulation.log
    ├── hydrobayescal/
    │   └── ...
    ├── input/
    │   ├── preprocessing/
    │   ├── simulation-cases/
    │   └── ...
    └── results/
        ├── mesh-convergence/
        │   └── ...
        ├── results.json
        └── qgis/
```

Do not rely solely on in-memory Python state.

The system must recover job information after:

* QGIS restart;
* plugin reload;
* runner restart where possible;
* terminal closure.

**The per-job directory is authoritative; any registry is a derived index.**

A lightweight SQLite index is useful for listing hundreds of jobs quickly:

```text
~/.local/share/hydromate/jobs.sqlite
```

but it must be reconstructible by scanning the job root, and `hydromate list --rebuild`
must do exactly that. Otherwise the first crash mid-write produces a registry that
disagrees with the disk, and there is no rule for which one wins. Making the scan a
*tested* path also covers the case where a user moves or restores a job directory.

## Concurrency

Two QGIS windows, or a CLI and QGIS, can act at once. Therefore:

* `status.json` is written **only** by the runner, and only by atomic replace;
* submission takes a lock file in the job directory holding PID and start time; a lock
  whose PID is gone is stale and may be broken, which must be tested, because the
  alternative is a job that can never be resubmitted after a crash;
* readers never lock - they read the last complete `status.json`.

## Schema versioning

`job.json` carries `schema_version`. On read:

* **older** than the runner: migrate in memory, and rewrite only on the next write;
* **equal**: proceed;
* **newer**: refuse with a clear message naming both versions.

Never silently ignore an unknown field: that is how a job that *looks* submitted
quietly runs something else.

---

# 6. Job and solver configuration schema

Define a versioned job schema in line with `case-config.yml`.

Example concept:

```json
{
  "schema_version": 1,
  "job_id": "...",
  "solver": "telemac",
  "solver_profile": "telemac_local",

  "model": {
    "steering_file": "/path/to/model.cas"
  },

  "resources": {
    "mpi_processes": 16
  },

  "calibration": {
    "BAL-options": "...",
    "parameters": {}
  }
}
```

Do not assume this exact schema if the existing `hydromate` configuration model is already suitable.

Prefer extending or wrapping the existing configuration system.

The schema must be explicitly versioned to support future migrations.


Implement persistent solver profiles also in line with `case-config.yml`. Conceptually:

```yaml
profiles:

  telemac_linux:
    solver: telemac
    environment: posix                 # bash -lc 'source <script>; cmd'
    setup_script: /home/user/telemac/v9.1/configs/pysource.sh
    config_name: ubuntu
    mpi_launcher: mpirun
    mpi_processes: 16
    working_root: /scratch/hydromate

  telemac_windows:
    solver: telemac
    environment: windows               # cmd /c 'call <script>.bat && cmd'
    setup_script: C:\telemac\v9.1\configs\pysource.bat
    mpi_launcher: mpiexec              # MS-MPI
    mpi_processes: 8
    working_root: D:\scratch\hydromate

  openfoam_linux:
    solver: openfoam
    environment: posix
    setup_script: /opt/openfoam9/etc/bashrc
    mpi_launcher: mpirun
    mpi_processes: 32
    working_root: /scratch/hydromate
    postprocess:
      - foamToVTK

  openfoam_windows:                    # Foundation OpenFOAM: WSL is the route
    solver: openfoam
    environment: wsl
    distro: Ubuntu-22.04
    setup_script: /opt/openfoam9/etc/bashrc      # a path INSIDE the distro
    mpi_launcher: mpirun
    mpi_processes: 16
    working_root: /scratch/hydromate             # inside the distro
```

`environment` is the discriminator; `shell` overrides the interpreter where a
distribution ships its own (blueCFD-Core and the ESI Windows package both bundle an
MSYS bash, and entering their environment means using *that* bash, not the system one).
`mpi_launcher` exists because `mpirun` is not universal.

The exact schema can be improved.

Requirements:

* support TELEMAC environment initialization;
* support OpenFOAM environment initialization;
* support arbitrary installation paths;
* support configurable shell, normally `/bin/bash`;
* validate solver functionality (environment state) before submission;
* do not store environment state inside QGIS;
* the external runner must load the required environment itself.

---

# 7. Environment initialization

TELEMAC/OpenFOAM must be invoked from the correct shell environment.

For example:

```bash
source /path/to/telemac/environment.sh
```

or:

```bash
source /opt/openfoam/etc/bashrc
```

**Capture the environment into a dictionary, then launch the solver directly in it.**
This was one of three options in the original draft; it should be *the* mechanism,
because it is the only one that is both portable and compatible with detached execution:

* it works the same way across all three kinds - `source <script>; env -0` under bash,
  `call <script>.bat && set` under cmd - so there is one design rather than three;
* it lets the launcher (§8) set the environment **directly on the detached process**, so
  no intermediate shell sits inside the job's process tree. That is what makes tree-kill
  in §9 clean: fewer layers between the unit and the MPI ranks;
* it doubles as the validation §5 asks for. A profile is correct if capturing it yields
  the variables the solver needs (`HOMETEL` for TELEMAC, `WM_PROJECT_VERSION` for
  OpenFOAM) - checkable at configuration time rather than at submit time.

The wrapper form is still needed for **WSL**, where a captured Linux environment cannot
launch a Windows process, so commands must go through `wsl.exe` regardless.

One implementation owns all of this (`hydromate.core.environment.SolverEnvironment`),
including **path translation**, which belongs in exactly one place rather than at every
call site:

```text
posix     bash -lc 'source <script>; <cmd>'
windows   cmd /c 'call <script>.bat && <cmd>'      or a bundled MSYS bash
wsl       wsl.exe -d <distro> -- bash -lc '...'    + wslpath translation
```

Avoid unsafe shell command concatenation.

Do not take arbitrary GUI text and directly pass it into:

```python
shell=True
```

Validate paths and arguments.

---

# 8. Detached process execution

Linux and Windows architectures should support robust detached execution.

Prefer the following hierarchy:

## Preferred Linux backend: systemd user services

If available, support launching jobs through transient user services, conceptually:

```bash
systemd-run --user ...
```

Each hydromate job should have a deterministic unit name such as:

```text
hydromate-<job-id>.service
```

Benefits required:

* solver survives QGIS exit;
* status can be queried;
* entire process tree can be terminated;
* logs can be retrieved;
* MPI child processes belong to the controlled unit.

Abstract this behind a launcher interface.

For example:

```python
class JobLauncher:
    def submit(...)
    def status(...)
    def cancel(...)
    def logs(...)
```

Implement:

```python
class SystemdUserLauncher(JobLauncher):
    ...
```

## Windows backend (native)

Create the process with `CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS` and assign it to
a **Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` cleared, so the tree survives
the submitting process but can still be terminated as a unit. Without the Job Object
there is no reliable tree-kill on Windows and cancellation leaks MPI ranks.

```python
class WindowsJobObjectLauncher(JobLauncher):
    ...
```

## WSL backend

For `environment: wsl`, the job must be detached **inside the distro** - `setsid` /
`nohup` under WSL - and its Linux PID recorded. The Windows side then monitors and
cancels by running commands through `wsl.exe`, never by acting on the `wsl.exe` process
itself.

```python
class WslLauncher(JobLauncher):
    ...
```

Two caveats to document rather than pretend away:

* **killing `wsl.exe` does not kill the Linux-side tree.** It is a client, not the
  parent. Cancellation must therefore run inside the distro and act on the Linux process
  group, exactly as the POSIX launcher does.
* **the distro's lifetime is not ours to control.** `wsl --shutdown`, a Windows restart,
  or WSL2 idling the VM out will terminate running jobs regardless of what hydromate
  does. Jobs must recover to a sane state (`FAILED`, not a stuck `RUNNING`) when their
  recorded PID is gone - which is the same stale-process detection the POSIX fallback
  needs, so it is one implementation, not two.

## Fallback backend

Provide a POSIX fallback for systems without user systemd support.

Use detached process semantics such as:

```python
subprocess.Popen(
    ...,
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    ...
)
```

The fallback must still persist sufficient process information for monitoring.

Do not make terminal emulator processes such as these part of the execution architecture:

```text
gnome-terminal
konsole
xterm
```

An external terminal may optionally be opened for the user to inspect logs, but it must not own the solver.

---

# 9. Process-tree-aware cancellation

Cancellation must be robust.

A solver hierarchy may look like:

```text
hydromate
  |
  +-- mpirun
       |
       +-- TELEMAC rank 0
       +-- TELEMAC rank 1
       +-- TELEMAC rank N
```

or equivalent OpenFOAM processes.

Never implement cancellation as simply:

```python
os.kill(pid)
```

without considering descendants.

With systemd, cancel the entire unit.

For the POSIX fallback, use process groups/session IDs appropriately.

Update the persistent state consistently:

```text
CANCEL_REQUESTED
CANCELLED
```

---

# 10. Job state machine

Create a clearly defined state model.

At minimum support:

```text
QUEUED
STARTING
RUNNING
POSTPROCESSING
COMPLETED
FAILED
CANCEL_REQUESTED
CANCELLED
```

Potentially include more detailed solver-specific states if useful.

`status.json` should contain useful information such as:

```json
{
  "schema_version": 1,
  "job_id": "2026-08-13-isar-steady-a3f19c",
  "state": "RUNNING",
  "solver": "telemac",
  "kind": "calibration",
  "progress": {
    "iteration": 38,
    "max_iterations": 50,
    "best_objective": 0.0472
  },
  "started": "...",
  "updated": "..."
}
```

**Progress is nested and typed by job kind**, not flattened into the top level. Most
jobs are not optimizations: a steady run reports simulated time against duration and a
flux imbalance, a mesh-convergence study reports levels completed, a BAL run reports
iterations and best objective. Putting `iteration` and `best_objective` at the top
level forces every other job kind to leave them null and invites the dashboard to
display meaningless zeros.

Use atomic file writes so QGIS never reads a partially written JSON file.

For example:

```text
write temporary file
fsync if appropriate
atomic rename
```

---

# 11. Calibration and its progress

QGIS plugin UI should have a dedicated tab to design relevant gpkg files with ground truth data.

## Calibration target definition

Ground truth point gpkg files may contain sediment and hydraulic measurements of:

* grain size distribution (surface with thickness)
* grain size distribution (subsurface at depth/with thickness)
* ADV velocity means + errors + TKE proxy method
* Water depth (ADV)

Ground truth line gpkg files may contain sediment and hydraulic measurements of:

* discharge across line (water)
* discharge across line (sediment)
* tracer concentration across line

Ground truth **raster** files (GeoTIFF - not GeoPackage; PTV/PIV maps and DEMs of Difference are rasters) may contain:

* velocity magnitude/error maps from PTV/PIV
* topographic change (delta z_bed between two DEMs)

## Calibration parameter definition

Calibration parameters are model (Telemac, OpenFOAM) dependent.

Typical hydraulic calibration parameters:

* wall roughness (ks [default], strickler, manning's n)
* transversal currents
* turbulence parametrization (e.g. background eddy viscosity)
* CFL condition (minimum; primarily for wall-clock runtime minimization)
* transversal/secondary currents (2d only)
* minimum water depth for wet/dry thresholds

Typical sedimentary calibration parameters:

* mobile/active layer thickness
* numbers of substrate layers
* critical Shields parameter (i.e. dimensionless bed shear stress)
* transport mode (suspended load, bedload, washload)

Users should be able to set these parameters depending on the chosen solver:

* Telemac2d
* Telemac3d
* Telemac modules: Gaia, WAQTEL
* OpenFOAM (pimple, interFOAM, simpleFOAM)

## Calibration surveillance

Expose sufficient state for QGIS to monitor long-running optimization.

Where supported by the existing optimization implementation HydroBayesCal standards.

Do not require QGIS to parse human-readable console logs to obtain state.

---

# 12. Solver abstraction

**It already exists - extend it, do not add a second one.** `hydromate.core.registry`
splits the abstraction in two, deliberately:

```python
BackendSpec      # metadata only: name, capability support matrix, artifact predicates.
                 # Import-light by contract, so `hydromate status` and the plugin can
                 # ask "what can this do?" without importing gmsh, numpy or a solver.

SolverBackend    # the implementation protocol: check_environment / build / run
```

Backends register through the `hydromate.solvers` entry-point group, so a third solver
is `pip install hydromate-basement`, not a fork. `hydromate/solvers/telemac/spec.py` and
`hydromate/solvers/openfoam/spec.py` are the worked examples.

What the job system needs to **add** to `SolverBackend`, per solver:

```python
    def postprocess(self, cfg, job): ...          # OpenFOAM: reconstructPar, foamToVTK
    def extract_objective(self, cfg, job): ...    # for BAL
    def export_qgis_results(self, cfg, job): ...  # writes results/qgis/, no PyQGIS
```

Two rules the existing design already enforces and that must survive: nothing in
`hydromate.core` imports a solver, and one backend never reaches into another. Both are
checked by tests that read the source, so they hold for function-local imports too.

The optimization code should not need solver-specific branching everywhere.

Avoid designs such as:

```python
if solver == "telemac":
    ...
elif solver == "openfoam":
    ...
```

throughout the optimization core.

Solver-specific behavior should live behind adapters/interfaces.

---

# 13. Solver support

Solver support according to existing hydromate setups

## TELEMAC support

Reuse existing TELEMAC support in `hydromate`.

The runner must support:

* TELEMAC environment loading;
* steering file handling;
* parameter modification;
* MPI execution if configured - through the profile's `mpi_launcher`, since MS-MPI on
  Windows provides `mpiexec` rather than `mpirun`;
* solver output collection;
* failure detection;
* objective extraction;
* persistent logging.

Do not assume the user has TELEMAC installed at a fixed path.

The configuration/profile must determine the setup.

## OpenFOAM support

Reuse existing OpenFOAM functionality. Target the **Foundation** build (openfoam.org),
which is what the dictionary writer is verified against; on Windows that means
`environment: wsl`, because Foundation ships no native Windows build. Do not add an ESI
dictionary variant unless someone can verify it against a real ESI install.

Support:

* case preparation;
* environment loading;
* parameter changes;
* parallel execution;
* decomposition/reconstruction if the existing workflow requires it;
* result collection;
* postprocessing;
* objective extraction.

The postprocessing pipeline must support an optional VTK conversion step.

For example:

```text
OpenFOAM solver
    |
    +--> reconstructPar        if needed
    |
    +--> foamToVTK             if configured
    |
    +--> further extraction
```

Treat postprocessing as part of the external runner lifecycle.

QGIS must not perform heavy OpenFOAM postprocessing.

---

# 14. Separate raw solver output from QGIS-oriented results

Use separate directories.

Conceptually:

```text
results/qgis/
```

Examples of QGIS-oriented output include:

```text
*.gpkg
*.tif
*.csv
*.json
*.vtk
*.vtu
*.pvd
*.slf
```

depending on solver and result type.

The external runner may prepare QGIS-friendly files.

However:

**the external runner must not import PyQGIS.**

It creates files.

The QGIS plugin loads/styles those files.

---

# 15. QGIS plugin

Create the plugin as a separate package/module around `hydromate`.

Use a structure approximately like:

```text
qgis_plugin/
└── hydromate/
    ├── __init__.py
    ├── metadata.txt
    ├── LICENSE
    ├── README.md
    ├── plugin.py
    ├── gui/
    │   ├── dock.py
    │   ├── jobs_widget.py
    │   └── settings_dialog.py
    ├── core/
    │   ├── runner_client.py
    │   ├── job_registry.py
    │   └── result_loader.py
    ├── processing/
    │   ├── provider.py
    │   └── algorithms/
    ├── resources/
    └── tests/
```

Adapt naming as needed to the existing repository.

Do not unnecessarily mix plugin code with solver code.

---

# 16. QGIS 3.44+ AND QGIS 4.x compatibility

This is a strict requirement.

One codebase should work with:

```text
QGIS >= 3.44
QGIS 4.x
```

Use:

```python
from qgis.PyQt...
```

rather than directly importing:

```python
PyQt5
PyQt6
```

unless there is a documented compatibility reason.

Do not assume Qt5-specific APIs.

Avoid deprecated QGIS APIs where a QGIS 3.44-compatible replacement exists.

Where QGIS 3.44 and QGIS 4 APIs genuinely differ:

* create small compatibility helpers;
* centralize version-specific behavior;
* do not scatter version checks throughout the codebase.

For example:

```text
compat.py
```

with tightly scoped helpers.

Use QGIS version information only where required.

The plugin metadata must declare an appropriate minimum QGIS version.

Target:

```text
qgisMinimumVersion=3.44
```

Do not artificially restrict QGIS 4.x compatibility.

Audit all:

* Qt enum changes;
* signal/slot behavior;
* QAction/QDialog APIs;
* QVariant-related assumptions;
* deprecated PyQGIS APIs;
* plugin metadata;
* Processing APIs;
* mesh APIs;
* layer-loading APIs.

---

# 17. Plugin UI

Use a QGIS dock widget as the primary interface.

Suggested tabs/sections:

```text
hydromate
|
+-- Setup
|
+-- Preprocessing (meshing)
|
+-- Steady jobs
|
+-- Mesh convergence
|
+-- BAL
|
+-- Unsteady jobs
```

**Generate the tab set from hydromate's capability matrix rather than hardcoding it.**
`hydromate status <config> --full` already reports, per solver, every capability on
three axes - implemented / configured / built-run - using the shared vocabulary
(`steady2d`, `unsteady2d`, `steady3d`, `free_surface_3d`, `morphodynamics`,
`mesh_convergence`, `vertical_convergence`, `calibration`, `gain_lose`). Those *are*
the tabs.

Driving the UI from it means:

* a tab is **hidden** where the capability is `n/a` for the chosen solver (OpenFOAM has
  no depth-averaged mode, so no "Steady 2D" tab), and **shown but disabled with a
  reason** where it is `not_implemented` - a distinction users otherwise have to guess;
* actions enable from `configured` / `built` / `run` rather than from the plugin's own
  idea of what a case has done;
* **adding a solver or a capability to hydromate surfaces in the plugin with no plugin
  change**, which is the whole point of the registry.

The plugin obtains this as JSON from the CLI. It must never import hydromate to ask.

The plugin Setup should allow configuring:

## Setup: Solver

```text
TELEMAC
OpenFOAM
```

## Setup: Solver profile

Example:

```text
telemac_local
openfoam_local
```

The Setup first (default) tab enables users to define the solver with options first:

* Telemac2d
* Telemac3d
* Telemac modules: Gaia, WAQTEL
* OpenFOAM (pimple, interFOAM, simpleFOAM)

Users must define functional environment -- other tabs and options remain inactive until solver definition is correct.

Alternatively, users can select `<project-name.hydromate-prj` files that contain all user settings.


## Preprocessing, Steady jobs, Mesh convergence, BAL, Unsteady jobs

Adapt from hydromate workflow.

Preprocessing:

* guides users through required geodata (gpkg) creation
* provides guidance for additional optional inputs like the percolation zone (gain/loose conditions)


## Job actions

At minimum:

```text
Submit
Refresh
Cancel
View logs
Open job directory
Load results
```

## Results are integrated in each tab

### Steady and unsteady Jobs

Visualize results mesh in QGIS with predefined settings:

* water depth h:
  - white > bright blue > dark blue color map
  - white=0.000 meters (transparent)
  - all colors below user-defined minimum water depth are transparent
  - max. h is maximum water depth on mesh
  - users can override these options
* velocity (U):
  - arrows with inverted 'plasma' color map (0=bright yellow; max=dark purple)
  - max. U is maximum velocity from mesh; warn if higher than 5 m/s
  - default absolute max. is 5 m/s (reset to 5 m/s is max. U is higher)
  - users can override these options
  - U magnitude and direction is the Eucledian of all available component (u, v, w)
* All other parameters: Users are pointed to Layer Symbology

For unsteady jobs integrate an "Export movie" button that enables a dialogue to export consecutive mesh timesteps:

* with user-defined parameters (currently visualized mesh variables)
* frameduration
* output format is the most storage-efficient format (you choose)

An "Export rasters" option just points to the default QGIS functions that export rasters from a mesh.

### Mesh convergence

* an `Open Report` button should open the Excel file.
* a `Add meshes to layers` checkbox adds the resulting meshes into a group called "Mesh convergence" in the QGIS Layer panel.

### BAL

BAL plots should be saved in a folder that opens upon click on a "Show results folder" button in the BAL tab.


### Global QGIS print layout

Add one default A3 print layout that contains:

* simulation ROI
* one "North" arrow
* one bold "Q" (flow direction) arrow following bulk velocity gradient
* one scalebar (one black, one white section)
* empty legend with hint that users should enable themselves
* all text is 16 pt Arial

---

# 18. Job dashboard (both steady and unsteady tabs)

Implement a jobs table.

Example:

```text
Job ID      Solver      State          Iteration      Best objective
--------------------------------------------------------------------
f835ac7     TELEMAC     RUNNING        38             0.0472
9ab19d2     OpenFOAM    COMPLETED      124            0.0198
5b31cd9     TELEMAC     FAILED         3              -
```

The plugin must regularly refresh job state while QGIS is open.

A `QTimer` polling lightweight status files or the runner CLI/API is acceptable.

Do not use busy loops.

Do not block the GUI.

**Polling policy**, because a naive timer over a scratch volume with hundreds of jobs
becomes its own performance problem:

* poll **only rows that are visible and not terminal** - a `COMPLETED` job never
  changes again;
* **stat before parse**: compare `status.json` mtime and skip the JSON read when
  unchanged. This is the single biggest saving, since most polls find nothing new;
* **adaptive interval**: ~2 s while a job is RUNNING and its tab is visible, ~30 s when
  the dock is hidden, stop entirely when QGIS loses focus and nothing is running;
* never poll the solver's own output files - only `status.json`.

---

# 19. Rediscover jobs when a `.hydromate-prj` is loaded

On plugin initialization:

1. find the configured hydromate job registry/root;
2. load existing jobs;
3. determine their current states;
4. populate the jobs dashboard.

This workflow must account for:

```text
1. User submits jobs in QGIS.

2. User closes QGIS.

3. TELEMAC/OpenFOAM continues running.

4. User opens QGIS several hours or days later.

5. User opens plugin and selects the `.hydromate-prj` file: Plugin discovers the existing job.

6. Plugin displays:

   COMPLETED

   or:

   RUNNING

7. User loads the results.
```

---

# 20. QGIS result loading

Implement a result loader abstraction.

For example:

```python
class ResultLoader:
    def discover_results(job):
        ...

    def load_result(result, project):
        ...
```

Support common GIS formats where available.

For TELEMAC mesh outputs, use QGIS mesh-layer support where feasible.

Conceptually:

```python
layer = QgsMeshLayer(
    path,
    display_name,
    "mdal",
)
```

Check layer validity before adding it.

Support GIS-native outputs such as:

```text
GeoPackage
GeoTIFF
CSV
mesh datasets
VTK-compatible datasets where supported
```

Group imported layers in the layer tree.

Example:

```text
hydromate
└── f835ac7
    ├── TELEMAC result
    ├── maximum depth
    ├── maximum velocity
    ├── observations
    └── optimization optimum
```

Avoid silently modifying the current QGIS project beyond the requested imports.

---

# 21. QGIS Processing integration

Implement a Processing provider if appropriate.

Suggested algorithms:

```text
hydromate
├── Submit optimization
├── Import optimization results
└── Check optimization status
```

Potential model/input preparation algorithms may also be added where useful.

Important:

`Submit optimization` must **not** execute the entire long-running simulation inside the Processing algorithm.

It should:

```text
validate
create job
submit external runner
return job ID
```

Then finish.

---

# 22. QGIS tasks

`QgsTask` may be used for short-lived QGIS-specific operations such as:

* preparing large GIS datasets;
* reading large output metadata;
* loading/preprocessing files;
* other operations that are safe to terminate when QGIS exits.

Do **not** use `QgsTask` to own TELEMAC/OpenFOAM/BAL execution.

---

# 23. Logging

Adapt `hydromate.logsetup` (per-phase compound logfile, `log_step()` timings).

**Add rotation, which hydromate does not currently have.** A multi-day parallel run
produces a very large listing - TELEMAC prints a block per time step, and an interFoam
run at ~1e-3 s steps over hours produces hundreds of thousands - and an unbounded
`simulation.log` will fill the scratch volume and take the job down with it. Rotate by
size with a bounded number of parts, and keep the *last* part rather than the first:
when a run fails, the end is what matters.

Keep the solver's own native listing separate from the runner's log. `runner.log` is
hydromate's narration (state transitions, commands issued, exit codes); the solver's
output is its own artifact. §24 already requires errors in both.

---

# 24. Failure handling

Handle at minimum:

* missing TELEMAC installation;
* missing OpenFOAM installation;
* invalid setup script;
* missing input files;
* invalid solver profile;
* subprocess startup failure;
* MPI failure;
* solver nonzero exit code;
* malformed output;
* failed objective extraction;
* postprocessing failure;
* VTK conversion failure;
* insufficient disk space where detectable;
* deleted job directories;
* stale process metadata;
* QGIS closing during a job.

A solver failure must not corrupt the job registry.

Write meaningful error information to:

```text
status.json
runner.log
```

For example:

```json
{
  "state": "FAILED",
  "error": {
    "component": "telemac",
    "message": "...",
    "return_code": 1
  }
}
```

---

# 25. Configuration locations

Use sensible Linux or Windows user configuration/data directories.

Prefer platform-aware Python APIs rather than hardcoded paths where possible.

Conceptually separate in Linux:

```text
configuration
persistent jobs
logs
cache
```

For example on Linux:

```text
~/.config/hydromate/
~/.local/share/hydromate/
```

but implement this through an appropriate platform-aware mechanism.

Allow the job root / scratch location to be overridden, since CFD/hydrodynamic simulations may require large storage volumes.

Example:

```text
/scratch/user/hydromate/
```

---

# 26. Large-data considerations

TELEMAC/OpenFOAM jobs may produce very large outputs.

Do not:

* load complete result datasets into plugin memory unnecessarily;
* copy multi-GB output files unless required;
* serialize binary solver data into JSON;
* pass large data through QGIS signals;
* poll huge files repeatedly.

Use file paths and lightweight metadata.

The runner and QGIS plugin should communicate through:

```text
job metadata
status metadata
file paths
small structured results
```

not through solver field arrays.

---

# 27. Testing

Add automated tests.

At minimum:

## Core tests

* job schema parsing;
* status transitions;
* atomic state writes;
* job registry;
* solver profile validation;
* command construction;
* detached launcher abstraction;
* process cancellation;
* fake solver execution;
* result discovery.

## Solver tests

Where actual TELEMAC/OpenFOAM installations are unavailable, create mock/fake solver executables.

For example:

```text
fake_telemac.sh
fake_openfoam.sh
```

which:

* sleep;
* generate sample outputs;
* return configurable exit codes.

This allows testing the runner without requiring TELEMAC/OpenFOAM in CI.

## Plugin tests

Test what is practical without requiring full solver execution.

At minimum verify:

* plugin imports;
* metadata;
* runner-client behavior;
* result discovery;
* configuration serialization.

Plan separate compatibility testing against:

```text
QGIS 3.44
latest supported QGIS 3.x if applicable
QGIS 4.x
```

---

# 28. Dependency policy

Keep the QGIS plugin lightweight.

Do not bundle:

* TELEMAC;
* OpenFOAM;
* MPI;
* giant Python scientific environments.

External dependencies must be clearly detected and reported.

Where `hydromate` itself requires installation, implement a clear discovery mechanism.

For example:

```text
configured hydromate executable
```

**Discovery order, first hit wins**, so that a working default exists but the user can
always override:

1. the path configured in the plugin's settings;
2. `$HYDROMATE_EXE`;
3. `shutil.which("hydromate")`;
4. otherwise fail with an actionable message naming all three - never a silent
   fallback to a different interpreter.

Validate the find by running `hydromate --version` and checking it parses. A wrong or
half-installed executable must be caught at configuration time, not at submit time.

The QGIS Python environment should not need to become the solver's Python environment.

This is important.

QGIS may use one Python installation while TELEMAC/hydromate uses another.

The plugin should invoke `hydromate` as an external executable/process rather than importing solver-specific environments into QGIS.

---

# 29. Preserve standalone hydromate usage

After all changes, this must remain possible:

```bash
hydromate execute job.json
```

without QGIS installed.

The standalone optimizer/solver runner is the primary computational component.

QGIS is an optional frontend.

Do not introduce dependencies in the core package that make QGIS mandatory.

---

# 30. Documentation

Adopt the existing hydromate developer and user documentation.

At minimum document:

## Architecture

Explain:

```text
QGIS
  -> job submission
  -> persistent runner
  -> solver
  -> postprocessing
  -> result files
  -> QGIS import
```

## Installation

Explain:

* hydromate installation;
* QGIS plugin installation;
* TELEMAC configuration;
* OpenFOAM configuration;
* solver profiles.

## Job lifecycle

Explain:

```text
submit
monitor
close QGIS
reopen QGIS
rediscover
load results
```

## Debugging

Explain how to run a job manually:

```bash
hydromate execute job.json
```

and how to inspect:

```text
job.json
status.json
runner.log
```

---

# 31. Implementation quality

Use:

* type hints;
* dataclasses where suitable;
* `pathlib.Path`;
* enums for job state;
* explicit exceptions;
* dependency injection around launcher/solver interfaces where useful;
* clear module boundaries.

Avoid:

* giant GUI classes containing solver logic;
* global mutable job state;
* solver-specific logic in QGIS widgets;
* blocking calls in the main QGIS GUI thread;
* copy-pasted TELEMAC/OpenFOAM workflows;
* unstructured shell commands;
* undocumented magic paths.

---

# 32. Backward compatibility

Because `hydromate` already exists:

* preserve existing public APIs where practical;
* preserve existing CLI behavior unless there is a strong reason to change it;
* introduce migration/deprecation layers where needed;
* do not remove functioning solver capabilities simply to fit the new architecture.

If existing code conflicts with the architecture described here, refactor incrementally.

Document any breaking change before making it.

---

# 33. Implementation sequence

Phase 1 of the original draft ("repository analysis") is **done** - its answer is §2.
The sequence below starts from that, and each step must leave the test suite green and
`hydromate execute` working without QGIS.

## Step 1: config round-trip and one shared schema

`dump_config()` beside `load_config()`, and the last duplicated user inputs folded into
shared intent (`mesh.channel_size` vs `openfoam.cell_size`, `mesh.vertical_layers` vs
`openfoam.n_layers`; the ~8 solver-agnostic fields of `hydrodynamics` separated from
its ~24 TELEMAC keywords). A form-based Setup tab needs exactly one schema to render
and needs to write it back. Old keys keep loading, with a deprecation warning.

## Step 2: job model

`Job`, `JobState`, `JobRegistry`, `job.json`, `status.json`, atomic writes, the lock
file, the readable job ID, and `<job_root>/<job_id>/`. Pure Python, no solver, fully
testable. This is the foundation everything else stands on.

## Step 3: runner CLI verbs

`execute` (synchronous, the debugging path) first, then `submit`, `status`, `cancel`,
`logs`, `list`. `execute` is what proves the job model drives a real build before any
detachment exists to obscure the failures.

## Step 4: detached launchers

`SystemdUserLauncher`, `PosixDetachedLauncher`, `WindowsJobObjectLauncher`, behind
`JobLauncher`. Process-tree cancellation and the state transitions that go with it.
Verified with the fake solvers of §27, then with a real TELEMAC run.

## Step 5: adapt TELEMAC

Drive the existing pipeline through the job system. Acceptance: it survives termination
of the submitting shell.

## Step 6: adapt OpenFOAM

`postprocess` (reconstructPar, optional foamToVTK) and `export_qgis_results` on the
backend. The mesh, case build and two-stage run already exist.

## Step 7: QGIS plugin skeleton

Loading, 3.44/4.x compatibility, `compat.py`, executable discovery.

## Step 8: job dashboard

Submit / status / cancel / logs, with the polling policy of §18, and tabs generated
from the capability matrix (§17).

## Step 9: result loading

`ResultLoader`, mesh layers, layer-tree grouping, the styling defaults of §17.

## Step 10: Processing integration

Lightweight algorithms that submit and return, never execute.

## Step 11: packaging, tests, docs


# 34. Acceptance tests

The implementation is not complete until these workflows work.

## A. TELEMAC persistence

```text
1. Start QGIS.
2. Configure TELEMAC optimization.
3. Submit job.
4. Confirm TELEMAC starts.
5. Close QGIS completely.
6. Confirm TELEMAC and hydromate continue running.
7. Reopen QGIS.
8. Plugin rediscovers job.
9. Status is correct.
10. Let optimization finish.
11. Load TELEMAC results into QGIS.
```

## B. OpenFOAM persistence (loose)

With void functionality (no real results required), the following passes without errors:

```text
1. Submit OpenFOAM optimization.
2. Close QGIS.
3. OpenFOAM continues.
4. Postprocessing/VTK conversion continues.
5. Reopen QGIS.
6. Plugin rediscovers job.
7. Load generated results.
```

## C. Cancellation

```text
1. Start MPI solver optimization.
2. Cancel from QGIS.
3. Entire optimizer/solver/MPI process tree terminates.
4. No orphan solver processes remain.
5. Job becomes CANCELLED.
```

## D. Failure handling

```text
1. Configure an invalid solver.
2. Submit.
3. Job becomes FAILED.
4. QGIS remains responsive.
5. Meaningful error is shown.
6. Full error exists in runner.log.
```

## E. QGIS restart and `.hydromate-prj` file loading

```text
1. Submit several jobs.
2. Close QGIS.
3. Some finish and some remain running.
4. Start QGIS.
5. All jobs are rediscovered with correct states.
```

## F. Standalone operation

With QGIS completely absent:

```bash
hydromate execute job.json
```

must still work.

## G. QGIS compatibility

Plugin must load and operate on:

```text
QGIS 3.44+
QGIS 4.x
```

without maintaining separate plugin codebases.

---

# 35. How to work on this repository

Do not immediately generate large amounts of new code.

For each major change:

1. inspect the relevant existing implementation;
2. state what existing components will be reused;
3. make the smallest coherent architectural change;
4. run the relevant tests;
5. fix regressions;
6. continue to the next layer.

When choosing between approaches, prioritize:

1. solver-process independence from QGIS;
2. reliability for multi-hour/multi-day simulations;
3. recovery after QGIS restart;
4. clean separation between GUI and computational core;
5. Linux process-management correctness;
6. QGIS 3.44+/4.x compatibility;
7. maintainability;
8. ease of future HPC/SLURM support.

Do not optimize primarily for minimal code.

This is scientific/HPC workflow software; robustness and traceability matter more than shaving a few modules.

---

# 36. Future extensibility

Do not implement these unless they naturally fit the current work, but ensure the architecture does not prevent future execution backends such as:

```text
LLM-guided plugin usage
local workstation
systemd
SSH remote workstation
SLURM
PBS
```

The current TELEMAC/OpenFOAM executions themselves should remain native external processes according to the configured environment.

The launcher abstraction should make future HPC submission possible without rewriting the QGIS plugin.

Likewise, adding another solver should ideally require implementing another solver adapter rather than modifying the optimization and QGIS layers extensively.

---

# Final architectural rule

```text
hydromate core MUST NOT depend on QGIS.
solver code MUST NOT depend on QGIS.
QGIS MUST NOT own solver process lifetime.
```

