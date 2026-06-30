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
OUTFLOW_CONDITIONS = ["stage_discharge", "elevation", "free"]
QUANTITIES = [
    "WATER DEPTH", "SCALAR VELOCITY", "VELOCITY U", "VELOCITY V",
    "TURBULENT ENERG", "CUMUL BED EVOL",
]
SAMPLING = ["sobol", "random", "latin_hypercube"]
# optional input paths (blank = omit); the anisotropic-meshing + roughness layers
# come first as they drive the default (flow-aligned) mesh and zonal friction.
OPTIONAL_INPUTS = [
    "dem_target", "mesh_zones", "channel_centerline", "roughness_zones",
    "roughness_table", "breaklines", "region_points", "region_table",
    "stage_discharge", "measurements", "dem_of_difference",
]

TEMPLATE: dict = {
    "project": {"name": "case", "crs_epsg": 25832,
                "preprocessing_dir": "hydromate-case/preprocessing",
                "model_dir": "hydromate-case/simulation",
                "postprocessing_dir": "hydromate-case/postprocessing",
                "calibration_dir": "hydromate-case/calibration-validation"},
    "telemac": {"pysource": "/path/to/telemac/configs/pysource.sh",
                "solver": "telemac2d", "n_processors": 4},
    "inputs": {"dem_initial": "path/to/dem.tif", "boundary": "path/to/roi.gpkg",
               "liquid_boundaries": "path/to/liquid-boundaries.gpkg",
               "inflow": "path/to/inflow.csv"},
    "mesh": {"channel_size": 0.5, "floodplain_size": 1.5, "refinement_size": 0.5,
             "growth_ratio": 1.2, "channel_anisotropy": 4.0, "max_aspect_ratio": 4.0},
    "friction": {"default_law": 4, "default_coefficient": 0.03, "roughness_law": 5,
                 "boundary_law": 4, "boundary_coefficient": 0.03, "zones": []},
    "hydrodynamics": {"regime": "steady", "finite_volumes": False,
                      "turbulence_model": "auto", "initial_velocity_guess": 1.0,
                      "outflow_condition": "stage_discharge", "time_step": 1.0,
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
    out["inputs"] = {k: v for k, v in out.get("inputs", {}).items()
                     if v not in (None, "", "path/to/")}
    if not out.get("inputs", {}).get("ground_truth"):
        out.get("inputs", {}).pop("ground_truth", None)
    h = out.get("hydrodynamics", {})
    # drop blank optional hydrodynamics knobs
    for key in ("prescribed_flowrate", "prescribed_elevation", "prewet_depth",
                "turbulence_length_scale", "velocity_diffusivity"):
        if h.get(key) in (None, ""):
            h.pop(key, None)
    # prescribed_elevation only matters for the 'elevation' outflow condition
    if h.get("outflow_condition") != "elevation":
        h.pop("prescribed_elevation", None)
    for sec, key in (("mesh", "region_sizes"), ("friction", "zones")):
        if not out.get(sec, {}).get(key):
            out.get(sec, {}).pop(key, None)
    if not out.get("morphodynamics", {}).get("enabled"):
        out.pop("morphodynamics", None)
    return yaml.safe_dump(out, sort_keys=False, default_flow_style=False, allow_unicode=True)


def run_cli(args: list[str]) -> tuple[int, str]:
    """Run the hydromate CLI and capture combined output."""
    proc = subprocess.run(["hydromate", *args], text=True, capture_output=True)
    return proc.returncode, (proc.stdout + proc.stderr)


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

tabs = st.tabs(["Project", "TELEMAC", "Inputs", "Mesh", "Friction",
                "Hydrodynamics", "Morphodynamics", "Calibration", "Workflow"])

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

# ---- Inputs -------------------------------------------------------------- #
with tabs[2]:
    inp = cfg.setdefault("inputs", {})
    st.markdown("**Required** (paths are resolved relative to the saved config file)")
    for key in ("dem_initial", "boundary", "liquid_boundaries", "inflow"):
        inp[key] = st.text_input(key, _get(cfg, "inputs", key, default=""))
    st.markdown("**Optional** (leave blank to omit). `mesh_zones` + "
                "`channel_centerline` enable the anisotropic flow-aligned mesh; "
                "`roughness_zones` + `roughness_table` drive zonal friction.")
    for key in OPTIONAL_INPUTS:
        val = st.text_input(key, _get(cfg, "inputs", key, default="") or "")
        if val:
            inp[key] = val
        else:
            inp.pop(key, None)

    with st.expander("Ground-truth sources (compiled into the calibration table)"):
        st.caption("Raw sources joined by `join_key` into the tidy measurements "
                   "table. Leave empty if you provide `measurements` directly.")
        gt = _get(cfg, "inputs", "ground_truth", default=[]) or []
        gt_edit = st.data_editor(
            gt, num_rows="dynamic", key="ground_truth",
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
            inp["ground_truth"] = cleaned
        else:
            inp.pop("ground_truth", None)

# ---- Mesh ---------------------------------------------------------------- #
with tabs[3]:
    m = cfg.setdefault("mesh", {})
    st.markdown("**Anisotropic, flow-aligned strategy** (used when `inputs.mesh_zones` "
                "+ `inputs.channel_centerline` are set). Edge lengths are read per "
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
with tabs[4]:
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
with tabs[5]:
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

    st.markdown("**Boundary & initial conditions**")
    c1, c2 = st.columns(2)
    oc = _get(cfg, "hydrodynamics", "outflow_condition", default="stage_discharge")
    h["outflow_condition"] = c1.selectbox(
        "Outflow condition", OUTFLOW_CONDITIONS,
        index=OUTFLOW_CONDITIONS.index(oc) if oc in OUTFLOW_CONDITIONS else 0,
        help="stage_discharge: WSE from the rating curve; elevation: fixed WSE; "
             "free: Neumann outflow.")
    with c2:
        if h["outflow_condition"] == "elevation":
            h["prescribed_elevation"] = st.number_input(
                "Downstream prescribed elevation (m a.s.l.)",
                value=float(_get(cfg, "hydrodynamics", "prescribed_elevation", default=0.0)),
                format="%.3f")
        else:
            h.pop("prescribed_elevation", None)
        q = _opt_float("Prescribed inflow Q (m³/s) - blank = mean of inflow series",
                       _get(cfg, "hydrodynamics", "prescribed_flowrate", default=""),
                       fmt="%.3f")
        if q is not None:
            h["prescribed_flowrate"] = q
        else:
            h.pop("prescribed_flowrate", None)
    st.caption("Initial condition: the run is a **dry start** by default - only a thin "
               "plug at the inflow is wetted so the prescribed-Q boundary can establish "
               "(a fully dry bed makes DEBIMP abort), dry elsewhere.")
    c1, c2, c3 = st.columns(3)
    with c1:
        h["dry_start_depth"] = c1.number_input(
            "Dry-start plug depth (m)",
            value=float(_get(cfg, "hydrodynamics", "dry_start_depth", default=0.5)),
            format="%.2f", help="Inflow-plug seed depth; keep >= the normal depth so "
                               "the inflow stays subcritical.")
    with c2:
        ext = _opt_float("Dry-start plug extent (m) - blank = ~5 cells",
                         _get(cfg, "hydrodynamics", "dry_start_extent", default=""), fmt="%.1f")
        if ext is not None:
            h["dry_start_extent"] = ext
        else:
            h.pop("dry_start_extent", None)
    with c3:
        pw = _opt_float("Pre-wet depth (m) - warm-start the whole channel instead; "
                        "blank = dry start",
                        _get(cfg, "hydrodynamics", "prewet_depth", default=""), fmt="%.2f")
        if pw is not None:
            h["prewet_depth"] = pw
        else:
            h.pop("prewet_depth", None)
    reg = _get(cfg, "hydrodynamics", "regime", default="steady")
    h["regime"] = st.selectbox(
        "Regime", ["steady", "unsteady"], index=0 if reg == "steady" else 1,
        help="The initial run (steady2d.cas) is ALWAYS steady; when inputs.inflow is "
             "a varying hydrograph an additional unsteady2d.cas is written too.")

# ---- Morphodynamics ------------------------------------------------------ #
with tabs[6]:
    mo = cfg.setdefault("morphodynamics", {})
    mo["enabled"] = st.checkbox(
        "Enable GAIA morphodynamics (sediment transport / bed change)",
        value=bool(_get(cfg, "morphodynamics", "enabled", default=False)),
        help="Off by default (v1 focuses on 2D hydraulics). Needs inputs.dem_target "
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

# ---- Calibration --------------------------------------------------------- #
with tabs[7]:
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

# ---- Workflow (info) ----------------------------------------------------- #
with tabs[8]:
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
    save_path = st.text_input("Save to path", value="cases/example-Inn/case-config-edited.yml")
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
