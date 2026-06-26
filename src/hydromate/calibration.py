"""Stage 5 - calibration points CSV and HydroBayesCal config emit.

Turns the hydraulic measurements into the calibration-points CSV that
HydroBayesCal reads (``id, x, y, z, <QTY>_DATA, <QTY>_ERROR`` columns) and emits
a ``config_Telemac.py`` wiring the produced case to the calibration parameters
and BAL settings declared in the hydromate config.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from hydromate.config import Config
from hydromate import ground_truth


def _resolve_quantity(df: pd.DataFrame, qty: str) -> tuple[pd.Series, pd.Series | None] | None:
    """Map a SELAFIN quantity name to (values, measured_error) from a tidy table.

    Returns ``None`` when the tidy table lacks the columns to produce *qty*, so
    callers can report missing quantities. ``measured_error`` is ``None`` when no
    per-point error was measured (the caller falls back to the configured
    fraction).
    """
    q = qty.upper()
    if q == "WATER DEPTH" and "h" in df:
        return df["h"].astype(float), None
    if q == "SCALAR VELOCITY" and any(c in df for c in ("u", "v", "w")):
        vel = ground_truth.scalar_velocity(df)
        # propagate component errors: sigma_V = (1/V) * sqrt(sum((c_i * sigma_i)^2))
        terms = [(df[c].astype(float) * df[f"{c}_err"].astype(float)) ** 2
                 for c in ("u", "v", "w") if c in df and f"{c}_err" in df]
        err = None
        if terms:
            safe_v = vel.replace(0.0, np.nan)
            err = (np.sqrt(sum(terms)) / safe_v).fillna(0.0)
        return vel, err
    if q == "VELOCITY U" and "u" in df:
        return df["u"].astype(float), df["u_err"].astype(float) if "u_err" in df else None
    if q == "VELOCITY V" and "v" in df:
        return df["v"].astype(float), df["v_err"].astype(float) if "v_err" in df else None
    return None


def _pick_table(tables: dict[str, pd.DataFrame], quantities: list[str]) -> pd.DataFrame:
    """Choose the tidy category whose columns satisfy all calibration quantities."""
    for df in (*([tables["hydraulics"]] if "hydraulics" in tables else []), *tables.values()):
        if all(_resolve_quantity(df, q) is not None for q in quantities):
            return df
    available = {cat: [c for c in df.columns if c not in ("x", "y", "z")]
                 for cat, df in tables.items()}
    raise ValueError(
        f"no ground-truth category provides all calibration quantities {quantities}. "
        f"Available columns per category: {available}"
    )


def build_calibration_csv(cfg: Config) -> Path | None:
    """Write the HydroBayesCal calibration-points CSV from the tidy ground truth.

    The tidy table is compiled from ``inputs.ground_truth`` when given, otherwise
    read from a user-authored ``inputs.measurements``. Returns ``None`` if neither
    is configured.
    """
    compile_ground_truth(cfg)            # no-op when the table is user-supplied
    if not cfg.inputs.ground_truth and cfg.inputs.measurements is None:
        return None
    tables = ground_truth.read_tidy(cfg.ground_truth_path)
    quantities = cfg.calibration.calibration_quantities
    df = _pick_table(tables, quantities).reset_index(drop=True)

    out = pd.DataFrame({
        "id": np.arange(1, len(df) + 1),
        "x": df["x"].astype(float),
        "y": df["y"].astype(float),
        "z": df["z"].astype(float) if "z" in df else 0.0,
    })
    frac = cfg.calibration.measurement_error
    for qty in quantities:
        values, measured_err = _resolve_quantity(df, qty)
        err = measured_err if measured_err is not None else values.abs() * frac
        out[f"{qty}_DATA"] = values.round(6)
        out[f"{qty}_ERROR"] = err.round(6)

    path = cfg.calibration_path(cfg.calibration_csv)
    out.to_csv(path, index=False)
    return path


def compile_ground_truth(cfg: Config) -> Path | None:
    """Compile configured raw ground-truth sources into the tidy table (if any)."""
    return ground_truth.compile_ground_truth(cfg)


def emit_hbc_config(cfg: Config, calibration_csv: Path | None) -> Path:
    """Emit a HydroBayesCal ``config_Telemac.py`` for the produced case."""
    c = cfg.calibration
    params = [p.name for p in c.parameters]
    ranges = [[p.min, p.max] for p in c.parameters]
    results_base = Path(cfg.results_slf).stem

    def pylist(items, q=True):
        if q:
            return "[" + ", ".join(repr(str(i)) for i in items) + "]"
        return "[" + ", ".join(str(i) for i in items) + "]"

    # bal_telemac.py reads config.morphodynamic_simulation['gaia_cas'] /
    # ['gaia_results_filename_base'] unconditionally, so always emit the block;
    # the GAIA values are None when morphodynamics is off (hydraulic-only case).
    if cfg.morphodynamics.enabled:
        gaia_cas = repr(cfg.gaia_cas)
        gaia_base = repr(cfg.morphodynamics.gaia_results_base)
    else:
        gaia_cas = gaia_base = "None"
    morph = (
        "morphodynamic_simulation = {\n"
        f"    'gaia_cas': {gaia_cas},\n"
        f"    'gaia_results_filename_base': {gaia_base},\n"
        "}\n\n"
    )

    text = f'''"""HydroBayesCal TELEMAC config generated by hydromate for case '{cfg.name}'.

Run a surrogate-assisted Bayesian calibration with:
    python bal_telemac.py --config {cfg.hbc_config}
"""

import os

BASE_DIR = {str(cfg.model_dir)!r}

paths = {{
    'case_template_dir': BASE_DIR,
    'model_dir': BASE_DIR,
    'res_dir': {str(cfg.calibration_dir)!r},
    'calibration_pts_file_path': {(str(calibration_csv) if calibration_csv else "")!r},
}}

hydrodynamic_simulation = {{
    'solver_name': 'Telemac2d',
    'n_processors': {cfg.telemac.n_processors},
    'results_filename_base': {results_base!r},
    'control_file': {cfg.cas_file!r},
    'friction_file': {cfg.friction_tbl!r},
    'fortran_file': None,
}}

{morph}calibration = {{
    'parameters': {pylist(params)},
    'param_values': {ranges},
    'extraction_quantities': {pylist(c.extraction_quantities)},
    'calibration_quantities': {pylist(c.calibration_quantities)},
    'dict_output_name': 'extraction-data',
}}

sampling = {{
    'init_runs': {c.init_runs},
    'max_runs': {c.max_runs},
    'parameter_distribution': 'uniform',
    'parameter_sampling_method': {c.parameter_sampling_method!r},
    'tp_selection_criteria': 'dkl',
    'eval_steps': 1,
    'prior_samples': 25000,
    'mc_samples_al': 2000,
    'mc_exploration': 1000,
    'gp_library': {c.gp_library!r},
}}

execution = {{
    'complete_bal_mode': True,
    'only_bal_mode': False,
    'delete_complex_outputs': True,
    'validation': False,
    'user_param_values': False,
}}
'''
    path = cfg.calibration_path(cfg.hbc_config)
    path.write_text(text)
    return path
