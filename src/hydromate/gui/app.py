"""Streamlit configuration editor for hydromate.

A browser form to create / edit a hydromate YAML config, mirroring the dataclass
schema in :mod:`hydromate.config`, with the ability to save the YAML and run
``hydromate --check`` (validate) or a full build directly from the browser.

It edits the **case configuration** only; the rest of the workflow (test run,
mesh-convergence study, calibration, the optional 3D extension) is run from the
per-case scripts - see the **Workflow** tab and the docs.

Launch with ``hydromate-gui`` (after ``pip install -e ".[gui]"``).
"""

from __future__ import annotations

import copy
import math
import subprocess
from pathlib import Path

import yaml
import streamlit as st

SOLVERS = ["telemac2d", "telemac3d"]
LAWS = {0: "NOFR", 1: "HAAL", 2: "CHEZ", 3: "STRI", 4: "MANN", 5: "NIKU", 7: "COWH"}
# turbulence model: "auto" picks from the mesh resolution + velocity guess; the
# integers are the TELEMAC-2D model numbers (finite volumes accept only 1).
TURB_OPTIONS = ["auto", 1, 3, 4, 6]
TURB_LABELS = {
    "auto": "auto (from mesh resolution + U guess)", 1: "1 - constant viscosity",
    3: "3 - k-epsilon", 4: "4 - Smagorinski (LES)", 6: "6 - Spalart-Allmaras",
}
OUTFLOW_CONDITIONS = ["elevation", "stage_discharge", "free"]
QUANTITIES = [
    "WATER DEPTH", "SCALAR VELOCITY", "VELOCITY U", "VELOCITY V",
    "TURBULENT ENERG", "CUMUL BED EVOL",
]
SAMPLING = ["sobol", "random", "latin_hypercube"]
# optional geodata paths (blank = omit); the anisotropic-meshing + roughness layers
# come first as they drive the default (flow-aligned) mesh and zonal friction.
OPTIONAL_GEODATA = [
    "dem_target", "mesh_zones", "channel_centerline", "roughness_zones",
    "roughness_table", "breaklines", "region_points", "region_table",
    "dem_of_difference",
]

TEMPLATE: dict = {
    "project": {"name": "case", "crs_epsg": 25832,
                "preprocessing_dir": "hydromate-case/preprocessing",
                "model_dir": "hydromate-case/simulation",
                "postprocessing_dir": "hydromate-case/postprocessing",
                "calibration_dir": "hydromate-case/calibration-validation"},
    "telemac": {"pysource": "/path/to/telemac/configs/pysource.sh",
                "solver": "telemac2d", "n_processors": 4},
    "geodata": {"dem_initial": "path/to/dem.tif", "boundary": "path/to/roi.gpkg"},
    "boundaries": {"liquid_boundaries": "path/to/liquid-boundaries.gpkg",
                   "outflow_condition": "elevation", "prescribed_flowrate": 47.2,
                   "prescribed_elevation": 0.0},
    "initialization": {},
    "mesh": {"channel_size": 0.5, "floodplain_size": 1.5, "refinement_size": 0.5,
             "growth_ratio": 1.2, "channel_anisotropy": 4.0, "max_aspect_ratio": 4.0},
    "friction": {"default_law": 4, "default_coefficient": 0.03, "roughness_law": 5,
                 "boundary_law": 4, "boundary_coefficient": 0.03, "zones": []},
    "hydrodynamics": {"regime": "steady", "finite_volumes": False,
                      "turbulence_model": "auto", "initial_velocity_guess": 1.0,
                      "time_step": 1.0,
                      "n_time_steps": 15000, "desired_courant": 0.6,
                      "graphic_printout_period": 500, "listing_printout_period": 500},
    "calibration": {"calibration_quantities": ["WATER DEPTH"],
                    "extraction_quantities": ["WATER DEPTH", "SCALAR VELOCITY"],
                    "measurement_error": 0.10, "init_runs": 30, "max_runs": 50,
                    "parameter_sampling_method": "sobol", "gp_library": "gpy",
                    "parameters": [{"name": "zone1", "min": 0.02, "max": 0.04,
                                    "comment": ""}]},
}


def _get(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if d != {} else default


def _opt_float(label, value, *, fmt="%.4f", help=None):
    """A text box that yields a float when filled, or None when blank (omitted)."""
    raw = st.text_input(label, "" if value in (None, "") else str(value), help=help)
    return float(raw) if str(raw).strip() else None


def build_yaml(cfg: dict) -> str:
    """Serialise the working config to YAML, dropping empty / inapplicable fields."""
    out = copy.deepcopy(cfg)
    out["geodata"] = {k: v for k, v in out.get("geodata", {}).items()
                      if v not in (None, "", "path/to/")}
    # boundaries: drop blank optional knobs; the rating only matters for
    # stage_discharge, the prescribed elevation only for the 'elevation' outflow.
    b = out.get("boundaries", {})
    for key in ("inflow", "stage_discharge", "prescribed_flowrate", "prescribed_elevation"):
        if b.get(key) in (None, "", "path/to/"):
            b.pop(key, None)
    if b.get("outflow_condition") != "elevation":
        b.pop("prescribed_elevation", None)
    if b.get("outflow_condition") != "stage_discharge":
        b.pop("stage_discharge", None)
    # ground truth: drop when neither a measurements table nor raw sources are given
    gt = out.get("ground_truth", {})
    if not gt.get("sources"):
        gt.pop("sources", None)
    if gt.get("measurements") in (None, ""):
        gt.pop("measurements", None)
    if not gt:
        out.pop("ground_truth", None)
    # initialization: drop blank optional knobs (and the section if left at defaults)
    ini = out.get("initialization", {})
    for key in ("dry_start_extent", "prewet_depth"):
        if ini.get(key) in (None, ""):
            ini.pop(key, None)
    if not ini:
        out.pop("initialization", None)
    h = out.get("hydrodynamics", {})
    for key in ("turbulence_length_scale", "velocity_diffusivity"):
        if h.get(key) in (None, ""):
            h.pop(key, None)
    for sec, key in (("mesh", "region_sizes"), ("friction", "zones")):
        if not out.get(sec, {}).get(key):
            out.get(sec, {}).pop(key, None)
    if not out.get("morphodynamics", {}).get("enabled"):
        out.pop("morphodynamics", None)
    # DoD: drop blank optional floats, and the whole section when disabled
    dod = out.get("dem_of_difference", {})
    for key in ("uncertainty_initial", "uncertainty_target", "min_lod"):
        if dod.get(key) in (None, ""):
            dod.pop(key, None)
    if not dod.get("enabled"):
        out.pop("dem_of_difference", None)
    return yaml.safe_dump(out, sort_keys=False, default_flow_style=False, allow_unicode=True)


def run_cli(args: list[str]) -> tuple[int, str]:
    """Run the hydromate CLI and capture combined output."""
    proc = subprocess.run(["hydromate", *args], text=True, capture_output=True)
    return proc.returncode, (proc.stdout + proc.stderr)


# --------------------------------------------------------------------------- #
# trapezoidal stage-discharge helpers (Manning-Strickler normal flow)
#
# The production rating pipeline (hydromate.rating) uses a *symmetric* section; the
# GUI calculator additionally supports independent left/right bank slopes, so the
# area/perimeter are recomputed here rather than reusing rating._conveyance_q.
#   A(h) = b*h + 0.5*(m_L + m_R)*h^2                       (bottom + two triangles)
#   P(h) = b + h*sqrt(1+m_L^2) + h*sqrt(1+m_R^2)           (bottom + two banks)
# with side slopes m as horizontal:vertical (bank inclination 1:m).
# --------------------------------------------------------------------------- #


def _trapezoid_area_perimeter(h: float, b: float, m_left: float,
                              m_right: float) -> tuple[float, float]:
    """Flow area and wetted perimeter of a trapezoid at depth *h*."""
    area = b * h + 0.5 * (m_left + m_right) * h ** 2
    perim = b + h * math.hypot(1.0, m_left) + h * math.hypot(1.0, m_right)
    return area, perim


def _manning_discharge(h: float, n: float, slope: float, b: float,
                       m_left: float, m_right: float) -> float:
    """Manning-Strickler discharge Q [m3/s] at depth *h* (0 if h<=0)."""
    if h <= 0.0:
        return 0.0
    area, perim = _trapezoid_area_perimeter(h, b, m_left, m_right)
    if perim <= 0.0:
        return 0.0
    radius = area / perim
    return (1.0 / n) * area * radius ** (2.0 / 3.0) * math.sqrt(slope)


def _normal_depth(discharge: float, n: float, slope: float, b: float,
                  m_left: float, m_right: float, tol: float = 1e-7) -> float:
    """Normal (uniform-flow) depth [m] conveying *discharge*, by bisection."""
    if discharge <= 0.0:
        return 0.0
    hi = 1.0
    for _ in range(100):                         # grow until the section conveys Q
        if _manning_discharge(hi, n, slope, b, m_left, m_right) >= discharge:
            break
        hi *= 2.0
    else:
        raise ValueError("could not bracket a normal depth - check the geometry")
    lo = 0.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _manning_discharge(mid, n, slope, b, m_left, m_right) < discharge:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def _required_manning_n(discharge: float, h: float, slope: float, b: float,
                        m_left: float, m_right: float) -> float:
    """Manning n that makes depth *h* convey *discharge* (closed form).

    Inverting Q = (1/n) A R^(2/3) sqrt(S0) for n gives n = A R^(2/3) sqrt(S0) / Q.
    """
    area, perim = _trapezoid_area_perimeter(h, b, m_left, m_right)
    radius = area / perim
    return area * radius ** (2.0 / 3.0) * math.sqrt(slope) / discharge


def _section_figure(b: float, m_left: float, m_right: float, h: float):
    """A matplotlib figure of the trapezoidal section with the water surface at *h*."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    top = max(h * 1.4, h + 0.3, 1.0)             # bank drawing height (open top)
    fig, ax = plt.subplots(figsize=(6.2, 4.0))
    # channel bed + banks (open at the top, where the water surface is)
    ax.plot([-m_left * top, 0.0, b, b + m_right * top], [top, 0.0, 0.0, top],
            color="#6b4f37", lw=2.4, solid_capstyle="round", zorder=3)
    if h > 0.0:                                  # wetted area + water surface
        ax.fill([-m_left * h, 0.0, b, b + m_right * h], [h, 0.0, 0.0, h],
                color="#8ecae6", alpha=0.75, zorder=1)
        ax.plot([-m_left * h, b + m_right * h], [h, h],
                color="#1f78b4", lw=1.8, ls="--", zorder=4)
        xa = b / 2.0 if b > 0 else (m_right * h - m_left * h) / 2.0
        ax.annotate("", xy=(xa, h), xytext=(xa, 0.0),
                    arrowprops=dict(arrowstyle="<->", color="#1f78b4"))
        ax.text(xa + 0.05 * (b + 1.0), h / 2.0, f"h = {h:.3f} m",
                color="#1f78b4", va="center", fontsize=9)
    # bottom-width dimension
    ax.annotate("", xy=(b, -0.10 * top), xytext=(0.0, -0.10 * top),
                arrowprops=dict(arrowstyle="<->", color="#6b4f37"))
    ax.text(b / 2.0, -0.17 * top, f"b = {b:.2f} m", ha="center", va="top",
            color="#6b4f37", fontsize=9)
    # bank inclination labels (vertical:horizontal = 1:m)
    ax.text(-m_left * top * 0.55, top * 0.5, f"1:{m_left:g}", ha="right",
            va="center", color="#6b4f37", fontsize=9)
    ax.text(b + m_right * top * 0.55, top * 0.5, f"1:{m_right:g}", ha="left",
            va="center", color="#6b4f37", fontsize=9)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("width (m)")
    ax.set_ylabel("height above bed (m)")
    # explicit limits (with margin) so the bottom-width dimension stays visible;
    # annotations/text below the bed are not counted by autoscale.
    half = 0.5 * (rb + (m_left + m_right) * top) + 0.5
    xc = 0.5 * rb + 0.5 * (m_right - m_left) * top
    ax.set_xlim(xc - half - 0.5, xc + half + 0.5)
    ax.set_ylim(-0.28 * top, top * 1.05)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #

st.set_page_config(page_title="hydromate config editor", layout="wide")
st.title("hydromate - configuration editor")
st.caption("Build and validate a TELEMAC-2D (+GAIA) case configuration, then run it. "
           "See the Workflow tab for the steps after the build.")

if "cfg" not in st.session_state:
    st.session_state.cfg = copy.deepcopy(TEMPLATE)
cfg = st.session_state.cfg

# ---- sidebar: load / reset ----------------------------------------------- #
with st.sidebar:
    st.header("Load")
    up = st.file_uploader("Load an existing YAML", type=["yml", "yaml"])
    if up is not None and st.button("Apply uploaded file"):
        st.session_state.cfg = yaml.safe_load(up.getvalue().decode()) or {}
        st.rerun()
    load_path = st.text_input("…or load from a path", value="")
    if load_path and st.button("Apply path"):
        st.session_state.cfg = yaml.safe_load(Path(load_path).read_text()) or {}
        st.rerun()
    if st.button("Reset to template"):
        st.session_state.cfg = copy.deepcopy(TEMPLATE)
        st.rerun()
    st.divider()
    st.caption("Launch: `hydromate-gui` (needs `pip install -e \".[gui]\"`). "
               "Nothing leaves your machine.")

tabs = st.tabs(["Project", "TELEMAC", "Geodata", "Boundaries", "Mesh", "Friction",
                "Hydrodynamics", "Initialization", "Morphodynamics", "Calibration",
                "Rating curve", "Workflow"])

# ---- Project ------------------------------------------------------------- #
with tabs[0]:
    p = cfg.setdefault("project", {})
    c1, c2 = st.columns(2)
    p["name"] = c1.text_input("Case name", _get(cfg, "project", "name", default="case"))
    p["crs_epsg"] = int(c2.number_input(
        "CRS EPSG (metric)", value=int(_get(cfg, "project", "crs_epsg", default=25832)),
        step=1, format="%d"))
    st.caption("Produced artifacts live under these phase folders (resolved relative "
               "to the saved config).")
    p["preprocessing_dir"] = c1.text_input(
        "Preprocessing dir",
        _get(cfg, "project", "preprocessing_dir", default="hydromate-case/preprocessing"))
    p["model_dir"] = c2.text_input(
        "Simulation (model) dir",
        _get(cfg, "project", "model_dir", default="hydromate-case/simulation"))
    p["postprocessing_dir"] = c1.text_input(
        "Postprocessing dir",
        _get(cfg, "project", "postprocessing_dir", default="hydromate-case/postprocessing"))
    p["calibration_dir"] = c2.text_input(
        "Calibration-validation dir",
        _get(cfg, "project", "calibration_dir", default="hydromate-case/calibration-validation"))

# ---- TELEMAC ------------------------------------------------------------- #
with tabs[1]:
    t = cfg.setdefault("telemac", {})
    t["pysource"] = st.text_input(
        "pysource.*.sh (TELEMAC environment script) - sourced, not imported",
        _get(cfg, "telemac", "pysource", default=""))
    c1, c2 = st.columns(2)
    sol = _get(cfg, "telemac", "solver", default="telemac2d")
    t["solver"] = c1.selectbox("Solver", SOLVERS,
                               index=SOLVERS.index(sol) if sol in SOLVERS else 0,
                               help="The build is 2D; the 3D case is written later by "
                                    "add3d.py (see Workflow).")
    t["n_processors"] = int(c2.number_input(
        "Processors", value=int(_get(cfg, "telemac", "n_processors", default=4)),
        min_value=1, step=1))

# ---- Geodata ------------------------------------------------------------- #
with tabs[2]:
    gd = cfg.setdefault("geodata", {})
    st.markdown("**Required** (paths are resolved relative to the saved config file)")
    for key in ("dem_initial", "boundary"):
        gd[key] = st.text_input(key, _get(cfg, "geodata", key, default=""))
    st.markdown("**Optional** (leave blank to omit). `mesh_zones` + "
                "`channel_centerline` enable the anisotropic flow-aligned mesh; "
                "`roughness_zones` + `roughness_table` drive zonal friction.")
    for key in OPTIONAL_GEODATA:
        val = st.text_input(key, _get(cfg, "geodata", key, default="") or "")
        if val:
            gd[key] = val
        else:
            gd.pop(key, None)

    st.markdown("**Ground truth (calibration targets)**")
    gt = cfg.setdefault("ground_truth", {})
    meas = st.text_input(
        "measurements (tidy multi-tab table, xlsx/csv)",
        _get(cfg, "ground_truth", "measurements", default="") or "",
        help="A user-authored tidy table, OR the output path for the table compiled "
             "from the raw sources below.")
    if meas:
        gt["measurements"] = meas
    else:
        gt.pop("measurements", None)
    with st.expander("Ground-truth sources (compiled into the calibration table)"):
        st.caption("Raw sources joined by `join_key` into the tidy measurements "
                   "table. Leave empty if you provide `measurements` directly.")
        srcs = _get(cfg, "ground_truth", "sources", default=[]) or []
        gt_edit = st.data_editor(
            srcs, num_rows="dynamic", key="ground_truth_sources",
            column_config={
                "category": st.column_config.SelectboxColumn(
                    "category", options=["hydraulics", "sediment"]),
                "kind": st.column_config.SelectboxColumn(
                    "kind", options=["flowtracker", "points"]),
                "values": st.column_config.TextColumn("values (xlsx/csv)"),
                "positions": st.column_config.TextColumn("positions (shp/gpkg)"),
                "join_key": st.column_config.TextColumn("join_key"),
            })
        cleaned = [dict(r) for r in gt_edit if r.get("category")]
        if cleaned:
            gt["sources"] = cleaned
        else:
            gt.pop("sources", None)

# ---- Boundaries ---------------------------------------------------------- #
with tabs[3]:
    b = cfg.setdefault("boundaries", {})
    b["liquid_boundaries"] = st.text_input(
        "liquid_boundaries (inflow/outflow LINE layer, shp/gpkg) [required for a build]",
        _get(cfg, "boundaries", "liquid_boundaries", default=""))
    inflow = st.text_input(
        "inflow (discharge time series csv) - OPTIONAL, drives a later unsteady case",
        _get(cfg, "boundaries", "inflow", default="") or "",
        help="The steady initial run uses the scalar prescribed_flowrate below; the "
             "inflow series is only needed for a varying-hydrograph unsteady run.")
    if inflow:
        b["inflow"] = inflow
    else:
        b.pop("inflow", None)
    st.markdown("**Prescribed values (drive the steady initial run)**")
    c1, c2 = st.columns(2)
    with c1:
        q = _opt_float("Prescribed inflow Q (m³/s) - blank = mean of inflow series",
                       _get(cfg, "boundaries", "prescribed_flowrate", default=""),
                       fmt="%.3f")
        if q is not None:
            b["prescribed_flowrate"] = q
        else:
            b.pop("prescribed_flowrate", None)
    oc = _get(cfg, "boundaries", "outflow_condition", default="elevation")
    b["outflow_condition"] = c2.selectbox(
        "Outflow condition", OUTFLOW_CONDITIONS,
        index=OUTFLOW_CONDITIONS.index(oc) if oc in OUTFLOW_CONDITIONS else 0,
        help="elevation: fixed WSE from prescribed_elevation (default); "
             "stage_discharge: WSE from the rating curve; free: Neumann outflow.")
    if b["outflow_condition"] == "elevation":
        b["prescribed_elevation"] = st.number_input(
            "Downstream prescribed elevation (m a.s.l.)",
            value=float(_get(cfg, "boundaries", "prescribed_elevation", default=0.0)),
            format="%.3f")
    else:
        b.pop("prescribed_elevation", None)
    if b["outflow_condition"] == "stage_discharge":
        sd = st.text_input(
            "stage_discharge (outlet rating curve csv Q,WSE) - blank = synthesised",
            _get(cfg, "boundaries", "stage_discharge", default="") or "")
        if sd:
            b["stage_discharge"] = sd
        else:
            b.pop("stage_discharge", None)
    else:
        b.pop("stage_discharge", None)

# ---- Mesh ---------------------------------------------------------------- #
with tabs[4]:
    m = cfg.setdefault("mesh", {})
    st.markdown("**Anisotropic, flow-aligned strategy** (used when `geodata.mesh_zones` "
                "+ `geodata.channel_centerline` are set). Edge lengths are read per "
                "polygon from the mesh-zones layer; these are the fallbacks.")
    c1, c2, c3 = st.columns(3)
    m["channel_size"] = c1.number_input(
        "Channel edge length (m)", value=float(_get(cfg, "mesh", "channel_size", default=0.5)),
        format="%.3f", help="Cross-channel cell size; elongated x anisotropy along flow.")
    m["floodplain_size"] = c2.number_input(
        "Floodplain edge length (m)",
        value=float(_get(cfg, "mesh", "floodplain_size", default=1.5)), format="%.3f")
    m["refinement_size"] = c3.number_input(
        "Refinement edge length (m)",
        value=float(_get(cfg, "mesh", "refinement_size", default=0.5)), format="%.3f")
    m["growth_ratio"] = c1.number_input(
        "Growth ratio", value=float(_get(cfg, "mesh", "growth_ratio", default=1.2)),
        format="%.2f", help="Max edge-size growth per element (channel -> floodplain).")
    m["channel_anisotropy"] = c2.number_input(
        "Channel anisotropy", value=float(_get(cfg, "mesh", "channel_anisotropy", default=4.0)),
        format="%.1f", help="Along-flow / cross-flow edge ratio in the channel.")
    m["max_aspect_ratio"] = c3.number_input(
        "Max aspect ratio", value=float(_get(cfg, "mesh", "max_aspect_ratio", default=4.0)),
        format="%.1f", help="Hard cap on cell aspect ratio (post-mesh edge-flip pass).")

    with st.expander("Quality-report thresholds (warnings only; channel exempt)"):
        q1, q2, q3 = st.columns(3)
        m["min_angle_deg"] = q1.number_input(
            "Min angle (deg)", value=float(_get(cfg, "mesh", "min_angle_deg", default=30.0)))
        m["max_angle_deg"] = q2.number_input(
            "Max angle (deg)", value=float(_get(cfg, "mesh", "max_angle_deg", default=90.0)))
        m["max_area_jump"] = q3.number_input(
            "Max area jump (frac)",
            value=float(_get(cfg, "mesh", "max_area_jump", default=0.20)), format="%.2f")

    with st.expander("Isotropic fallback (no mesh_zones/centerline)"):
        f1, f2, f3 = st.columns(3)
        m["default_size"] = f1.number_input(
            "Default edge length (m)",
            value=float(_get(cfg, "mesh", "default_size", default=10.0)))
        m["breakline_size"] = f2.number_input(
            "Breakline edge length (m)",
            value=float(_get(cfg, "mesh", "breakline_size", default=3.0)))
        m["min_size"] = f3.number_input(
            "Min size (m)", value=float(_get(cfg, "mesh", "min_size", default=0.5)))
        st.caption("Per-MATID refinement (optional)")
        rs = _get(cfg, "mesh", "region_sizes", default={}) or {}
        rs_rows = [{"matid": int(k), "size": float(v)} for k, v in rs.items()]
        edited = st.data_editor(
            rs_rows, num_rows="dynamic", key="region_sizes",
            column_config={"matid": st.column_config.NumberColumn("MATID", step=1),
                           "size": st.column_config.NumberColumn("size (m)")})
        m["region_sizes"] = {int(r["matid"]): float(r["size"]) for r in edited
                             if r.get("matid") not in (None, "")}

# ---- Friction ------------------------------------------------------------ #
with tabs[5]:
    f = cfg.setdefault("friction", {})
    law_names = [f"{k} {v}" for k, v in LAWS.items()]

    def _law_select(col, label, current):
        return list(LAWS)[col.selectbox(
            label, range(len(LAWS)),
            index=list(LAWS).index(current) if current in LAWS else 4,
            format_func=lambda i: law_names[i])]

    c1, c2 = st.columns(2)
    f["default_law"] = _law_select(c1, "Default friction law",
                                   int(_get(cfg, "friction", "default_law", default=4)))
    f["default_coefficient"] = c2.number_input(
        "Default coefficient",
        value=float(_get(cfg, "friction", "default_coefficient", default=0.03)), format="%.4f")
    f["roughness_law"] = _law_select(
        c1, "Roughness-zone law (for roughness_table rows)",
        int(_get(cfg, "friction", "roughness_law", default=5)))
    st.markdown("**Lateral (wall) boundary friction**")
    b1, b2 = st.columns(2)
    f["boundary_law"] = _law_select(b1, "Boundary law",
                                    int(_get(cfg, "friction", "boundary_law", default=4)))
    f["boundary_coefficient"] = b2.number_input(
        "Boundary coefficient",
        value=float(_get(cfg, "friction", "boundary_coefficient", default=0.03)), format="%.4f")
    st.markdown("Friction zones (one row per MATID; calibration perturbs the coefficient)")
    zones = _get(cfg, "friction", "zones", default=[]) or []
    z_edit = st.data_editor(
        zones, num_rows="dynamic", key="zones",
        column_config={
            "matid": st.column_config.NumberColumn("MATID", step=1),
            "name": st.column_config.TextColumn("name"),
            "law": st.column_config.NumberColumn("law", step=1),
            "coefficient": st.column_config.NumberColumn("coefficient", format="%.4f"),
        })
    f["zones"] = [dict(r) for r in z_edit if r.get("matid") not in (None, "")]

# ---- Hydrodynamics ------------------------------------------------------- #
with tabs[6]:
    h = cfg.setdefault("hydrodynamics", {})
    st.markdown("**Numerics & turbulence**")
    c1, c2 = st.columns(2)
    h["finite_volumes"] = c1.checkbox(
        "Use finite volumes (HLLC) instead of finite elements",
        value=bool(_get(cfg, "hydrodynamics", "finite_volumes", default=False)),
        help="Finite elements are the default; finite volumes accept only "
             "turbulence model 1 (constant viscosity).")
    tm = _get(cfg, "hydrodynamics", "turbulence_model", default="auto")
    try:
        tm = int(tm)
    except (TypeError, ValueError):
        tm = "auto"
    h["turbulence_model"] = c2.selectbox(
        "Turbulence model", TURB_OPTIONS,
        index=TURB_OPTIONS.index(tm) if tm in TURB_OPTIONS else 0,
        format_func=lambda v: TURB_LABELS[v])
    c1, c2 = st.columns(2)
    h["initial_velocity_guess"] = c1.number_input(
        "Initial velocity guess U (m/s)",
        value=float(_get(cfg, "hydrodynamics", "initial_velocity_guess", default=1.0)),
        format="%.2f", help="Assumed mean flow; sizes the eddy viscosity and feeds "
                            "the turbulence-model auto-selection.")
    with c2:
        tls = _opt_float("Turbulence length scale (m) - blank = flow depth",
                         _get(cfg, "hydrodynamics", "turbulence_length_scale", default=""))
        if tls is not None:
            h["turbulence_length_scale"] = tls
        else:
            h.pop("turbulence_length_scale", None)
        vdiff = _opt_float("Velocity diffusivity (m²/s) - blank = auto for model 1",
                           _get(cfg, "hydrodynamics", "velocity_diffusivity", default=""))
        if vdiff is not None:
            h["velocity_diffusivity"] = vdiff
        else:
            h.pop("velocity_diffusivity", None)

    st.markdown("**Time stepping**")
    c1, c2, c3 = st.columns(3)
    h["time_step"] = c1.number_input(
        "Time step (s)", value=float(_get(cfg, "hydrodynamics", "time_step", default=1.0)),
        help="Initial/max step; CFL-adapted when variable time step is on.")
    h["n_time_steps"] = int(c2.number_input(
        "Number of time steps",
        value=int(_get(cfg, "hydrodynamics", "n_time_steps", default=15000)), step=100))
    h["desired_courant"] = c3.number_input(
        "Desired Courant number",
        value=float(_get(cfg, "hydrodynamics", "desired_courant", default=0.6)), format="%.2f")
    h["variable_timestep"] = c1.checkbox(
        "Variable time step (CFL-adaptive)",
        value=bool(_get(cfg, "hydrodynamics", "variable_timestep", default=True)))
    h["graphic_printout_period"] = int(c2.number_input(
        "Graphic printout period",
        value=int(_get(cfg, "hydrodynamics", "graphic_printout_period", default=500)), step=50))
    h["listing_printout_period"] = int(c3.number_input(
        "Listing printout period",
        value=int(_get(cfg, "hydrodynamics", "listing_printout_period", default=500)), step=50))
    s1, s2 = st.columns(2)
    h["stop_if_steady"] = s1.checkbox(
        "Auto-stop the steady run at steady state",
        value=bool(_get(cfg, "hydrodynamics", "stop_if_steady", default=True)),
        help="STOP IF A STEADY STATE IS REACHED - halts when the solution stops "
             "changing instead of running all time steps (steady run only).")
    h["stop_criteria"] = s2.text_input(
        "Stop criteria (U,V; H; tracers)",
        _get(cfg, "hydrodynamics", "stop_criteria", default="1.E-4;1.E-4;1.E-4"))

    reg = _get(cfg, "hydrodynamics", "regime", default="steady")
    h["regime"] = st.selectbox(
        "Regime", ["steady", "unsteady"], index=0 if reg == "steady" else 1,
        help="The initial run (steady2d.cas) is ALWAYS steady; when boundaries.inflow "
             "is a varying hydrograph an additional unsteady2d.cas is written too.")

# ---- Initialization ------------------------------------------------------ #
with tabs[7]:
    ini = cfg.setdefault("initialization", {})
    st.caption("Initial condition: the run is a **dry start** by default - only a thin "
               "plug at the inflow is wetted so the prescribed-Q boundary can establish "
               "(a fully dry bed makes DEBIMP abort), dry elsewhere.")
    c1, c2, c3 = st.columns(3)
    ini["dry_start_depth"] = c1.number_input(
        "Dry-start plug depth (m)",
        value=float(_get(cfg, "initialization", "dry_start_depth", default=0.5)),
        format="%.2f", help="Inflow-plug seed depth; keep >= the normal depth so "
                           "the inflow stays subcritical.")
    with c2:
        ext = _opt_float("Dry-start plug extent (m) - blank = ~5 cells",
                         _get(cfg, "initialization", "dry_start_extent", default=""), fmt="%.1f")
        if ext is not None:
            ini["dry_start_extent"] = ext
        else:
            ini.pop("dry_start_extent", None)
    with c3:
        pw = _opt_float("Pre-wet depth (m) - hotstart the whole channel instead; "
                        "blank = dry start",
                        _get(cfg, "initialization", "prewet_depth", default=""), fmt="%.2f")
        if pw is not None:
            ini["prewet_depth"] = pw
        else:
            ini.pop("prewet_depth", None)

# ---- Morphodynamics ------------------------------------------------------ #
with tabs[8]:
    mo = cfg.setdefault("morphodynamics", {})
    mo["enabled"] = st.checkbox(
        "Enable GAIA morphodynamics (sediment transport / bed change)",
        value=bool(_get(cfg, "morphodynamics", "enabled", default=False)),
        help="Off by default (v1 focuses on 2D hydraulics). Needs geodata.dem_target "
             "or dem_of_difference for the topographic-change calibration.")
    if mo["enabled"]:
        mo["gaia_results_base"] = st.text_input(
            "GAIA results base name",
            _get(cfg, "morphodynamics", "gaia_results_base", default="rgaia"))
        st.caption("Sediment classes (diameter [m], density [kg/m³], Shields parameter)")
        classes = _get(cfg, "morphodynamics", "sediment_classes", default=[]) or []
        sc_edit = st.data_editor(
            classes or [{"diameter": 0.001, "density": 2650, "shields": 0.047}],
            num_rows="dynamic", key="sediment_classes",
            column_config={
                "diameter": st.column_config.NumberColumn("diameter (m)", format="%.5f"),
                "density": st.column_config.NumberColumn("density (kg/m³)", step=10),
                "shields": st.column_config.NumberColumn("shields", format="%.4f"),
            })
        mo["sediment_classes"] = [dict(r) for r in sc_edit if r.get("diameter") not in (None, "")]

    st.markdown("**DEM-of-Difference (DoD)**")
    dod = cfg.setdefault("dem_of_difference", {})
    dod["enabled"] = st.checkbox(
        "Compute a DoD (dem_target - dem_initial) in pre-processing",
        value=bool(_get(cfg, "dem_of_difference", "enabled", default=False)),
        help="Bed-change raster on the ROI-clipped grid, thresholded by a minimum "
             "level of detection. Needs geodata.dem_target (or a precomputed "
             "geodata.dem_of_difference). Independent of GAIA above.")
    if dod["enabled"]:
        st.caption("Level of detection - propagated survey uncertainty "
                   "LoD = t·√(u_i² + u_t²), or a fixed min_lod override.")
        d1, d2, d3 = st.columns(3)
        with d1:
            ui = _opt_float("dem_initial 1σ error (m) - blank to omit",
                            _get(cfg, "dem_of_difference", "uncertainty_initial", default=""),
                            fmt="%.3f")
            if ui is not None:
                dod["uncertainty_initial"] = ui
            else:
                dod.pop("uncertainty_initial", None)
        with d2:
            ut = _opt_float("dem_target 1σ error (m) - blank to omit",
                            _get(cfg, "dem_of_difference", "uncertainty_target", default=""),
                            fmt="%.3f")
            if ut is not None:
                dod["uncertainty_target"] = ut
            else:
                dod.pop("uncertainty_target", None)
        dod["confidence_level"] = d3.number_input(
            "Confidence level", min_value=0.50, max_value=0.999,
            value=float(_get(cfg, "dem_of_difference", "confidence_level", default=0.95)),
            format="%.2f")
        m1, m2 = st.columns(2)
        with m1:
            ml = _opt_float("Explicit min_lod (m) - overrides propagated; blank to omit",
                            _get(cfg, "dem_of_difference", "min_lod", default=""), fmt="%.3f")
            if ml is not None:
                dod["min_lod"] = ml
            else:
                dod.pop("min_lod", None)
        dod["mask_below_lod"] = m2.checkbox(
            "Mask sub-LoD changes to nodata (else set to 0)",
            value=bool(_get(cfg, "dem_of_difference", "mask_below_lod", default=True)))

# ---- Calibration --------------------------------------------------------- #
with tabs[9]:
    cal = cfg.setdefault("calibration", {})
    cal["calibration_quantities"] = st.multiselect(
        "Calibration quantities (measured)", QUANTITIES,
        default=_get(cfg, "calibration", "calibration_quantities", default=["WATER DEPTH"]))
    cal["extraction_quantities"] = st.multiselect(
        "Extraction quantities (from results)", QUANTITIES,
        default=_get(cfg, "calibration", "extraction_quantities",
                     default=["WATER DEPTH", "SCALAR VELOCITY"]))
    c1, c2, c3 = st.columns(3)
    cal["measurement_error"] = c1.number_input(
        "Measurement error (fraction)",
        value=float(_get(cfg, "calibration", "measurement_error", default=0.10)), format="%.2f")
    cal["init_runs"] = int(c2.number_input(
        "Initial runs", value=int(_get(cfg, "calibration", "init_runs", default=30)), step=1))
    cal["max_runs"] = int(c3.number_input(
        "Max runs", value=int(_get(cfg, "calibration", "max_runs", default=50)), step=1))
    c1, c2 = st.columns(2)
    samp = _get(cfg, "calibration", "parameter_sampling_method", default="sobol")
    cal["parameter_sampling_method"] = c1.selectbox(
        "Parameter sampling method", SAMPLING,
        index=SAMPLING.index(samp) if samp in SAMPLING else 0)
    cal["gp_library"] = c2.text_input(
        "GP library", _get(cfg, "calibration", "gp_library", default="gpy"))
    st.markdown("Calibration parameters (HydroBayesCal naming: `zone<N>` friction, "
                "`gaiaCLASSES SHIELDS PARAMETERS <n>`, …)")
    params = _get(cfg, "calibration", "parameters", default=[]) or []
    p_edit = st.data_editor(
        params, num_rows="dynamic", key="params",
        column_config={
            "name": st.column_config.TextColumn("name"),
            "min": st.column_config.NumberColumn("min", format="%.4f"),
            "max": st.column_config.NumberColumn("max", format="%.4f"),
            "comment": st.column_config.TextColumn("comment"),
        })
    cal["parameters"] = [dict(r) for r in p_edit if r.get("name")]

# ---- Rating curve (trapezoidal stage-discharge calculator) --------------- #
with tabs[10]:
    st.markdown("**Trapezoidal stage-discharge calculator** (Manning-Strickler "
                "normal flow). Size a downstream rating curve, or back out the "
                "roughness a measured stage implies.")
    st.caption("Standalone tool - it does **not** edit the config. Same normal-flow "
               "physics as `hydromate rating` / `synthesize_outflow_rating`, extended "
               "here to independent left/right bank slopes. Side slope `m` is the "
               "bank inclination 1:m (1 vertical : m horizontal; m = 0 is vertical).")

    geo, out = st.columns([1, 1])
    with geo:
        st.markdown("**Cross-section geometry**")
        rb = st.number_input("Bottom width b (m)", min_value=0.0, value=10.0,
                             step=0.5, format="%.3f")
        gcl, gcr = st.columns(2)
        ml = gcl.number_input("Left bank slope m_L (1:m)", min_value=0.0, value=2.0,
                             step=0.25, format="%.3f")
        mr = gcr.number_input("Right bank slope m_R (1:m)", min_value=0.0, value=2.0,
                             step=0.25, format="%.3f")
        s0 = st.number_input("Bed slope S0 (m/m)", min_value=1e-6, value=1e-3,
                            step=5e-4, format="%.5f",
                            help="Longitudinal bed slope for the normal-flow equation.")
        bed = st.number_input("Bed elevation (m a.s.l., for WSE)", value=0.0,
                             format="%.3f", help="WSE = bed elevation + depth h.")

    mode = st.radio(
        "Solve for", ["Depth h from discharge Q", "Roughness from depth h and Q"],
        horizontal=True,
        help="Forward: geometry + roughness + Q -> normal depth h. "
             "Inverse: geometry + measured h + Q -> the Manning n / Strickler Kst "
             "that reproduces it.")

    degenerate = rb <= 0.0 and ml <= 0.0 and mr <= 0.0
    depth = req_n = area = None
    with out:
        st.markdown("**Result**")
        if degenerate:
            st.warning("Zero-area section: set a bottom width or a bank slope > 0.")
        elif mode.startswith("Depth"):
            q = st.number_input("Discharge Q (m³/s)", min_value=0.0, value=50.0,
                               step=1.0, format="%.3f", key="rating_q_fwd")
            rough_kind = st.radio("Roughness given as", ["Manning n", "Strickler Kst"],
                                  horizontal=True, key="rating_rough_fwd")
            if rough_kind == "Manning n":
                nval = st.number_input("Manning n (s/m^{1/3})", min_value=1e-4,
                                      value=0.035, step=0.005, format="%.4f")
            else:
                kst = st.number_input("Strickler Kst (m^{1/3}/s)", min_value=1e-4,
                                     value=30.0, step=1.0, format="%.2f")
                nval = 1.0 / kst
            if q > 0.0:
                try:
                    depth = _normal_depth(q, nval, s0, rb, ml, mr)
                except ValueError as exc:
                    st.error(str(exc))
        else:
            q = st.number_input("Discharge Q (m³/s)", min_value=1e-6, value=50.0,
                               step=1.0, format="%.3f", key="rating_q_inv")
            h_meas = st.number_input("Measured water depth h (m)", min_value=1e-4,
                                    value=1.5, step=0.1, format="%.3f")
            depth = h_meas
            req_n = _required_manning_n(q, h_meas, s0, rb, ml, mr)

        if depth is not None and depth > 0.0:
            area, perim = _trapezoid_area_perimeter(depth, rb, ml, mr)
            top_w = rb + (ml + mr) * depth            # free-surface width
            vel = q / area if area > 0 else 0.0
            hyd_depth = area / top_w if top_w > 0 else 0.0
            froude = vel / math.sqrt(9.81 * hyd_depth) if hyd_depth > 0 else 0.0
            if mode.startswith("Depth"):
                st.metric("Normal depth h", f"{depth:.3f} m")
                st.metric("Water-surface elevation", f"{bed + depth:.3f} m a.s.l.")
            else:
                st.metric("Required Manning n", f"{req_n:.4f} s/m^(1/3)")
                st.metric("Required Strickler Kst", f"{1.0 / req_n:.2f} m^(1/3)/s")
            st.caption(
                f"A = {area:.3f} m² · top width = {top_w:.3f} m · "
                f"mean velocity = {vel:.3f} m/s · "
                f"Froude = {froude:.3f} ({'sub' if froude < 1 else 'super'}critical)")

    if not degenerate and depth is not None:
        st.pyplot(_section_figure(rb, ml, mr, max(depth, 0.0)))

    if not degenerate and mode.startswith("Depth"):
        with st.expander("Tabulate a Q–h rating curve (and download CSV)"):
            cc1, cc2, cc3 = st.columns(3)
            q_min = cc1.number_input("Q min (m³/s)", min_value=0.0, value=5.0,
                                    step=1.0, format="%.2f")
            q_max = cc2.number_input("Q max (m³/s)", min_value=0.0, value=100.0,
                                    step=1.0, format="%.2f")
            q_steps = int(cc3.number_input("Points", min_value=2, value=15, step=1))
            if q_max > q_min:
                import pandas as pd
                qs = [q_min + (q_max - q_min) * i / (q_steps - 1)
                      for i in range(q_steps)]
                rows = []
                for qq in qs:
                    hq = _normal_depth(qq, nval, s0, rb, ml, mr)
                    rows.append((qq, bed + hq, hq))
                df = pd.DataFrame(rows, columns=["Q", "WSE", "depth"])
                st.line_chart(df.set_index("Q")["depth"], y_label="depth h (m)")
                st.download_button(
                    "Download rating CSV (Q,WSE,depth)",
                    df.to_csv(index=False, float_format="%.4f"),
                    file_name="rating-curve.csv", mime="text/csv")
                st.caption("Same `Q,WSE,depth` schema as `boundaries.stage_discharge`.")
            else:
                st.info("Set Q max greater than Q min to tabulate.")

# ---- Workflow (info) ----------------------------------------------------- #
with tabs[11]:
    st.markdown(
        """
This editor produces the **case configuration** only. After saving (and optionally
**Build**ing) below, run the rest of the workflow from the per-case scripts in
`cases/<your-case>/` (`mamba run -n hydromate-env python cases/<case>/<script>.py`):

**2D workflow (run in order)**
1. **`preprocessing.py`** - build the full TELEMAC-2D case into `simulation/`
   (mesh, `.cli`, `.tbl`, `steady2d.cas`; synthesises the inflow + rating if missing).
   *Equivalent to the **Build case** button here (`hydromate <config>`).*
1b. **`initial_run.py`** - test-run that `steady2d.cas` once (produces the hotstart
   result `r2d.slf`); ends preprocessing.
2. **`mesh_convergence_study.py`** - horizontal grid-independence study + xlsx report.
3. **`run_Bayes_cal.py`** - HydroBayesCal calibration against the ground truth.

**Optional 3D extension (only after the 2D path)** - a 3D run needs the 2D hotstart
(`r2d.slf`) and reuses the horizontal mesh the mesh-convergence study validated:
- **`add3d.py`** - write a non-hydrostatic TELEMAC-3D case hotstarted from the 2D
  result (sigma layers + turbulence inferred; Courant 0.6). `--run` also launches it.
- **`vertical_convergence_3d.py`** - a *separate* grid-independence study over the
  number of **vertical (sigma) layers** (`dz`), the 3D analogue of step 2.

The **Numerics** here apply to the 2D case; the 3D case is generated by `add3d.py`
and infers its own settings from the 2D result.
        """)

st.session_state.cfg = cfg

# ---- output: preview / save / run ---------------------------------------- #
st.divider()
left, right = st.columns([3, 2])

with left:
    st.subheader("Generated YAML")
    yaml_text = build_yaml(cfg)
    st.code(yaml_text, language="yaml")
    st.download_button("Download YAML", yaml_text,
                       file_name=f"{_get(cfg, 'project', 'name', default='case')}.yml")

with right:
    st.subheader("Save & run")
    save_path = st.text_input("Save to path", value="cases/my-case/case-config-edited.yml")
    if st.button("💾 Save YAML"):
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_yaml(cfg))
        st.success(f"Saved to {path}")

    st.caption("Validate / build the saved file (paths resolve relative to it). "
               "Build = workflow step 1; run the later steps from the case scripts.")
    if st.button("✅ Validate (--check)"):
        if not Path(save_path).exists():
            st.warning("Save the YAML first.")
        else:
            rc, out = run_cli([save_path, "--check"])
            (st.success if rc == 0 else st.error)(f"exit code {rc}")
            st.code(out or "(no output)")

    if st.button("🚀 Build case"):
        if not Path(save_path).exists():
            st.warning("Save the YAML first.")
        else:
            with st.spinner("Running hydromate… (meshing can take a while)"):
                rc, out = run_cli([save_path, "-v"])
            (st.success if rc == 0 else st.error)(f"exit code {rc}")
            st.code(out or "(no output)")
