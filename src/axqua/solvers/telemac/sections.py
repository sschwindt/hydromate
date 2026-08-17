"""Discharge across internal cross-section ("baffle") lines.

TELEMAC's own ``CONTROL SECTIONS`` keyword reports the flux across sections defined
by *node pairs* in the steering file, which means the section geometry has to be
pinned to mesh node numbers before the run. This module does the same job
**afterwards, from the result file**, so the sections can be drawn freely in GIS as
a line layer and changed without re-running the solver.

For each line the unit-discharge vector ``q = (H*U, H*V)`` is interpolated from the
mesh onto dense sample points along the line and integrated against the line normal:

.. math:: Q = \\int_L (q \\cdot n) \\, ds

The normal is the right-hand normal of the digitised line; because the digitising
direction of a GIS layer is arbitrary, the sign is normalised so that a section
reports the discharge **positive in the direction of its own net flow** (the sign
convention is reported in ``orientation``). A braided reach can therefore be split
into its threads and each thread's share read off directly.

Accuracy notes
--------------
* Interpolation is linear within the triangle containing each sample point
  (:class:`matplotlib.tri.LinearTriInterpolator`), i.e. the same P1 basis TELEMAC
  itself uses for the depth and velocity fields - so the integral is consistent
  with the discretisation rather than a nearest-node approximation.
* Sample spacing defaults to a quarter of the local mesh edge length, which keeps
  the trapezoidal integration error well below the solver's own mass imbalance.
* Coordinates come from the *geometry* SELAFIN (written in double precision) when
  available: the result file is single precision, and on UTM easting ~677 000 m the
  float32 spacing is ~0.06 m - a fifth of the 0.3 m channel cell size, which would
  visibly distort the section geometry.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

#: sample spacing along a section, as a fraction of the mean mesh edge length
SAMPLE_FRACTION = 0.25
#: never sample coarser than this (m), whatever the mesh
MAX_SAMPLE_SPACING = 0.5


def _mean_edge_length(x, y, ikle) -> float:
    import numpy as np

    t = ikle - 1 if ikle.min() >= 1 else ikle
    p = np.column_stack([x, y])
    d = [np.hypot(*(p[t[:, a]] - p[t[:, b]]).T) for a, b in ((0, 1), (1, 2), (2, 0))]
    d = np.concatenate(d)
    d = d[d > 0]
    return float(d.mean()) if d.size else 1.0


def _read_fields(results: Path, geometry: Path | None):
    """Last frame of *results* plus double-precision coordinates from *geometry*."""
    import numpy as np

    from axqua.core.selafin import read_slf

    res = read_slf(Path(results))
    vals = res["values"]
    names = {n.strip().upper(): n for n in vals}
    need = ("WATER DEPTH", "VELOCITY U", "VELOCITY V")
    missing = [q for q in need if q not in names]
    if missing:
        raise ValueError(f"{Path(results).name}: result file lacks {missing} "
                         "(the .cas graphic printout needs 'U,V,H')")
    h = np.asarray(vals[names["WATER DEPTH"]], dtype=float)
    u = np.asarray(vals[names["VELOCITY U"]], dtype=float)
    v = np.asarray(vals[names["VELOCITY V"]], dtype=float)

    x, y, ikle = res["x"], res["y"], res["ikle"]
    if geometry is not None and Path(geometry).exists():
        geo = read_slf(Path(geometry))
        if len(geo["x"]) != len(x):
            raise ValueError(
                f"{Path(geometry).name} has {len(geo['x'])} nodes but "
                f"{Path(results).name} has {len(x)}: they come from different builds. "
                "Re-run the solver so the result matches the current mesh "
                "(a stale result cannot be sampled on the new geometry)."
            )
        x, y, ikle = geo["x"], geo["y"], geo["ikle"]
    else:
        # single-precision result coordinates: on UTM eastings the float32 spacing is
        # ~0.06 m, which merges nodes of a sub-metre mesh into degenerate triangles.
        log.warning("no geometry file given - integrating on the result's "
                    "single-precision coordinates; sub-metre meshes may fail to "
                    "triangulate")
    t = ikle - 1 if ikle.min() >= 1 else ikle
    return np.asarray(x, float), np.asarray(y, float), np.asarray(t), h, u, v


def line_discharges(results: Path, lines, *, geometry: Path | None = None,
                    name_field: str | None = None, crs_epsg: int | None = None):
    """Integrate the discharge across each line of the *lines* layer.

    ``lines`` is a path to a line layer (any CRS - reprojected to *crs_epsg*) or an
    already-loaded GeoDataFrame. Returns a DataFrame with one row per line:
    ``name``, ``discharge`` (m3/s, positive along the section's net flow),
    ``wetted_width``, ``mean_depth``, ``mean_velocity``, ``orientation``.
    """
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    from matplotlib.tri import LinearTriInterpolator, Triangulation

    x, y, tri, h, u, v = _read_fields(results, geometry)
    triang = Triangulation(x, y, tri)
    # interpolate the unit discharge (H*U, H*V), not U and H separately: the product
    # of two P1 fields is not P1, and on a partly wet section interpolating H*U
    # keeps the flux exactly zero where the depth is zero.
    interp_qx = LinearTriInterpolator(triang, h * u)
    interp_qy = LinearTriInterpolator(triang, h * v)
    interp_h = LinearTriInterpolator(triang, h)

    if isinstance(lines, (str, Path)):
        gdf = gpd.read_file(lines)
    else:
        gdf = lines.copy()
    if crs_epsg is not None and gdf.crs is not None and gdf.crs.to_epsg() != crs_epsg:
        gdf = gdf.to_crs(epsg=crs_epsg)
    if name_field is None:
        cand = [c for c in gdf.columns if c != gdf.geometry.name
                and gdf[c].dtype == object]
        name_field = cand[0] if cand else None

    ds = min(SAMPLE_FRACTION * _mean_edge_length(x, y, tri), MAX_SAMPLE_SPACING)
    rows = []
    for i, rec in gdf.iterrows():
        geom = rec.geometry
        if geom is None or geom.is_empty:
            continue
        name = str(rec[name_field]) if name_field else f"section-{i + 1}"
        n = max(3, int(np.ceil(geom.length / ds)) + 1)
        s = np.linspace(0.0, geom.length, n)
        pts = np.array([[geom.interpolate(t).x, geom.interpolate(t).y] for t in s])
        # tangent by central differences -> right-hand normal (ty, -tx)
        tx = np.gradient(pts[:, 0], s)
        ty = np.gradient(pts[:, 1], s)
        norm = np.hypot(tx, ty)
        norm[norm == 0] = 1.0
        nx, ny = ty / norm, -tx / norm

        qx = np.asarray(interp_qx(pts[:, 0], pts[:, 1]).filled(0.0), dtype=float)
        qy = np.asarray(interp_qy(pts[:, 0], pts[:, 1]).filled(0.0), dtype=float)
        hh = np.asarray(interp_h(pts[:, 0], pts[:, 1]).filled(0.0), dtype=float)
        qn = qx * nx + qy * ny
        q_signed = float(np.trapezoid(qn, s))

        wet = hh > 1e-3
        width = float(np.trapezoid(wet.astype(float), s))
        mean_h = float(hh[wet].mean()) if wet.any() else 0.0
        area = float(np.trapezoid(hh, s))
        mean_u = abs(q_signed) / area if area > 1e-9 else 0.0
        # normalise the sign: report the section's own net flow as positive
        flip = q_signed < 0
        rows.append({
            "name": name,
            "discharge": abs(q_signed),
            "wetted_width": width,
            "mean_depth": mean_h,
            "mean_velocity": mean_u,
            "orientation": "left-hand" if flip else "right-hand",
        })
        log.info("  section %-16s Q = %8.4f m3/s  (wet %5.1f m, mean h %.3f m, "
                 "mean |U| %.3f m/s)", name, abs(q_signed), width, mean_h, mean_u)
    return pd.DataFrame(rows)


def write_line_discharges(results: Path, lines, out: Path, *,
                          geometry: Path | None = None,
                          name_field: str | None = None,
                          crs_epsg: int | None = None,
                          name_column: str = "Baffle Name",
                          discharge_column: str = "discharge (m3/s)"):
    """Extract the section discharges and write the two-column CSV to *out*.

    The CSV carries exactly the section name and its discharge in m3/s; the full
    table (wetted width, mean depth, mean velocity) is returned for logging.
    """
    df = line_discharges(results, lines, geometry=geometry,
                         name_field=name_field, crs_epsg=crs_epsg)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.rename(columns={"name": name_column, "discharge": discharge_column})[
        [name_column, discharge_column]
    ].to_csv(out, index=False, float_format="%.4f")
    log.info("wrote %d section discharges -> %s", len(df), out)
    return df
