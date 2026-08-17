"""Reading raster values at arbitrary points.

Separated from the meshers because both of them need it for the same reason - to put
a bed elevation on a mesh node - and neither owns the DEM. Keeping it here is what
lets the OpenFOAM backend sample the terrain without importing anything from the
TELEMAC backend.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def sample_raster_at(dem_path: Path, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinear raster value at arbitrary points, nearest-filled where nodata."""
    import rasterio
    from scipy.interpolate import RegularGridInterpolator

    with rasterio.open(dem_path) as src:
        band = src.read(1, masked=True).astype(float)
        nodata = src.nodata
        t = src.transform
        rows = np.arange(src.height)
        cols = np.arange(src.width)
        xs = t.c + t.a * (cols + 0.5)
        ys = t.f + t.e * (rows + 0.5)

    data = np.array(band.filled(np.nan))
    if t.e < 0:  # north-up raster: flip rows so y is ascending for the interpolator
        ys = ys[::-1]
        data = data[::-1, :]

    interp = RegularGridInterpolator(
        (ys, xs), data, bounds_error=False, fill_value=np.nan
    )
    z = interp(np.column_stack([y, x]))

    # fill any NaNs (nodata / just outside grid) from nearest valid node
    nan = np.isnan(z)
    if nan.any():
        from scipy.spatial import cKDTree

        good = ~nan
        if not good.any():
            raise ValueError(f"DEM {dem_path} yielded no valid elevations on the mesh")
        tree = cKDTree(np.column_stack([x[good], y[good]]))
        _, idx = tree.query(np.column_stack([x[nan], y[nan]]))
        z[nan] = z[good][idx]
    if nodata is not None:
        z[z == nodata] = np.nan
    return z
