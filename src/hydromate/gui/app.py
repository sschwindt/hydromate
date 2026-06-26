"""Streamlit configuration editor for hydromate.

A browser form to create / edit a hydromate YAML config, mirroring the dataclass
schema in :mod:`hydromate.config`, with the ability to save the YAML and run
``hydromate --check`` (validate) or a full build directly from the browser.

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
QUANTITIES = [
    "WATER DEPTH", "SCALAR VELOCITY", "VELOCITY U", "VELOCITY V",
    "TURBULENT ENERG", "CUMUL BED EVOL",
]
OPTIONAL_INPUTS = [
    "dem_target", "breaklines", "region_points", "region_table",
    "stage_discharge", "measurements", "dem_of_difference",
]

TEMPLATE: dict = {
    "project": {"name": "case", "crs_epsg": 25832,
                "preprocessing_dir": "tm-simulation/preprocessing",
                "model_dir": "tm-simulation/simulation",
                "postprocessing_dir": "tm-simulation/postprocessing",
                "calibration_dir": "tm-simulation/calibration-validation"},
    "telemac": {"pysource": "/path/to/telemac/configs/pysource.sh",
                "solver": "telemac2d", "n_processors": 4},
    "inputs": {"dem_initial": "path/to/dem.tif", "boundary": "path/to/roi.gpkg",
               "liquid_boundaries": "path/to/liquid-boundaries.shp",
               "inflow": "path/to/inflow.csv"},
    "mesh": {"default_size": 10.0, "breakline_size": 3.0, "min_size": 0.5,
             "region_sizes": {}},
    "friction": {"default_law": 4, "default_coefficient": 0.03, "zones": []},
    "hydrodynamics": {"regime": "steady", "time_step": 1.0, "n_time_steps": 15000,
                      "graphic_printout_period": 500, "listing_printout_period": 500,
                      "prescribed_elevation": 0.0},
    "calibration": {"calibration_quantities": ["WATER DEPTH"],
                    "extraction_quantities": ["WATER DEPTH", "SCALAR VELOCITY"],
                    "measurement_error": 0.10, "init_runs": 30, "max_runs": 50,
                    "gp_library": "gpy",
                    "parameters": [{"name": "zone1", "min": 0.02, "max": 0.04,
                                    "comment": ""}]},
}


def _get(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if d != {} else default


def build_yaml(cfg: dict) -> str:
    """Serialise the working config to YAML, dropping empty optional fields."""
    out = copy.deepcopy(cfg)
    # drop empty optional inputs
    out["inputs"] = {k: v for k, v in out.get("inputs", {}).items()
                     if v not in (None, "", "path/to/")}
    # drop empty prescribed_flowrate
    h = out.get("hydrodynamics", {})
    if h.get("prescribed_flowrate") in (None, ""):
        h.pop("prescribed_flowrate", None)
    if not out.get("mesh", {}).get("region_sizes"):
        out.get("mesh", {}).pop("region_sizes", None)
    if not out.get("friction", {}).get("zones"):
        out.get("friction", {}).pop("zones", None)
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
st.caption("Build and validate a TELEMAC case configuration, then run it.")

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

tabs = st.tabs(["Project", "TELEMAC", "Inputs", "Mesh", "Friction",
                "Hydrodynamics", "Calibration"])

# ---- Project ------------------------------------------------------------- #
with tabs[0]:
    p = cfg.setdefault("project", {})
    c1, c2 = st.columns(2)
    p["name"] = c1.text_input("Case name", _get(cfg, "project", "name", default="case"))
    p["crs_epsg"] = int(c2.number_input(
        "CRS EPSG (metric)", value=int(_get(cfg, "project", "crs_epsg", default=25832)),
        step=1, format="%d"))
    p["preprocessing_dir"] = c1.text_input(
        "Preprocessing dir",
        _get(cfg, "project", "preprocessing_dir", default="tm-simulation/preprocessing"))
    p["model_dir"] = c2.text_input(
        "Simulation (model) dir",
        _get(cfg, "project", "model_dir", default="tm-simulation/simulation"))
    p["postprocessing_dir"] = c1.text_input(
        "Postprocessing dir",
        _get(cfg, "project", "postprocessing_dir", default="tm-simulation/postprocessing"))
    p["calibration_dir"] = c2.text_input(
        "Calibration-validation dir",
        _get(cfg, "project", "calibration_dir", default="tm-simulation/calibration-validation"))

# ---- TELEMAC ------------------------------------------------------------- #
with tabs[1]:
    t = cfg.setdefault("telemac", {})
    t["pysource"] = st.text_input(
        "pysource.*.sh (TELEMAC environment script) - sourced, not imported",
        _get(cfg, "telemac", "pysource", default=""))
    c1, c2 = st.columns(2)
    sol = _get(cfg, "telemac", "solver", default="telemac2d")
    t["solver"] = c1.selectbox("Solver", SOLVERS,
                               index=SOLVERS.index(sol) if sol in SOLVERS else 0)
    t["n_processors"] = int(c2.number_input(
        "Processors", value=int(_get(cfg, "telemac", "n_processors", default=4)),
        min_value=1, step=1))

# ---- Inputs -------------------------------------------------------------- #
with tabs[2]:
    inp = cfg.setdefault("inputs", {})
    st.markdown("**Required** (paths are resolved relative to the saved config file)")
    for key in ("dem_initial", "boundary", "liquid_boundaries", "inflow"):
        inp[key] = st.text_input(key, _get(cfg, "inputs", key, default=""))
    st.markdown("**Optional** (leave blank to omit)")
    for key in OPTIONAL_INPUTS:
        val = st.text_input(key, _get(cfg, "inputs", key, default="") or "")
        if val:
            inp[key] = val
        else:
            inp.pop(key, None)

# ---- Mesh ---------------------------------------------------------------- #
with tabs[3]:
    m = cfg.setdefault("mesh", {})
    c1, c2, c3 = st.columns(3)
    m["default_size"] = c1.number_input("Default edge length (m)",
                                        value=float(_get(cfg, "mesh", "default_size", default=10.0)))
    m["breakline_size"] = c2.number_input("Breakline edge length (m)",
                                          value=float(_get(cfg, "mesh", "breakline_size", default=3.0)))
    m["min_size"] = c3.number_input("Min size (m)",
                                    value=float(_get(cfg, "mesh", "min_size", default=0.5)))
    st.markdown("Per-MATID refinement (optional)")
    rs = _get(cfg, "mesh", "region_sizes", default={}) or {}
    rs_rows = [{"matid": int(k), "size": float(v)} for k, v in rs.items()]
    edited = st.data_editor(rs_rows or [{"matid": 1, "size": 1.5}],
                            num_rows="dynamic", key="region_sizes",
                            column_config={"matid": st.column_config.NumberColumn("MATID", step=1),
                                           "size": st.column_config.NumberColumn("size (m)")})
    m["region_sizes"] = {int(r["matid"]): float(r["size"]) for r in edited
                         if r.get("matid") not in (None, "")}

# ---- Friction ------------------------------------------------------------ #
with tabs[4]:
    f = cfg.setdefault("friction", {})
    c1, c2 = st.columns(2)
    law = int(_get(cfg, "friction", "default_law", default=4))
    law_names = [f"{k} {v}" for k, v in LAWS.items()]
    f["default_law"] = list(LAWS)[c1.selectbox(
        "Default friction law", range(len(LAWS)),
        index=list(LAWS).index(law) if law in LAWS else 4,
        format_func=lambda i: law_names[i])]
    f["default_coefficient"] = c2.number_input(
        "Default coefficient", value=float(_get(cfg, "friction", "default_coefficient", default=0.03)),
        format="%.4f")
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
    c1, c2 = st.columns(2)
    reg = _get(cfg, "hydrodynamics", "regime", default="steady")
    h["regime"] = c1.selectbox("Regime", ["steady", "unsteady"],
                               index=0 if reg == "steady" else 1)
    h["n_time_steps"] = int(c2.number_input(
        "Number of time steps", value=int(_get(cfg, "hydrodynamics", "n_time_steps", default=15000)),
        step=100))
    h["time_step"] = c1.number_input("Time step (s)",
                                     value=float(_get(cfg, "hydrodynamics", "time_step", default=1.0)))
    h["prescribed_elevation"] = c2.number_input(
        "Downstream prescribed elevation (m a.s.l.)",
        value=float(_get(cfg, "hydrodynamics", "prescribed_elevation", default=0.0)), format="%.3f")
    q = _get(cfg, "hydrodynamics", "prescribed_flowrate", default="")
    q_in = st.text_input("Prescribed inflow Q (m³/s) - blank = mean of inflow series", str(q or ""))
    if q_in.strip():
        h["prescribed_flowrate"] = float(q_in)
    else:
        h.pop("prescribed_flowrate", None)

# ---- Calibration --------------------------------------------------------- #
with tabs[6]:
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
    st.markdown("Calibration parameters (HydroBayesCal naming: `zone<MATID>`, "
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

st.session_state.cfg = cfg

# ---- output: preview / save / run ---------------------------------------- #
st.divider()
left, right = st.columns([3, 2])

with left:
    st.subheader("Generated YAML")
    yaml_text = build_yaml(cfg)
    st.code(yaml_text, language="yaml")
    st.download_button("Download YAML", yaml_text, file_name=f"{cfg['project']['name']}.yml")

with right:
    st.subheader("Save & run")
    save_path = st.text_input("Save to path", value="cases/example-Inn/case-config-edited.yml")
    if st.button("💾 Save YAML"):
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_yaml(cfg))
        st.success(f"Saved to {path}")

    st.caption("Validate and build run the saved file (paths resolve relative to it).")
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
