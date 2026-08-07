"""Stability test ladder for the internal loss/gain (percolation) setup.

Runs one rung of the ladder documented in ``test-approaches.md``. Each rung builds
into its OWN folder (``hydromate-case/ladder-<rung>/``) so the rungs can be compared
side by side and nothing clobbers the production ``simulation/`` case.

    T1  baseline, NO internal sources   (does the pre-wet fix alone converge?)
    T2  + internal sources as thin buffered SOURCE REGIONS
    T3  + losing region spread over the whole percolation patch
    T4  + USER_RAIN depth-limited percolation Fortran

Everything a rung changes relative to ``case-config.yml`` is listed in ``RUNGS``
below - nothing is hidden, so a rung can be reproduced by hand from the base config.

Usage:
    python cases/isar-2025/ladder.py T1              # build + run
    python cases/isar-2025/ladder.py T2 --build-only # build, do not launch the solver
    python cases/isar-2025/ladder.py T3 --ncsize 8   # override the core count

After the run it prints (and writes ``rung.json``) the boundary-flux verdict:
inflow vs outflow, the imbalance, and whether a steady window was reached.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from hydromate import (
    format_flux_convergence,
    pipeline,
    prepare_steady_inputs,
    run_solver_streaming,
    setup_logging,
)
from hydromate.config import load_config
from hydromate.env import TelemacRuntime
from hydromate.flux_convergence import HOTSTART_TOLERANCE, analyze_flux_convergence

CONFIG = Path(__file__).resolve().parent / "case-config.yml"

# Per-rung overrides applied to the base config, plus what each rung is testing.
RUNGS: dict[str, dict] = {
    "T1": {
        "why": "baseline: does prewet 0.30 alone restore the boundary-flux balance?",
        "drop_internal_lines": True,      # build without the int-* exchange at all
        "percolation_mode": "off",
    },
    "T2": {
        "why": "internal exchange as thin buffered source regions (3 m strips)",
        "drop_internal_lines": False,
        "percolation_mode": "off",
    },
    "T3": {
        "why": "losing exchange spread over the whole percolation patch (~20x gentler)",
        "drop_internal_lines": False,
        "percolation_mode": "region",
    },
    "T4": {
        "why": "USER_RAIN: depth-limited withdrawal + mass-exact reinjection",
        "drop_internal_lines": False,
        "percolation_mode": "fortran",
    },
    # T5 uses the SAME mode as T4; what changed is the generated routine itself.
    # T4 spread Q uniformly over the whole patch and then tapered, so the dry half
    # of the patch simply lost its share (~48 % of the target delivered). T5
    # normalises over the currently WET area, so the full 0.065 m3/s is delivered
    # whenever there is enough wet patch to draw it from.
    "T5": {
        "why": "USER_RAIN normalised over the WET patch area (full target delivery)",
        "drop_internal_lines": False,
        "percolation_mode": "fortran",
    },
    # T6 draws the exchange from a 12 m strip along the losing LINE instead of the
    # whole patch. Measured on the T1 converged result: the patch is essentially
    # dry at steady state (median 0.2 mm, ~88 m2 wet) while the line strip sits in
    # the wetted channel (median ~0.07 m, ~183 m2 wet at 12 m) - so this is the
    # only region that can actually supply 0.065 m3/s once the reach settles.
    "T6": {
        "why": "USER_RAIN over a 12 m strip along the losing LINE (wetted channel)",
        "drop_internal_lines": False,
        "percolation_mode": "fortran",
        "losing_region": "line",
        "region_width": 12.0,
    },
}


def _strip_internal_lines(src: Path, dest: Path) -> Path:
    """Copy the liquid-boundary layer WITHOUT its internal ('int-*') lines.

    Used by T1 to get the same mesh and the same inflow/outflow prescription with
    no internal exchange, isolating the initial-condition fix from the sources.
    """
    import geopandas as gpd

    from hydromate.boundary import _is_internal, _type_column

    gdf = gpd.read_file(src)
    type_col = _type_column(gdf)
    keep = gdf[~gdf[type_col].map(_is_internal)] if type_col else gdf
    dest.parent.mkdir(parents=True, exist_ok=True)
    keep.to_file(dest, driver="GPKG")
    print(f"  T1: dropped {len(gdf) - len(keep)} internal line(s) -> {dest.name}")
    return dest


def check_side_channel(cfg, model_dir: Path, radius: float = 40.0) -> None:
    """Is the side channel actually fed? - the point of the whole exercise.

    Reads the run's ``r2d.slf`` and reports the wetted fraction and mean depth in a
    *radius* around the **gaining** line, which is what supplies the side channel.
    Compare a percolation run against the T1 baseline (which has no exchange at
    all): if the exchange is doing its job, the gaining neighbourhood is wetter.
    """
    import numpy as np
    import shapely

    from hydromate.boundary import _internal_sign, _is_internal, _type_column
    from hydromate.selafin import read_slf

    res = model_dir / cfg.results_slf
    if not res.exists():
        print(f"no result file yet at {res}")
        return
    import geopandas as gpd

    gdf = gpd.read_file(cfg.boundaries.liquid_boundaries)
    if gdf.crs and gdf.crs.to_epsg() != cfg.crs_epsg:
        gdf = gdf.to_crs(epsg=cfg.crs_epsg)
    tcol = _type_column(gdf)
    gain = [r.geometry for _, r in gdf.iterrows()
            if _is_internal(r[tcol]) and _internal_sign(str(r[tcol])) > 0]
    if not gain:
        print("no gaining line in the layer (T1 baseline?) - reporting the same "
              "neighbourhood anyway for comparison")
        gain = [r.geometry for _, r in gpd.read_file(
            cfg.config_dir / "user-sources/geodata/liquid-boundaries.gpkg"
        ).iterrows() if _is_internal(r[tcol]) and _internal_sign(str(r[tcol])) > 0]
    zone = gain[0].buffer(radius)

    d = read_slf(res)
    h = np.asarray(d["values"]["WATER DEPTH"])
    x, y = np.asarray(d["x"]), np.asarray(d["y"])
    m = shapely.contains_xy(zone, x, y)
    hz = h[m]
    if not len(hz):
        print("no nodes near the gaining line")
        return
    wet = hz > 0.01
    print(f"\nside channel around the gaining line (r={radius:g} m, {len(hz)} nodes):")
    print(f"  wetted (h>1 cm): {wet.sum()} nodes ({100 * wet.mean():.1f} %)")
    print(f"  mean depth     : {hz.mean():.4f} m   (wet only: "
          f"{hz[wet].mean() if wet.any() else 0:.4f} m)")
    print(f"  max depth      : {hz.max():.4f} m")


def summarize_sortie(model_dir: Path) -> dict:
    """Boundary-flux verdict parsed straight from the TELEMAC listing.

    ``hydromate.analyze_flux_convergence`` needs several listing printouts, which is not
    installed everywhere; this fallback needs nothing but the `.sortie`. It reads
    every ``BALANCE OF WATER VOLUME`` block and reports how the domain volume, the
    gross in/out fluxes and their relative imbalance evolve - which is the actual
    convergence criterion for these rungs.
    """
    import re

    cands = [p for p in model_dir.glob("**/*.sortie") if "_p0" not in p.name]
    if not cands:
        return {"error": "no .sortie found"}
    sortie = max(cands, key=lambda p: p.stat().st_size)

    blocks, cur = [], None
    for ln in sortie.read_text(errors="replace").splitlines():
        if "BALANCE OF WATER VOLUME" in ln:
            cur = {"fluxes": []}
        elif cur is not None:
            if (m := re.search(r"VOLUME IN THE DOMAIN\s*:\s*(\S+)", ln)):
                cur["volume"] = float(m.group(1))
            elif (m := re.search(r"FLUX BOUNDARY\s+(\d+):\s*(\S+)", ln)):
                cur["fluxes"].append(float(m.group(2)))
            elif (m := re.search(r"RELATIVE ERROR IN VOLUME AT T =\s*(\S+)", ln)):
                cur["t"] = float(m.group(1))
            elif (m := re.search(r"TIME-STEP\s*:\s*(\S+)", ln)):
                cur["dt"] = float(m.group(1))
                if "t" in cur and cur["fluxes"]:
                    q_in = sum(f for f in cur["fluxes"] if f > 0)
                    q_out = -sum(f for f in cur["fluxes"] if f < 0)
                    cur["q_in"], cur["q_out"] = q_in, q_out
                    cur["imbalance"] = (q_in - q_out) / q_in if q_in else None
                    blocks.append(cur)
                cur = None
    if not blocks:
        return {"error": "no balance blocks", "sortie": str(sortie)}

    last = blocks[-1]
    trespass = "LIMIT VALUES TRESPASSED" in sortie.read_text(errors="replace")
    return {
        "sortie": str(sortie), "n_printouts": len(blocks),
        "final_time_s": last["t"], "final_volume_m3": last.get("volume"),
        "final_dt_s": last.get("dt"),
        "final_q_in": last["q_in"], "final_q_out": last["q_out"],
        "final_imbalance": last["imbalance"],
        "limit_values_trespassed": trespass,
        "history": [{"t": b["t"], "vol": b.get("volume"), "q_in": b["q_in"],
                     "q_out": b["q_out"], "imb": b["imbalance"], "dt": b.get("dt")}
                    for b in blocks[-12:]],
    }


def print_summary(s: dict) -> None:
    if "error" in s:
        print(f"flux summary unavailable: {s['error']}")
        return
    print(f"\nboundary-flux summary ({s['n_printouts']} printouts, "
          f"final T={s['final_time_s']:g} s):")
    print(f"{'T [s]':>10} {'volume':>11} {'Q_in':>9} {'Q_out':>9} "
          f"{'imbalance':>11} {'dt [s]':>9}")
    for b in s["history"]:
        imb = "n/a" if b["imb"] is None else f"{b['imb'] * 100:9.3f} %"
        print(f"{b['t']:10.1f} {b['vol']:11.1f} {b['q_in']:9.4f} "
              f"{b['q_out']:9.4f} {imb:>11} {b['dt']:9.5f}")
    if s["limit_values_trespassed"]:
        print("VERDICT: FAILED - LIMIT VALUES TRESPASSED (divergence guard fired)")
    elif abs(s["final_imbalance"] or 1) < 0.01:
        print(f"VERDICT: balanced to {s['final_imbalance'] * 100:.3f} % "
              "(<1 %) at the last printout")
    else:
        print(f"VERDICT: not yet balanced ({s['final_imbalance'] * 100:.2f} %)")


def configure(rung: str):
    """Load the base config and apply this rung's overrides."""
    spec = RUNGS[rung]
    cfg = load_config(CONFIG)

    # every rung gets its own model dir, so results survive for comparison
    base = Path(cfg.model_dir).parent
    cfg.model_dir = base / f"ladder-{rung}"
    cfg.postprocessing_dir = cfg.model_dir / "postprocessing"
    cfg.calibration_dir = cfg.model_dir / "calibration"
    cfg.preprocessing_dir = cfg.model_dir / "preprocessing"
    cfg.ensure_dirs()

    cfg.percolation.mode = spec["percolation_mode"]
    if "losing_region" in spec:
        cfg.percolation.losing_region = spec["losing_region"]
    if "region_width" in spec:
        cfg.boundaries.internal_source_region_width = spec["region_width"]
    if spec["drop_internal_lines"]:
        cfg.boundaries.liquid_boundaries = _strip_internal_lines(
            Path(cfg.boundaries.liquid_boundaries),
            Path(cfg.model_dir) / "liquid-boundaries-no-internal.gpkg",
        )
    return cfg, spec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("rung", choices=sorted(RUNGS))
    ap.add_argument("--build-only", action="store_true",
                    help="build the case but do not launch the solver")
    ap.add_argument("--ncsize", type=int, default=None,
                    help="MPI process count for this run (default: config)")
    ap.add_argument("--summary", action="store_true",
                    help="only summarise an already-completed rung's listing")
    args = ap.parse_args()

    if args.summary:   # read-only: no rebuild, no filtered-layer side effects
        base_cfg = load_config(CONFIG)
        rung_dir = Path(base_cfg.model_dir).parent / f"ladder-{args.rung}"
        print_summary(summarize_sortie(rung_dir))
        check_side_channel(base_cfg, rung_dir)
        return 0

    cfg, spec = configure(args.rung)
    setup_logging(cfg.model_path(cfg.log_file))
    print(f"\n=== ladder {args.rung}: {spec['why']}")
    print(f"    building into {cfg.model_dir}")
    print(f"    prewet={cfg.initialization.prewet_depth} m  "
          f"duration={cfg.hydrodynamics.duration} s  "
          f"percolation={cfg.percolation.mode}  "
          f"region_width={cfg.boundaries.internal_source_region_width} m\n")

    q = prepare_steady_inputs(cfg)
    print(f"steady discharge Q={q:g} m3/s")
    art = pipeline.run(cfg, validate_env=False, dry_run=False)
    print(f"built: {art.cas_file.name}"
          + (f", {art.source_regions.name}" if art.source_regions else "")
          + (f", {art.user_fortran.name}/" if art.user_fortran else ""))
    if cfg.boundaries.stage_discharge and Path(cfg.boundaries.stage_discharge).exists():
        shutil.copy(cfg.boundaries.stage_discharge, cfg.model_path("rating-curve.csv"))

    summary = {"rung": args.rung, "why": spec["why"],
               "prewet_depth": cfg.initialization.prewet_depth,
               "duration": cfg.hydrodynamics.duration,
               "percolation_mode": cfg.percolation.mode,
               "region_width": cfg.boundaries.internal_source_region_width,
               "model_dir": str(cfg.model_dir)}

    if args.build_only:
        print("build-only: not launching the solver")
        (cfg.model_path("rung.json")).write_text(json.dumps(summary, indent=2) + "\n")
        return 0

    runtime = TelemacRuntime(cfg.telemac)
    runtime.check_available()
    proc = run_solver_streaming(runtime, cfg, ncsize=args.ncsize)
    summary["returncode"] = proc.returncode
    if proc.returncode != 0:
        print(f"FAILED - solver returned {proc.returncode}")
        (cfg.model_path("rung.json")).write_text(json.dumps(summary, indent=2) + "\n")
        return proc.returncode

    # the full analysis needs enough listing printouts to fit a convergence rate;
    # always emit the built-in summary too so a killed rung still has a verdict
    try:
        fc = analyze_flux_convergence(cfg, tolerance=HOTSTART_TOLERANCE)
    except Exception as exc:  # noqa: BLE001 - the run finished; report cleanly
        print(f"flux-convergence analysis skipped: {type(exc).__name__}: {exc}")
    else:
        for line in format_flux_convergence(fc):
            print(line)
        summary["flux"] = {k: (str(v) if isinstance(v, Path) else v)
                           for k, v in asdict(fc).items()}
    sortie_summary = summarize_sortie(Path(cfg.model_dir))
    print_summary(sortie_summary)
    summary["sortie_summary"] = sortie_summary
    (cfg.model_path("rung.json")).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nrung summary -> {cfg.model_path('rung.json')}")
    print("record the outcome in cases/isar-2025/test-approaches.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
