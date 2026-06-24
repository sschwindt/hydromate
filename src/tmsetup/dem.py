"""Stage 1 — DEM ingest, reprojection and clipping to the region of interest.

Takes the user's initial (and optional target) DEM, reprojects to the project
CRS if needed, and clips it to the ROI boundary polygon. The clipped initial DEM
feeds bathymetry interpolation onto the mesh; the (initial, target) pair feeds an
optional DEM-of-Difference used as topographic-change calibration data for the
morphodynamic (GAIA) calibration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tmsetup.config import Config


@dataclass
class ClippedDEM:
    path: Path
    nodata: float


def _read_boundary_geom(boundary_path: Path, crs_epsg: int):
    import geopandas as gpd

    gdf = gpd.read_file(boundary_path)
    if gdf.crs is None:
        raise ValueError(f"{boundary_path} has no CRS; expected EPSG:{crs_epsg}")
    if gdf.crs.to_epsg() != crs_epsg:
        gdf = gdf.to_crs(epsg=crs_epsg)
    # Lines (max wetted extent drawn as a closed polyline) -> polygonise.
    geom_types = set(gdf.geom_type)
    if geom_types & {"LineString", "MultiLineString"}:
        from shapely.ops import polygonize, unary_union

        merged = unary_union(gdf.geometry.values)
        polys = list(polygonize(merged))
        if not polys:
            raise ValueError(
                f"{boundary_path}: boundary lines do not form a closed polygon."
            )
        return polys
    return list(gdf.geometry.values)


def _reproject_to(src_path: Path, dst_path: Path, crs_epsg: int) -> Path:
    """Reproject a raster to the target CRS only if it differs."""
    import rasterio
    from rasterio.warp import calculate_default_transform, reproject, Resampling

    with rasterio.open(src_path) as src:
        if src.crs and src.crs.to_epsg() == crs_epsg:
            return src_path
        dst_crs = f"EPSG:{crs_epsg}"
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        meta = src.meta.copy()
        meta.update(crs=dst_crs, transform=transform, width=width, height=height)
        with rasterio.open(dst_path, "w", **meta) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform, src_crs=src.crs,
                    dst_transform=transform, dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                )
    return dst_path


def clip_dem(cfg: Config, dem_path: Path, out_name: str) -> ClippedDEM:
    """Reproject *dem_path* to the project CRS and clip it to the ROI boundary."""
    import rasterio
    from rasterio.mask import mask

    cfg.ensure_dirs()
    work = Path(cfg.work_dir)
    reproj = _reproject_to(Path(dem_path), work / f"_reproj_{out_name}", cfg.crs_epsg)
    geoms = _read_boundary_geom(Path(cfg.inputs.boundary), cfg.crs_epsg)

    with rasterio.open(reproj) as src:
        nodata = src.nodata if src.nodata is not None else -9999.0
        out_image, out_transform = mask(src, geoms, crop=True, nodata=nodata, filled=True)
        meta = src.meta.copy()
        meta.update(
            height=out_image.shape[1], width=out_image.shape[2],
            transform=out_transform, nodata=nodata,
        )
    out_path = work / out_name
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(out_image)
    return ClippedDEM(path=out_path, nodata=float(nodata))


def dem_of_difference(cfg: Config, initial: ClippedDEM, target: ClippedDEM) -> Path:
    """Compute target - initial on the initial grid -> bed-change raster (m)."""
    import rasterio
    from rasterio.warp import reproject, Resampling

    with rasterio.open(initial.path) as ref:
        ref_data = ref.read(1, masked=True)
        meta = ref.meta.copy()
        tgt = np.full(ref_data.shape, ref.nodata, dtype="float32")
        with rasterio.open(target.path) as t:
            reproject(
                source=rasterio.band(t, 1), destination=tgt,
                src_transform=t.transform, src_crs=t.crs,
                dst_transform=ref.transform, dst_crs=ref.crs,
                resampling=Resampling.bilinear, dst_nodata=ref.nodata,
            )
    tgt_m = np.ma.masked_equal(tgt, ref.nodata)
    diff = (tgt_m - ref_data).filled(meta["nodata"])
    out_path = Path(cfg.work_dir) / "dem-of-difference.tif"
    meta.update(dtype="float32", count=1)
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(diff.astype("float32"), 1)
    return out_path


def run(cfg: Config) -> dict[str, Path]:
    """Execute stage 1; returns the produced raster paths."""
    out: dict[str, Path] = {}
    initial = clip_dem(cfg, Path(cfg.inputs.dem_initial), "dem-initial-roi.tif")
    out["dem_initial_roi"] = initial.path

    target = None
    if cfg.inputs.dem_target is not None:
        target = clip_dem(cfg, Path(cfg.inputs.dem_target), "dem-target-roi.tif")
        out["dem_target_roi"] = target.path

    if cfg.morphodynamics.enabled:
        if cfg.inputs.dem_of_difference is not None:
            out["dod"] = Path(cfg.inputs.dem_of_difference)
        elif target is not None:
            out["dod"] = dem_of_difference(cfg, initial, target)
    return out
