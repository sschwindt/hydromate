"""Has the OpenFOAM run reached a steady discharge balance?

The 3D analogue of :mod:`axqua.flux_convergence`, and deliberately the same
judgement: a free-surface run is converged when the **water** entering equals the
water leaving, measured as a relative imbalance against the prescribed inflow. The
tolerance grades carry over unchanged (``hydrodynamics.flux_tolerance``, 1e-3 for a
result read as discharge/depth/velocity), so a 2D and a 3D run of the same reach are
held to the same standard and the numbers are directly comparable.

The data come from the ``surfaceFieldValue`` monitors
:func:`axqua.solvers.openfoam.dicts._flux_functions` writes into ``controlDict``:
``sum(phi)`` weighted by ``alpha.water`` over each inlet and outlet patch, i.e. the
water discharge alone rather than the water-plus-air flux ``phi`` would give.
OpenFOAM writes one ``surfaceFieldValue.dat`` per monitor under
``postProcessing/<name>/<startTime>/``.

Sign convention: ``phi`` is positive out of the domain, so an inlet reports a
negative value. This module reports magnitudes, as the TELEMAC side does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("axqua")

STEADY_WINDOW = 10          # consecutive samples that must hold the tolerance


@dataclass
class DischargeHistory:
    """Per-patch water discharge against time, and the balance between them."""

    time: np.ndarray
    per_patch: dict[str, np.ndarray] = field(default_factory=dict)
    inflow: np.ndarray | None = None
    outflow: np.ndarray | None = None
    target: float = 0.0
    tolerance: float = 1.0e-3
    steady_time: float | None = None
    converged: bool = False

    @property
    def imbalance(self) -> np.ndarray:
        """Relative |Q_in| - |Q_out| over |Q_in|, the same measure as in 2D."""
        if self.inflow is None or self.outflow is None:
            return np.zeros(0)
        denom = np.where(np.abs(self.inflow) > 1e-12, np.abs(self.inflow), np.nan)
        return (np.abs(self.inflow) - np.abs(self.outflow)) / denom

    def lines(self) -> list[str]:
        if self.time.size == 0:
            return ["no discharge monitors found - has the run written any output yet?"]
        out = [f"discharge balance over {self.time.size} samples "
               f"(t = {self.time[0]:.2f} .. {self.time[-1]:.2f} s)"]
        for name, values in self.per_patch.items():
            out.append(f"  {name:<12}: {np.abs(values[-1]):8.4f} m3/s "
                       f"(mean of last 10: {np.abs(values[-10:]).mean():.4f})")
        if self.inflow is not None and self.outflow is not None:
            imb = self.imbalance
            out.append(f"  inflow total : {np.abs(self.inflow[-1]):8.4f} m3/s "
                       f"(prescribed {self.target:g})")
            out.append(f"  outflow total: {np.abs(self.outflow[-1]):8.4f} m3/s")
            out.append(f"  imbalance    : {100 * imb[-1]:+8.3f}% "
                       f"(tolerance {100 * self.tolerance:g}%)")
        if self.converged:
            out.append(f"  CONVERGED at t = {self.steady_time:.2f} s: the balance held "
                       f"inside {100 * self.tolerance:g}% for {STEADY_WINDOW} "
                       "consecutive samples")
        else:
            out.append("  NOT yet converged - extend openfoam.end_time, or check "
                       "whether the inflow has reached the prescribed discharge at all")
        return out


def _read_monitor(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Read one ``surfaceFieldValue.dat`` into ``(time, value)``."""
    times, values = [], []
    with open(path) as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                times.append(float(parts[0]))
                values.append(float(parts[1]))
            except ValueError:
                continue
    return np.asarray(times), np.asarray(values)


def read_monitors(case_dir: str | Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Every ``Q_*`` monitor in the case, concatenated across restart directories.

    A two-stage run writes ``postProcessing/Q_inlet_1/0/`` for the spin-up and
    ``.../30/`` for the production stage; both are read and joined in time order so
    the history spans the whole run rather than only its last leg.
    """
    root = Path(case_dir) / "postProcessing"
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    if not root.is_dir():
        return out
    for monitor in sorted(root.iterdir()):
        if not monitor.is_dir() or not monitor.name.startswith("Q_"):
            continue
        chunks = []
        for start in sorted(monitor.iterdir(), key=lambda p: _as_float(p.name)):
            dat = start / "surfaceFieldValue.dat"
            if dat.is_file():
                chunks.append(_read_monitor(dat))
        if not chunks:
            continue
        time = np.concatenate([c[0] for c in chunks])
        value = np.concatenate([c[1] for c in chunks])
        order = np.argsort(time, kind="stable")
        # a restart re-reports its start time; keep the later sample
        time, value = time[order], value[order]
        keep = np.ones(time.size, dtype=bool)
        keep[:-1] = time[1:] != time[:-1]
        name = monitor.name[2:].replace("_", "-")
        out[name] = (time[keep], value[keep])
    return out


def _as_float(name: str) -> float:
    try:
        return float(name)
    except ValueError:
        return float("inf")


def analyse(cfg, case_dir: str | Path | None = None, *,
            target: float | None = None) -> DischargeHistory:
    """Build the discharge history and decide whether the run has settled."""
    case_dir = Path(case_dir) if case_dir else cfg.openfoam_case_dir
    monitors = read_monitors(case_dir)
    tolerance = float(cfg.hydrodynamics.flux_tolerance)
    if target is None:
        target = float(cfg.boundaries.prescribed_flowrate or 0.0)
    if not monitors:
        return DischargeHistory(time=np.zeros(0), target=target, tolerance=tolerance)

    # the monitors share a write interval, but a restart can leave them ragged;
    # interpolate every patch onto the shortest common time base
    base = min((t for t, _ in monitors.values()), key=lambda t: t[-1] if t.size else 0)
    per_patch = {name: np.interp(base, t, v) for name, (t, v) in monitors.items()}

    inlets = [v for k, v in per_patch.items() if k.startswith("inlet")]
    outlets = [v for k, v in per_patch.items() if k.startswith("outlet")]
    history = DischargeHistory(
        time=base, per_patch=per_patch, target=target, tolerance=tolerance,
        inflow=np.sum(inlets, axis=0) if inlets else None,
        outflow=np.sum(outlets, axis=0) if outlets else None,
    )
    if history.inflow is not None and history.outflow is not None:
        _find_steady(history)
    for line in history.lines():
        log.info("%s", line)
    return history


def _find_steady(history: DischargeHistory) -> None:
    """First time the relative imbalance holds the tolerance for STEADY_WINDOW samples."""
    imb = np.abs(history.imbalance)
    good = np.isfinite(imb) & (imb <= history.tolerance)
    if good.size < STEADY_WINDOW:
        return
    # a run of STEADY_WINDOW consecutive True values
    window = np.convolve(good.astype(int), np.ones(STEADY_WINDOW, dtype=int), "valid")
    hits = np.flatnonzero(window == STEADY_WINDOW)
    if hits.size:
        history.converged = True
        history.steady_time = float(history.time[hits[0]])


# --------------------------------------------------------------------------- #
# did the 2D seed box the run in?
# --------------------------------------------------------------------------- #

# How much contact is not a finding. The lid stands clear of the water everywhere by
# construction, so anything but numerical noise there is real. The lateral wall is
# different: the domain ends somewhere, and at the inflow and outflow the channel
# necessarily runs into that edge - so a small share of the banks patch is wet in every
# healthy run, and a threshold of zero would cry wolf on all of them.
LID_CONTACT_TOLERANCE = 0.001    # of the lid patch area
WALL_CONTACT_TOLERANCE = 0.02    # of the banks patch area


@dataclass
class SurfaceFreedom:
    """Whether the free surface was ever stopped by the mesh rather than the flow."""

    lid_area: float = 0.0        # peak water area on the top patch [m2]
    wall_area: float = 0.0       # peak water area on the lateral wall [m2]
    lid_total: float = 0.0       # the top patch's own area [m2]
    wall_total: float = 0.0      # the banks patch's own area [m2]
    prescribed: bool = False     # rigid lid: the surface never had freedom to lose
    measured: bool = False       # were the monitors there to read at all?

    @property
    def lid_fraction(self) -> float:
        return self.lid_area / self.lid_total if self.lid_total > 0 else 0.0

    @property
    def wall_fraction(self) -> float:
        return self.wall_area / self.wall_total if self.wall_total > 0 else 0.0

    @property
    def hit_lid(self) -> bool:
        return self.lid_fraction > LID_CONTACT_TOLERANCE

    @property
    def hit_wall(self) -> bool:
        return self.wall_fraction > WALL_CONTACT_TOLERANCE

    @property
    def free(self) -> bool:
        return not self.hit_lid and not self.hit_wall

    def lines(self, cfg) -> list[str]:
        if self.prescribed:
            return ["surface   : prescribed by the 2D seed (mode: rigid-lid), not "
                    "solved - this run cannot show a jump, a standing wave or "
                    "superelevation"]
        if not self.measured:
            return []
        of = cfg.openfoam
        if self.free:
            return [f"surface   : free - it stayed clear of the lid "
                    f"({self.lid_fraction:.2%} of that patch ever wet) and of the "
                    f"lateral wall ({self.wall_fraction:.2%}), so the 2D seed bounded "
                    "it without constraining it"]
        out = ["surface   : ! CONSTRAINED by the mesh, not by the flow"]
        if self.hit_lid:
            out.append(f"  water reached the lid over up to {self.lid_area:,.1f} m2 "
                       f"({self.lid_fraction:.1%} of it). The freeboard "
                       f"({of.freeboard:g} m) was too small, so the surface could not "
                       "rise as far as the hydraulics wanted. Raise openfoam.freeboard "
                       "and rebuild - the result at that level is not physical.")
        if self.hit_wall:
            out.append(f"  water reached the lateral wall over up to "
                       f"{self.wall_area:,.1f} m2 ({self.wall_fraction:.1%} of it, "
                       f"past the {WALL_CONTACT_TOLERANCE:.0%} that inflow and outflow "
                       "corners account for). The footprint (wet_margin "
                       f"{of.wet_margin:g} m past the 2D water line) stopped the flow "
                       "spreading. Raise openfoam.wet_margin, or set domain: roi.")
        return out


def surface_freedom(cfg, case_dir: str | Path | None = None) -> SurfaceFreedom:
    """Read the ``lidContact`` / ``wallContact`` monitors into a verdict.

    The seed fixes two things the flow has no say in - how high the lid stands and
    how far the footprint reaches - and both are sized from a TELEMAC result that is
    itself a model. If the 3D surface reached either, the answer was set by a meshing
    decision rather than by the hydraulics, and nothing else in the output would say
    so: the run converges, the discharge balances, the picture looks like a river.

    Zero on both is the result you want. Reported per run rather than checked at
    build time, because it is the only moment the question can actually be answered.
    """
    if cfg.openfoam.mode == "rigid-lid":
        return SurfaceFreedom(prescribed=True, measured=True)
    root = Path(case_dir) if case_dir else cfg.openfoam_case_dir
    lid = _read_named_monitor(root, "lidContact")
    wall = _read_named_monitor(root, "wallContact")
    if lid is None and wall is None:
        return SurfaceFreedom()
    lid = lid or (np.zeros(0), 0.0)
    wall = wall or (np.zeros(0), 0.0)
    return SurfaceFreedom(
        lid_area=float(np.max(lid[0])) if lid[0].size else 0.0,
        wall_area=float(np.max(wall[0])) if wall[0].size else 0.0,
        lid_total=lid[1], wall_total=wall[1], measured=True)


def _read_named_monitor(root: Path, name: str) -> tuple[np.ndarray, float] | None:
    """``(values, patch area)`` for one non-``Q_`` monitor, across restart folders.

    The patch area comes out of the ``# Area :`` header OpenFOAM writes, which is what
    turns a bare contact area into a fraction - and a fraction is the only form in
    which "did the water touch this?" has a threshold that means the same thing on a
    30 m side channel and a 300 m reach.
    """
    folder = root / "postProcessing" / name
    if not folder.is_dir():
        return None
    chunks, area = [], 0.0
    for start in sorted(folder.iterdir(), key=lambda p: _as_float(p.name)):
        dat = start / "surfaceFieldValue.dat"
        if dat.is_file():
            chunks.append(_read_monitor(dat)[1])
            area = max(area, _patch_area(dat))
    return (np.concatenate(chunks) if chunks else np.zeros(0)), area


def _patch_area(path: Path) -> float:
    """The ``# Area :`` line of a surfaceFieldValue header, or 0 when absent."""
    with open(path) as fh:
        for line in fh:
            if not line.startswith("#"):
                break
            if line.startswith("# Area"):
                try:
                    return float(line.split(":")[1])
                except (IndexError, ValueError):
                    return 0.0
    return 0.0


def write_report(cfg, case_dir: str | Path | None = None, *,
                 out_dir: str | Path | None = None,
                 target: float | None = None) -> tuple[DischargeHistory, list[Path]]:
    """Analyse the run and write ``discharge-convergence.csv`` + ``.png``."""
    case_dir = Path(case_dir) if case_dir else cfg.openfoam_case_dir
    out_dir = Path(out_dir) if out_dir else case_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    history = analyse(cfg, case_dir, target=target)
    if history.time.size == 0:
        return history, []

    written = [_write_csv(history, out_dir / "discharge-convergence.csv")]
    plot = _write_plot(history, out_dir / "discharge-convergence.png")
    if plot is not None:
        written.append(plot)
    for line in surface_freedom(cfg, case_dir).lines(cfg):
        log.info("%s", line)
    return history, written


def _write_csv(history: DischargeHistory, path: Path) -> Path:
    import pandas as pd

    data = {"time_s": history.time}
    for name, values in history.per_patch.items():
        data[f"Q_{name}_m3s"] = np.abs(values)
    if history.inflow is not None:
        data["Q_in_m3s"] = np.abs(history.inflow)
    if history.outflow is not None:
        data["Q_out_m3s"] = np.abs(history.outflow)
        data["relative_imbalance"] = history.imbalance
    pd.DataFrame(data).to_csv(path, index=False)
    log.info("wrote %s", path)
    return path


def _write_plot(history: DischargeHistory, path: Path) -> Path | None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax_q, ax_i) = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    for name, values in history.per_patch.items():
        ax_q.plot(history.time, np.abs(values), lw=1.2, label=name)
    if history.target:
        ax_q.axhline(history.target, color="k", ls="--", lw=1,
                     label=f"prescribed {history.target:g} m3/s")
    ax_q.set_ylabel("water discharge [m3/s]")
    ax_q.legend(fontsize=8, ncol=2)
    ax_q.grid(alpha=0.3)
    ax_q.set_title("OpenFOAM boundary discharge and mass balance")

    if history.inflow is not None and history.outflow is not None:
        ax_i.plot(history.time, 100 * np.abs(history.imbalance), lw=1.2, color="C3")
        ax_i.axhline(100 * history.tolerance, color="k", ls=":", lw=1,
                     label=f"tolerance {100 * history.tolerance:g}%")
        if history.converged:
            ax_i.axvline(history.steady_time, color="C2", lw=1,
                         label=f"converged t={history.steady_time:.1f} s")
        ax_i.set_yscale("log")
        ax_i.legend(fontsize=8)
    ax_i.set_ylabel("|relative imbalance| [%]")
    ax_i.set_xlabel("time [s]")
    ax_i.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    log.info("wrote %s", path)
    return path
