"""DEM-of-Difference (DoD) with a minimum level of detection (LoD).

Checks the propagated-uncertainty LoD maths and that :func:`axqua.dem.dem_of_difference`
thresholds the bed-change raster: changes below the LoD are masked to nodata (or set to
0), supra-LoD changes are kept.
"""

import math

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")


def _minimal_cfg(tmp_path, **dod_kwargs):
    from axqua.config import (
        Boundaries, Calibration, Config, DemOfDifference, Friction, Geodata,
        GroundTruth, Hydrodynamics, Initialization, MeshConfig, Morphodynamics,
        TelemacEnv,
    )

    dummy = tmp_path / "dummy"
    dummy.write_text("")
    return Config(
        name="dod-test", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=tmp_path, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path, telemac=TelemacEnv(pysource=dummy),
        geodata=Geodata(dem_initial=dummy, boundary=dummy),
        boundaries=Boundaries(liquid_boundaries=dummy),
        initialization=Initialization(), mesh=MeshConfig(), friction=Friction(),
        hydrodynamics=Hydrodynamics(), morphodynamics=Morphodynamics(),
        ground_truth=GroundTruth(), calibration=Calibration(),
        dem_of_difference=DemOfDifference(enabled=True, **dod_kwargs),
    )


def _write_raster(path, array, nodata=-9999.0, res=1.0):
    from rasterio.transform import from_origin

    array = np.asarray(array, dtype="float32")
    ny, nx = array.shape
    transform = from_origin(0.0, ny * res, res, res)   # north-up
    with rasterio.open(
        path, "w", driver="GTiff", height=ny, width=nx, count=1, dtype="float32",
        crs="EPSG:25832", transform=transform, nodata=nodata,
    ) as dst:
        dst.write(array, 1)
    return path


def test_propagated_lod_matches_formula():
    from axqua.dem import propagated_lod

    lod = propagated_lod(0.10, 0.05, confidence_level=0.95)
    assert lod == pytest.approx(1.959963985 * math.hypot(0.10, 0.05), rel=1e-6)
    # a tighter confidence gives a larger threshold
    assert propagated_lod(0.10, 0.05, 0.99) > lod


def _diff_field():
    """20x20 target - initial field: a +0.5 m block (supra-LoD), a +0.05 m block
    (sub-LoD), zero elsewhere. minLoD at 95% for (0.1, 0.05) is ~0.219 m."""
    delta = np.zeros((20, 20), dtype="float32")
    delta[2:6, 2:6] = 0.5      # significant deposition
    delta[12:16, 12:16] = 0.05  # below detection
    return delta


def test_dod_masks_below_lod(tmp_path):
    from axqua.dem import ClippedDEM, dem_of_difference

    base = np.full((20, 20), 100.0, dtype="float32")
    delta = _diff_field()
    ini = ClippedDEM(_write_raster(tmp_path / "ini.tif", base), nodata=-9999.0)
    tgt = ClippedDEM(_write_raster(tmp_path / "tgt.tif", base + delta), nodata=-9999.0)

    cfg = _minimal_cfg(tmp_path, uncertainty_initial=0.10, uncertainty_target=0.05,
                       confidence_level=0.95, mask_below_lod=True)
    out = dem_of_difference(cfg, ini, tgt)

    with rasterio.open(out) as src:
        dz = src.read(1)
        nod = src.nodata
    # supra-LoD block kept at its true value
    assert dz[3, 3] == pytest.approx(0.5, abs=1e-3)
    # sub-LoD block masked to nodata
    assert dz[13, 13] == pytest.approx(nod)
    # unchanged cells (|dz|=0 < LoD) also masked
    assert dz[0, 0] == pytest.approx(nod)


def test_dod_zero_below_lod_when_not_masking(tmp_path):
    from axqua.dem import ClippedDEM, dem_of_difference

    base = np.full((20, 20), 100.0, dtype="float32")
    delta = _diff_field()
    ini = ClippedDEM(_write_raster(tmp_path / "ini.tif", base), nodata=-9999.0)
    tgt = ClippedDEM(_write_raster(tmp_path / "tgt.tif", base + delta), nodata=-9999.0)

    cfg = _minimal_cfg(tmp_path, uncertainty_initial=0.10, uncertainty_target=0.05,
                       mask_below_lod=False)
    out = dem_of_difference(cfg, ini, tgt)

    with rasterio.open(out) as src:
        dz = src.read(1)
    assert dz[3, 3] == pytest.approx(0.5, abs=1e-3)   # significant kept
    assert dz[13, 13] == pytest.approx(0.0, abs=1e-6)  # sub-LoD set to 0, not nodata


def test_explicit_min_lod_overrides_propagated(tmp_path):
    from axqua.dem import ClippedDEM, dem_of_difference, resolve_lod

    cfg = _minimal_cfg(tmp_path, uncertainty_initial=0.10, uncertainty_target=0.05,
                       min_lod=0.4)
    lod, _ = resolve_lod(cfg)
    assert lod == pytest.approx(0.4)

    base = np.full((20, 20), 100.0, dtype="float32")
    ini = ClippedDEM(_write_raster(tmp_path / "ini.tif", base), nodata=-9999.0)
    tgt = ClippedDEM(_write_raster(tmp_path / "tgt.tif", base + _diff_field()),
                     nodata=-9999.0)
    out = dem_of_difference(cfg, ini, tgt)
    with rasterio.open(out) as src:
        dz = src.read(1)
        nod = src.nodata
    # 0.5 > 0.4 kept; the 0.05 block masked
    assert dz[3, 3] == pytest.approx(0.5, abs=1e-3)
    assert dz[13, 13] == pytest.approx(nod)
