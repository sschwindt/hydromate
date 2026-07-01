"""Mesh-convergence (grid-independence) study for the 2D hydraulic case.

Runs the *same* steady simulation on a sequence of progressively finer meshes and
monitors quantities of interest phi (water depth h, scalar velocity U) at a fixed
set of probe points, until the relative change between successive refinements
falls below a tolerance (typically 1-5%). This is the formal way to quantify the
spatial-discretization error and demonstrate solution independence from the mesh.

Notes following standard practice:

* Mesh convergence is distinct from iterative (solver) convergence — the latter
  (residual reduction within one run) must be reached first or the comparison is
  meaningless. The runs here use TELEMAC's variable time step so each mesh marches
  at its own CFL-admissible dt (refining Delta x forces a smaller dt).
* Use at least three meshes spanning at least one doubling of cell size, so an
  observed order of convergence and a Grid Convergence Index (GCI) can be formed.

The TELEMAC execution is injected (the ``simulate`` callable), so the report
maths are unit-testable without the solver.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from hydromate import selafin
from hydromate.config import Config

log = logging.getLogger("hydromate")

DEFAULT_QUANTITIES = ("WATER DEPTH", "SCALAR VELOCITY")
GCI_SAFETY = 1.25  # Roache factor of safety for >=3 grids


# --------------------------------------------------------------------------- #
# quantity-of-interest extraction from a result SELAFIN
# --------------------------------------------------------------------------- #


def _resolve_field(values: dict[str, np.ndarray], qty: str) -> np.ndarray:
    """Node field for *qty*, deriving it from components/aliases when needed."""
    names = {k.upper(): k for k in values}
    q = qty.upper()
    if q in names:
        return values[names[q]]
    if q == "SCALAR VELOCITY":
        u = next((values[names[n]] for n in ("VELOCITY U", "U") if n in names), None)
        v = next((values[names[n]] for n in ("VELOCITY V", "V") if n in names), None)
        if u is not None and v is not None:
            return np.hypot(u, v)
    if q == "WATER DEPTH" and "FREE SURFACE" in names and "BOTTOM" in names:
        return values[names["FREE SURFACE"]] - values[names["BOTTOM"]]
    raise KeyError(f"quantity {qty!r} not in result (have {list(values)})")


def _interp_to_points(x, y, field, points):
    """Linear interpolation of a node field onto *points*; nearest for outliers."""
    from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator

    nodes = np.column_stack([x, y])
    out = LinearNDInterpolator(nodes, field)(points)
    nan = np.isnan(out)
    if nan.any():
        out[nan] = NearestNDInterpolator(nodes, field)(points[nan])
    return out


def extract_at_points(slf_path, quantities, points) -> dict[str, np.ndarray]:
    """Read the last frame of *slf_path* and sample *quantities* at *points*."""
    data = selafin.read_slf(slf_path, frame=-1)
    points = np.asarray(points, dtype=float)
    return {q: _interp_to_points(data["x"], data["y"], _resolve_field(data["values"], q),
                                 points) for q in quantities}


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #


@dataclass
class LevelResult:
    """One mesh in the sequence: its resolution, size and sampled QoI."""

    label: str
    cell_size: float                       # representative (channel) edge length [m]
    n_elem: int
    n_nodes: int
    min_edge: float
    dt: float | None                       # CFL-admissible time step estimate [s]
    runtime_s: float | None
    qoi: dict[str, np.ndarray]             # {quantity: (n_probes,) values}

    def repr_value(self, qty: str) -> float:
        """Representative scalar: mean magnitude over the probes."""
        return float(np.mean(np.abs(self.qoi[qty])))


@dataclass
class ConvergenceReport:
    levels: list[LevelResult]              # ordered coarse -> fine
    quantities: list[str]
    tolerance: float
    probes: np.ndarray
    rel_change: dict[str, list[float]] = field(default_factory=dict)   # per qty, len n-1
    observed_order: dict[str, float | None] = field(default_factory=dict)
    gci_fine: dict[str, float | None] = field(default_factory=dict)
    converged: bool = False
    case: str = ""                          # case name (for the report title)

    def format(self) -> str:
        lv = self.levels
        w = max(12, *(len(x.label) for x in lv))
        cols = "".join(f"{x.label:>{w}}" for x in lv)
        lines = [
            "Mesh-convergence study (coarse -> fine)",
            f"  probes: {len(self.probes)}   tolerance: {self.tolerance * 100:.1f}%",
            f"  {'cell size [m]':<22}" + "".join(f"{x.cell_size:>{w}.3f}" for x in lv),
            f"  {'elements':<22}" + "".join(f"{x.n_elem:>{w}d}" for x in lv),
            f"  {'nodes':<22}" + "".join(f"{x.n_nodes:>{w}d}" for x in lv),
            f"  {'shortest edge [m]':<22}" + "".join(f"{x.min_edge:>{w}.4f}" for x in lv),
            f"  {'time step dt [s]':<22}"
            + "".join((f"{x.dt:>{w}.4f}" if x.dt else f"{'-':>{w}}") for x in lv),
            f"  {'runtime [s]':<22}"
            + "".join((f"{x.runtime_s:>{w}.1f}" if x.runtime_s else f"{'-':>{w}}")
                      for x in lv),
            "  " + "-" * (22 + w * len(lv)),
        ]
        lines.append(f"  {'quantity phi':<22}" + cols)
        for q in self.quantities:
            row = "".join(f"{x.repr_value(q):>{w}.4f}" for x in lv)
            lines.append(f"  {q:<22}{row}")
            # relative change between successive meshes, aligned under the finer one
            rc = self.rel_change.get(q, [])
            cells = [f"{'':>{w}}"] + [f"{e * 100:>{w - 1}.2f}%" for e in rc]
            lines.append(f"    {'rel. change':<20}" + "".join(cells))
            p = self.observed_order.get(q)
            g = self.gci_fine.get(q)
            extra = []
            if p is not None:
                extra.append(f"observed order p={p:.2f}")
            if g is not None:
                extra.append(f"GCI(fine)={g * 100:.2f}%")
            if extra:
                lines.append(f"    {'':<20}" + "; ".join(extra))
        verdict = "CONVERGED" if self.converged else "NOT converged"
        lines.append(f"  => {verdict} at {self.tolerance * 100:.1f}% "
                     "(finest relative change vs. tolerance)")
        if not self.converged:
            lines.append("     refine further (the finest mesh still changes the QoI "
                         "by more than the tolerance).")
        return "\n".join(lines)

    def save(self, path) -> Path:
        path = Path(path)
        path.write_text(self.format() + "\n")
        return path

    def recommendation(self) -> dict:
        """Recommend the coarsest grid-independent cell size (convergence vs. cost).

        A mesh is adequate when refining it further (to the next finer mesh)
        changes every quantity of interest by less than the tolerance; the
        coarsest such mesh is the cheapest converged choice. Returns a dict with
        the recommended :class:`LevelResult`, its cell size, whether it converged,
        a one-line reason, and the runtime speed-up versus the finest mesh.
        """
        lv = self.levels
        chosen = None
        for i in range(len(lv) - 1):                       # coarse -> fine
            if all(self.rel_change[q][i] < self.tolerance for q in self.quantities):
                chosen = lv[i]
                break
        if chosen is None:
            chosen = lv[-1]
            converged = False
            reason = (f"no mesh met the {self.tolerance * 100:.0f}% tolerance; using "
                      "the finest (refine further to confirm grid independence)")
        else:
            converged = True
            reason = (f"coarsest mesh whose water depth and velocity stay within "
                      f"{self.tolerance * 100:.0f}% under refinement (grid-independent "
                      "and cheapest to run)")
        finest = lv[-1]
        speedup = (finest.runtime_s / chosen.runtime_s
                   if chosen.runtime_s and finest.runtime_s else None)
        return {"level": chosen, "cell_size": chosen.cell_size,
                "converged": converged, "reason": reason, "speedup_vs_finest": speedup}

    def to_xlsx(self, path) -> Path:
        """Write a styled .xlsx convergence report with the cell-size recommendation."""
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

        path = Path(path)
        lv = self.levels
        head_fill = PatternFill("solid", fgColor="1F4E78")     # dark blue
        head_font = Font(bold=True, color="FFFFFF")
        label_font = Font(bold=True)
        rec_fill = PatternFill("solid", fgColor="C6EFCE")      # green
        warn_fill = PatternFill("solid", fgColor="FFC7CE")     # red
        sub_fill = PatternFill("solid", fgColor="DDEBF7")      # light blue
        center = Alignment(horizontal="center")
        thin = Side(style="thin", color="BFBFBF")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        wb = Workbook()
        ws = wb.active
        ws.title = "mesh convergence"
        ncol = 1 + len(lv)

        def row(cells, *, fill=None, font=None, fmt=None, align=None):
            ws.append(list(cells))
            r = ws.max_row
            for c in range(1, len(cells) + 1):
                cell = ws.cell(row=r, column=c)
                if fill:
                    cell.fill = fill
                if font:
                    cell.font = font
                if fmt and c > 1 and isinstance(cell.value, (int, float)):
                    cell.number_format = fmt
                if align and c > 1:
                    cell.alignment = align
                cell.border = border
            return r

        # title
        title = "Mesh-convergence study"
        if getattr(self, "case", ""):
            title += f" - case '{self.case}'"
        ws.append([title])
        ws.merge_cells(start_row=1, end_row=1, start_column=1, end_column=ncol)
        ws["A1"].font = Font(bold=True, size=14)
        ws.append([f"probes: {len(self.probes)}    tolerance: {self.tolerance * 100:.1f}%    "
                   f"meshes: {len(lv)} (coarse to fine)"])
        ws.merge_cells(start_row=2, end_row=2, start_column=1, end_column=ncol)
        ws.append([])

        row(["", *[x.label for x in lv]], fill=head_fill, font=head_font, align=center)
        row(["cell size [m]", *[x.cell_size for x in lv]], font=label_font,
            fmt="0.000", align=center)
        row(["elements", *[x.n_elem for x in lv]], font=label_font, fmt="#,##0",
            align=center)
        row(["nodes", *[x.n_nodes for x in lv]], font=label_font, fmt="#,##0",
            align=center)
        row(["shortest edge [m]", *[x.min_edge for x in lv]], font=label_font,
            fmt="0.0000", align=center)
        row(["time step dt [s]", *[(x.dt or float('nan')) for x in lv]],
            font=label_font, fmt="0.0000", align=center)
        row(["runtime [s]", *[(x.runtime_s or float('nan')) for x in lv]],
            font=label_font, fmt="0.0", align=center)
        ws.append([])

        for q in self.quantities:
            row([q, *[x.repr_value(q) for x in lv]], fill=sub_fill, font=label_font,
                fmt="0.0000", align=center)
            rc = ["", ""] + [f"{e * 100:.2f}%" for e in self.rel_change.get(q, [])]
            row(["  rel. change vs. coarser", *rc[1:]], align=center)
            p, g = self.observed_order.get(q), self.gci_fine.get(q)
            extra = []
            if p is not None:
                extra.append(f"observed order p = {p:.2f}")
            if g is not None:
                extra.append(f"GCI(fine) = {g * 100:.2f}%")
            if extra:
                row(["  " + "; ".join(extra)])
        ws.append([])

        rec = self.recommendation()
        sp = rec["speedup_vs_finest"]
        sp_txt = f" (~{sp:.1f}x faster than the finest)" if sp and sp > 1 else ""
        r = row([f"RECOMMENDATION: cell size = {rec['cell_size']:.3f} m"
                 + (f", runtime ~{rec['level'].runtime_s:.0f} s{sp_txt}"
                    if rec['level'].runtime_s else "")],
                fill=rec_fill if rec["converged"] else warn_fill, font=label_font)
        ws.merge_cells(start_row=r, end_row=r, start_column=1, end_column=ncol)
        r = row([rec["reason"]])
        ws.merge_cells(start_row=r, end_row=r, start_column=1, end_column=ncol)

        ws.column_dimensions["A"].width = 28
        for c in range(2, ncol + 1):
            ws.column_dimensions[chr(64 + c)].width = 13
        wb.save(path)
        return path


# --------------------------------------------------------------------------- #
# report maths
# --------------------------------------------------------------------------- #


def _rel_change(prev: np.ndarray, curr: np.ndarray) -> float:
    """Relative L2 change between two probe vectors, normalised by the finer one."""
    denom = np.linalg.norm(curr)
    if denom == 0:
        return float(np.linalg.norm(curr - prev))
    return float(np.linalg.norm(curr - prev) / denom)


def build_report(levels: list[LevelResult], quantities, tolerance: float,
                 probes: np.ndarray) -> ConvergenceReport:
    """Assemble a :class:`ConvergenceReport` (relative change, order, GCI, verdict)."""
    rep = ConvergenceReport(levels=list(levels), quantities=list(quantities),
                            tolerance=tolerance, probes=np.asarray(probes))
    for q in quantities:
        changes = [_rel_change(levels[i].qoi[q], levels[i + 1].qoi[q])
                   for i in range(len(levels) - 1)]
        rep.rel_change[q] = changes
        rep.observed_order[q], rep.gci_fine[q] = _order_and_gci(levels, q)
    rep.converged = all(rep.rel_change[q] and rep.rel_change[q][-1] < tolerance
                        for q in quantities)
    return rep


def _order_and_gci(levels: list[LevelResult], q: str):
    """Observed order p and fine-grid GCI from the (>=3) coarsest..finest triplet."""
    if len(levels) < 3:
        return None, None
    coarse, med, fine = levels[-3], levels[-2], levels[-1]
    r1 = coarse.cell_size / med.cell_size
    r2 = med.cell_size / fine.cell_size
    if min(r1, r2) <= 1.0:
        return None, None
    f_c, f_m, f_f = coarse.repr_value(q), med.repr_value(q), fine.repr_value(q)
    e21, e32 = f_m - f_c, f_f - f_m
    if e21 == 0 or e32 == 0 or (e32 / e21) <= 0:
        return None, None
    p = math.log(abs(e21 / e32)) / math.log(0.5 * (r1 + r2))
    if not math.isfinite(p) or p <= 0:
        return p if math.isfinite(p) else None, None
    rel = abs(e32 / f_f) if f_f != 0 else abs(e32)
    gci = GCI_SAFETY * rel / (r2 ** p - 1.0)
    return p, gci


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #


def default_levels(cfg: Config) -> list[tuple[str, float, float]]:
    """Three meshes spanning one doubling of cell size around the config sizes.

    Returns ``(label, channel_size, floodplain_size)`` coarse -> fine. The finest
    matches the config; coarser ones double the cell size (so coarse/fine = 2)."""
    ch, fp = cfg.mesh.channel_size, cfg.mesh.floodplain_size
    return [("coarse", ch * 2.0, fp * 2.0),
            ("medium", ch * math.sqrt(2.0), fp * math.sqrt(2.0)),
            ("fine", ch, fp)]


def percent_levels(cfg: Config,
                   percents=(0.40, 0.20, 0.0, -0.20, -0.40)
                   ) -> list[tuple[str, float, float]]:
    """Cell-size levels at fractional offsets from the config size, coarse -> fine.

    Positive *percents* coarsen (larger cells), negative refine (smaller cells);
    the default gives two 40%/20%-coarser, the baseline, and two 20%/40%-finer
    meshes. Returns ``(label, channel_size, floodplain_size)``."""
    ch, fp = cfg.mesh.channel_size, cfg.mesh.floodplain_size
    levels = []
    for p in sorted(percents, reverse=True):           # coarse (large) -> fine (small)
        factor = 1.0 + p
        if abs(p) < 1e-9:
            label = "baseline"
        elif p > 0:
            label = f"+{p * 100:.0f}% coarser"
        else:
            label = f"-{-p * 100:.0f}% finer"
        levels.append((label, ch * factor, fp * factor))
    return levels


def default_probes(cfg: Config, n: int = 40) -> np.ndarray:
    """Probe points for the QoI: the ground-truth points if available, else samples
    along the channel centerline (falling back to the ROI bbox diagonal)."""
    gt = cfg.ground_truth_path
    if gt is not None and Path(gt).exists():
        try:
            from hydromate import ground_truth

            tables = ground_truth.read_tidy(gt)
            pts = np.vstack([t[["x", "y"]].to_numpy(dtype=float)
                             for t in tables.values() if {"x", "y"} <= set(t.columns)])
            if len(pts):
                return pts
        except Exception as exc:  # pragma: no cover - fallback only
            log.debug("probe points from ground truth unavailable: %s", exc)
    if cfg.geodata.channel_centerline is not None:
        import geopandas as gpd
        from shapely.ops import linemerge, unary_union

        gdf = gpd.read_file(cfg.geodata.channel_centerline)
        if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
            gdf = gdf.to_crs(epsg=cfg.crs_epsg)
        merged = unary_union(gdf.geometry.values)
        # linemerge only accepts a MultiLineString; a single merged LineString is
        # already the line (passing it to linemerge raises "Cannot linemerge")
        line = linemerge(merged) if merged.geom_type == "MultiLineString" else merged
        if line.geom_type == "MultiLineString":
            line = max(line.geoms, key=lambda g: g.length)
        d = np.linspace(0, line.length, n)
        return np.array([[line.interpolate(s).x, line.interpolate(s).y] for s in d])
    raise ValueError("no probe points: provide ground truth or a channel centerline, "
                     "or pass probes= explicitly to run_mesh_convergence")


def make_telemac_simulator(cfg: Config, discharge: float, *, base_dir: Path,
                           runtime=None, n_processors: int | None = None):
    """Build a ``simulate(label, ch, fp, probes, quantities) -> LevelResult`` that
    builds the case at the given resolution, runs TELEMAC once at constant
    *discharge* (variable time step for per-mesh CFL), and samples the QoI."""
    import copy
    import time

    from hydromate import pipeline, steering

    base_dir = Path(base_dir)
    # pin the turbulence closure to the baseline choice across all grid levels, so
    # the study isolates the discretization error (per-level auto-selection would
    # otherwise switch closures as the cell size changes and confound the comparison)
    pinned_turbulence = steering.select_turbulence_model(cfg)[0]

    def simulate(label, ch, fp, probes, quantities):
        lc = copy.deepcopy(cfg)
        lc.mesh.channel_size, lc.mesh.floodplain_size = ch, fp
        lc.hydrodynamics.turbulence_model = pinned_turbulence
        lc.boundaries.prescribed_flowrate = discharge
        # let TELEMAC pick a CFL-admissible dt for this mesh (iterative convergence
        # to steady state must be reached before meshes are compared). These runs are
        # pre-wetted (hotstart), so a faster 0.6 Courant target keeps the 5-mesh
        # study tractable; the production dry-start default is the safer 0.30.
        lc.hydrodynamics.variable_timestep = True
        lc.hydrodynamics.desired_courant = 0.6
        if n_processors:
            lc.telemac.n_processors = n_processors
        # filesystem-safe per-level folder (labels carry spaces/%/+/- that TELEMAC
        # and shells dislike in paths)
        slug = "".join(c if c.isalnum() else "_" for c in label).strip("_") or "level"
        d = base_dir / slug
        lc.preprocessing_dir = d / "preprocessing"
        lc.model_dir = d / "model"
        lc.postprocessing_dir = d / "postprocessing"
        lc.calibration_dir = d / "calibration"
        lc.ensure_dirs()

        t0 = time.time()
        # reuse the parent run's one compound logfile (no per-level logfiles)
        pipeline.run(lc, validate_env=False, dry_run=True, log_to_file=False)
        runtime_s = time.time() - t0

        res = lc.model_path(lc.results_slf)
        data = selafin.read_slf(res, frame=-1)
        min_edge, mean_depth, mean_vel = _geom_stats(data, probes)
        dt = _cfl_dt(min_edge, mean_depth, mean_vel)
        qoi = extract_at_points(res, quantities, probes)
        return LevelResult(label=label, cell_size=ch, n_elem=data["ikle"].shape[0],
                           n_nodes=data["x"].size, min_edge=min_edge, dt=dt,
                           runtime_s=runtime_s, qoi=qoi)

    return simulate


def _geom_stats(data: dict, probes):
    """(shortest edge, mean probe depth, mean probe velocity) from a result dict."""
    x, y, ikle = data["x"], data["y"], data["ikle"]
    p = ikle
    e = np.concatenate([
        np.hypot(x[p[:, 0]] - x[p[:, 1]], y[p[:, 0]] - y[p[:, 1]]),
        np.hypot(x[p[:, 1]] - x[p[:, 2]], y[p[:, 1]] - y[p[:, 2]]),
        np.hypot(x[p[:, 2]] - x[p[:, 0]], y[p[:, 2]] - y[p[:, 0]]),
    ])
    probes = np.asarray(probes, dtype=float)
    try:
        depth = float(np.nanmean(_interp_to_points(
            x, y, _resolve_field(data["values"], "WATER DEPTH"), probes)))
        vel = float(np.nanmean(_interp_to_points(
            x, y, _resolve_field(data["values"], "SCALAR VELOCITY"), probes)))
    except KeyError:
        depth, vel = 1.0, 1.0
    return float(e.min()), max(depth, 1e-3), max(vel, 0.0)


def _cfl_dt(min_edge: float, depth: float, velocity: float, courant: float = 0.9) -> float:
    """CFL-admissible time step dt = C * dx / (|U| + sqrt(g*h))."""
    celerity = velocity + math.sqrt(9.81 * depth)
    return courant * min_edge / celerity if celerity > 0 else float("nan")


def run_mesh_convergence(cfg: Config, *, discharge: float, levels=None,
                         quantities=DEFAULT_QUANTITIES, probes=None,
                         tolerance: float = 0.02, simulate=None,
                         base_dir: Path | None = None, runtime=None,
                         n_processors: int | None = None) -> ConvergenceReport:
    """Run the convergence study and return the :class:`ConvergenceReport`.

    *levels* is a list of ``(label, channel_size, floodplain_size)`` coarse->fine
    (default: one doubling around the config sizes). *simulate* overrides the
    TELEMAC runner (used in tests); by default each mesh is built and run once at
    constant *discharge*. *probes* default to the ground-truth / centerline points.
    """
    levels = levels or default_levels(cfg)
    quantities = list(quantities)
    probes = np.asarray(probes if probes is not None else default_probes(cfg), float)
    if base_dir is None:
        base_dir = cfg.postprocessing_path("mesh-convergence")
    simulate = simulate or make_telemac_simulator(
        cfg, discharge, base_dir=base_dir, runtime=runtime, n_processors=n_processors)

    results: list[LevelResult] = []
    for label, ch, fp in levels:
        log.info("mesh convergence: running '%s' (channel %.3f m, floodplain %.3f m)",
                 label, ch, fp)
        results.append(simulate(label, ch, fp, probes, quantities))
        r = results[-1]
        log.info("  '%s': %d elements, shortest edge %.4f m, dt~%.4f s",
                 label, r.n_elem, r.min_edge, r.dt if r.dt else float("nan"))
    report = build_report(results, quantities, tolerance, probes)
    report.case = getattr(cfg, "name", "")
    rec = report.recommendation()
    log.info("mesh convergence: %s at %.1f%% tolerance; recommended cell size %.3f m",
             "CONVERGED" if report.converged else "NOT converged",
             tolerance * 100, rec["cell_size"])
    return report
