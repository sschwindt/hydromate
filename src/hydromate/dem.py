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

from hydromate.config import Config


@dataclass
class ClippedDEM:
    path: Path
    nodata: float


def _gdf_epsg(boundary_path: Path) -> int | None:
    import geopandas as gpd

    crs = gpd.read_file(boundary_path, rows=1).crs
    return crs.to_epsg() if crs is not None else None


def _load_polygons(boundary_path: Path, target_epsg: int):
    """Read the boundary, reproject to *target_epsg*, polygonise lines if needed."""
    import geopandas as gpd

    gdf = gpd.read_file(boundary_path)
    if gdf.crs is None:
        raise ValueError(
            f"{boundary_path} has no CRS; cannot place it relative to the DEM."
        )
    if gdf.crs.to_epsg() != target_epsg:
        gdf = gdf.to_crs(epsg=target_epsg)
    if set(gdf.geom_type) & {"LineString", "MultiLineString"}:
        from shapely.ops import polygonize, unary_union

        polys = list(polygonize(unary_union(gdf.geometry.values)))
        if not polys:
            raise ValueError(
                f"{boundary_path}: boundary lines do not form a closed polygon."
            )
        return polys
    return list(gdf.geometry.values)


def _reproject_raster(src_path: Path, dst_path: Path, target_epsg: int,
                      assume_epsg: int | None = None) -> Path:
    """Reproject *src_path* to *target_epsg* (no-op if already there).

    A raster without an embedded CRS is treated as being in *assume_epsg*.
    """
    import rasterio
    from rasterio.crs import CRS
    from rasterio.warp import calculate_default_transform, reproject, Resampling

    with rasterio.open(src_path) as src:
        src_crs = src.crs or (CRS.from_epsg(assume_epsg) if assume_epsg else None)
        if src_crs is None:
            raise ValueError(f"{src_path} has no CRS and no assumed EPSG was given.")
        if src_crs.to_epsg() == target_epsg:
            return Path(src_path)
        dst_crs = CRS.from_epsg(target_epsg)
        transform, width, height = calculate_default_transform(
            src_crs, dst_crs, src.width, src.height, *src.bounds
        )
        meta = src.meta.copy()
        meta.update(crs=dst_crs, transform=transform, width=width, height=height)
        with rasterio.open(dst_path, "w", **meta) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i), destination=rasterio.band(dst, i),
                    src_transform=src.transform, src_crs=src_crs,
                    dst_transform=transform, dst_crs=dst_crs,
                    resampling=Resampling.bilinear,
                )
    return Path(dst_path)


def clip_to_roi(dem_path, boundary_path, out_path, *, target_epsg: int | None = None,
                all_touched: bool = False, compress: str | None = "lzw") -> Path:
    """Clip a raster to a boundary polygon and write it to *out_path*.

    By default the raster is cropped in its own grid and CRS (resolution
    preserved). If the raster has no embedded CRS it is assumed to be in the
    boundary's CRS, which is then stamped onto the output. Pass *target_epsg* to
    reproject the raster before clipping.

    Parameters
    ----------
    dem_path, boundary_path, out_path : input raster, ROI polygon, output raster.
    target_epsg : optional EPSG to reproject the raster to before clipping.
    all_touched : include pixels touched by the polygon edge (default: centre-in).
    compress : GeoTIFF compression for the output (default ``"lzw"``).
    """
    import rasterio
    from rasterio.crs import CRS
    from rasterio.mask import mask

    dem_path, out_path = Path(dem_path), Path(out_path)
    boundary_epsg = _gdf_epsg(Path(boundary_path))

    with rasterio.open(dem_path) as src:
        dem_epsg = src.crs.to_epsg() if src.crs else None
    # CRS the DEM is effectively in (assume the boundary's if undeclared)
    effective_epsg = dem_epsg or boundary_epsg
    if effective_epsg is None:
        raise ValueError(
            f"Neither {dem_path} nor {boundary_path} declares a CRS; "
            "set target_epsg explicitly."
        )

    src_path = dem_path
    out_epsg = effective_epsg
    if target_epsg and target_epsg != effective_epsg:
        src_path = _reproject_raster(
            dem_path, out_path.with_suffix(".reproj.tif"), target_epsg,
            assume_epsg=effective_epsg,
        )
        out_epsg = target_epsg

    geoms = _load_polygons(Path(boundary_path), out_epsg)
    with rasterio.open(src_path) as src:
        nodata = src.nodata if src.nodata is not None else -9999.0
        out_image, out_transform = mask(
            src, geoms, crop=True, nodata=nodata, filled=True, all_touched=all_touched
        )
        meta = src.meta.copy()
    meta.update(
        height=out_image.shape[1], width=out_image.shape[2], transform=out_transform,
        nodata=nodata, crs=CRS.from_epsg(out_epsg),
    )
    if compress:
        meta["compress"] = compress
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(out_image)
    # tidy any temporary reprojected raster
    if src_path != dem_path and Path(src_path).exists():
        Path(src_path).unlink()
    return out_path


def clip_dem_to_roi(cfg: Config, dem_path, out_path=None) -> Path:
    """Clip *dem_path* to the configuration's ROI boundary, in the project CRS.

    Convenience wrapper around :func:`clip_to_roi` that takes the boundary and
    CRS from *cfg*. By default the clipped raster is written next to the source
    as ``<stem>-roi-clip.tif``; pass *out_path* to choose another location. The
    output path is returned.
    """
    dem_path = Path(dem_path)
    if out_path is None:
        out_path = dem_path.with_name(f"{dem_path.stem}-roi-clip.tif")
    return clip_to_roi(dem_path, cfg.inputs.boundary, out_path, target_epsg=cfg.crs_epsg)


def clip_dem(cfg: Config, dem_path: Path, out_name: str) -> ClippedDEM:
    """Pipeline wrapper: clip *dem_path* to the ROI, reprojected to the project CRS."""
    import rasterio

    cfg.ensure_dirs()
    out_path = clip_to_roi(
        dem_path, cfg.inputs.boundary, Path(cfg.work_dir) / out_name,
        target_epsg=cfg.crs_epsg,
    )
    with rasterio.open(out_path) as src:
        nodata = src.nodata if src.nodata is not None else -9999.0
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
