# aXqua for QGIS

Set up, run and monitor TELEMAC and OpenFOAM river models from QGIS — with simulations
that **keep running after you close QGIS**.

## What it is

A frontend. The plugin does not run solvers and does not do heavy numerics; it talks to
the separately installed [`axqua`](https://github.com/sschwindt/aXqua)
command-line tool, which owns the model build, the solver runs and the calibration.

That separation is the whole design. QGIS ships its own Python, while aXqua needs
gmsh, rasterio, geopandas and a TELEMAC or OpenFOAM environment. Forcing those into the
QGIS interpreter would make installation fragile and would break on every QGIS update, so
the boundary is a subprocess and a JSON document — **the plugin never imports aXqua**.

The second consequence is the important one: because the plugin does not own the solver
process, closing QGIS does not stop your simulation. Jobs are launched as detached system
jobs (a systemd user unit on Linux, a Job Object on Windows, a detached session inside the
distro under WSL). Reopen QGIS days later, open your `.axqua-prj`, and the jobs are
rediscovered with whatever state they have actually reached.

## Requirements

| | |
|---|---|
| QGIS | 3.44 or newer, including QGIS 4.x |
| aXqua | `pip install git+https://github.com/sschwindt/aXqua.git` (0.3 or newer), in an environment that can reach your solver. Not on PyPI yet. |
| TELEMAC | any version reachable through its `pysource.*.sh` (Linux/macOS) or `pysource.bat` (Windows) |
| OpenFOAM | Foundation build (openfoam.org); on Windows this means WSL |

Nothing is bundled: no TELEMAC, no OpenFOAM, no MPI, no scientific Python stack.

## Setting it up

1. Install aXqua in the environment that already has your solver tooling, and check
   it works: `axqua --version`.
2. In QGIS, install this plugin, then open **aXqua** from the toolbar.
3. On the **Setup** tab, press **Check**. If aXqua is not on `PATH`, set its path in
   **Settings…** — this is validated immediately, so a wrong path is caught here rather
   than when you submit a six-hour run.
4. **Add case…** and pick a `case-config.yml`, then **Save as…** to write a
   `.axqua-prj` next to it.

Optionally describe your solver installs once, in `~/.config/axqua/profiles.yml`, and
select the profile on the Setup tab. Run `axqua profiles validate` to check them.

## Using it

The tabs between **Setup** and **Jobs** are **generated from what aXqua says your case
can do**, not hardcoded. A capability that does not apply to your solver is hidden; one
aXqua has not implemented yet is shown disabled with the reason. Add a capability to
aXqua and it appears here with no plugin update.

Submit from a capability tab, then watch the **Jobs** tab: job id, solver, kind, state,
progress and best objective, refreshing while something is running and going quiet when
nothing is. From there you can cancel (which stops the whole process tree, MPI ranks
included), view either log, open the job folder, or load the results.

Results arrive as styled layers under `axqua/<job id>`: water depth on a white-to-blue
ramp with everything below your minimum depth transparent, and velocity as arrows on a
reversed *plasma* ramp capped at 5 m/s. Everything else points you at Layer Symbology,
because a plausible-looking wrong scale is harder to spot than an obviously default one.

**Processing**: `Submit a simulation job`, `Check job status` and `Import job results` are
available in the Processing toolbox and the Graphical Modeler. `Submit` returns a job id
in about a second — it never blocks on the simulation.

## Where things live

| what | where | who writes it |
|---|---|---|
| the model | `case-config.yml` | you (or aXqua) |
| which cases and where jobs go | `<name>.axqua-prj` | this plugin |
| your solver installs | `~/.config/axqua/profiles.yml` | you |
| what a job was asked to do | `<job>/job.json` | aXqua, once |
| what a job is doing now | `<job>/status.json` | aXqua's runner |

The project file deliberately carries **no simulation status**. The runner writes status
while QGIS is closed, so a copy here would be stale — and two open QGIS windows would
fight over it.

## Troubleshooting

**"aXqua could not be found"** — the message lists all three places it looked (the
plugin setting, `$AXQUA_EXE`, `PATH`). Set the path in Settings and press Test.

**A job shows FAILED immediately** — open **View logs**. `runner.log` is aXqua's
narration and carries the error with a suggested remedy; the solver's own listing is
behind the toggle.

**A job is stuck in RUNNING** — it will not be. aXqua reconciles a recorded process
that is gone (including after a reboot or `wsl --shutdown`) and reports FAILED rather than
leaving a job that can never finish. Press **Refresh**.

**No movie was produced** — install `ffmpeg`. Without it the PNG frames are kept and the
dialog shows the command to encode them later.

## Licence

GPL-2.0-or-later, because it links PyQGIS. The `axqua` package it drives is
BSD-3-Clause and remains usable on its own.
