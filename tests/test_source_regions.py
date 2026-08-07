"""Source-region steering and USER_RAIN percolation-fortran emission.

Validates (no solver):

* ``steering.write_source_regions`` writes the TELEMAC ``SOURCE REGIONS DATA
  FILE`` format (``X(i) Y(i)`` header per region, one vertex pair per line,
  ``#`` separators) and round-trips the region count / vertex counts;
* ``steering.write_cas`` emits the region keyword block (SOURCE REGIONS DATA
  FILE + MAXIMUM NUMBER OF SOURCES + WATER DISCHARGE OF SOURCES + TYPE OF
  SOURCES 1) and NEVER the point-source coordinates (ABSCISSAE/ORDINATES OF
  SOURCES would shadow the region file in lecdon's precedence chain);
* ``percolation.mode: fortran`` switches the .cas to FORTRAN FILE + RAIN
  keywords with NO region keywords (no double-counting), and
  ``fortran.write_user_fortran`` generates a fixed-form ``user_rain.f`` with the
  region vertices, signed discharges and depth guards baked in;
* ``hydrodynamics.control_of_limits: false`` drops the CONTROL OF LIMITS guard.

Run via pytest: mamba run -n hydromate-env pytest tests/test_source_regions.py
"""

from __future__ import annotations

import re

import pytest
from shapely.geometry import Polygon

from hydromate.boundary import InternalSourceRegion, LiquidBoundary


def _cfg(tmp_path, **overrides):
    from hydromate.config import (
        Boundaries,
        Calibration,
        Config,
        Friction,
        Geodata,
        GroundTruth,
        Hydrodynamics,
        Initialization,
        MeshConfig,
        Morphodynamics,
        Percolation,
        TelemacEnv,
    )

    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    dummy = tmp_path / "dummy"
    dummy.write_text("")
    return Config(
        name="source-region-test", crs_epsg=25832, config_dir=tmp_path,
        preprocessing_dir=tmp_path, model_dir=model, postprocessing_dir=tmp_path,
        calibration_dir=tmp_path,
        telemac=TelemacEnv(pysource=dummy),
        geodata=Geodata(dem_initial=dummy, boundary=dummy),
        boundaries=Boundaries(liquid_boundaries=dummy, outflow_condition="elevation",
                              prescribed_elevation=815.1),
        initialization=Initialization(),
        mesh=MeshConfig(), friction=Friction(),
        hydrodynamics=overrides.pop("hydrodynamics", Hydrodynamics()),
        morphodynamics=Morphodynamics(), ground_truth=GroundTruth(),
        calibration=Calibration(),
        gain_lose=overrides.pop("percolation", Percolation(faces="lines", )),
    )


def _regions():
    lose = Polygon([(0, 0), (30, 0), (30, 3), (0, 3)])
    gain = Polygon([(0, 50), (45, 50), (45, 53), (0, 53), (-1, 51)])
    return [
        InternalSourceRegion(name="int-outflow-lose", discharge=-0.065,
                             polygon=lose, area=lose.area, n_nodes=222),
        InternalSourceRegion(name="int-inflow-gain", discharge=0.065,
                             polygon=gain, area=gain.area, n_nodes=332),
    ]


def _liquids():
    return [LiquidBoundary(1, "inflow", 24, discharge=0.8),
            LiquidBoundary(2, "outflow", 29),
            LiquidBoundary(3, "inflow", 34, discharge=1.6)]


def test_write_source_regions_roundtrip(tmp_path):
    from hydromate import steering

    cfg = _cfg(tmp_path)
    regions = _regions()
    path = steering.write_source_regions(cfg, regions)
    assert path.name == cfg.source_regions_file
    text = path.read_text()

    # one X(i) Y(i) header per region, in order
    headers = re.findall(r"^X\((\d)\)\s+Y\(\d\)$", text, flags=re.MULTILINE)
    assert headers == ["1", "2"]
    # vertex pairs per region match the polygon outlines (closing vertex dropped)
    blocks = re.split(r"^X\(\d\)\s+Y\(\d\)$", text, flags=re.MULTILINE)[1:]
    for region, block in zip(regions, blocks):
        pairs = [ln for ln in block.splitlines()
                 if ln and not ln.startswith("#")]
        assert len(pairs) == len(region.polygon.exterior.coords) - 1
        assert all(len(p.split()) == 2 for p in pairs)


def test_cas_region_keywords(tmp_path):
    from hydromate import steering

    cfg = _cfg(tmp_path)
    regions = _regions()
    cas = steering.write_cas(cfg, _liquids(), inflow_q=2.4, outflow_wse=815.1,
                             turbulence_model=6, source_regions=regions)
    txt = cas.read_text()

    assert f"SOURCE REGIONS DATA FILE : {cfg.source_regions_file}" in txt
    assert "MAXIMUM NUMBER OF SOURCES : 2" in txt
    assert "WATER DISCHARGE OF SOURCES : -0.065;0.065" in txt
    assert "TYPE OF SOURCES : 1" in txt
    # vertex budget covers the largest outline (>= dico default 10)
    m = re.search(r"MAXIMUM NUMBER OF POINTS FOR SOURCES REGIONS : (\d+)", txt)
    assert m and int(m.group(1)) >= 10
    # the coordinate route would SHADOW the region file (lecdon precedence)
    assert "ABSCISSAE OF SOURCES" not in txt
    assert "ORDINATES OF SOURCES" not in txt
    # no fortran/rain leakage in region mode
    assert "FORTRAN FILE" not in txt and "RAIN OR EVAPORATION" not in txt
    # the SA turbulence solve budget is raised (default 50 is the GRACJG storm)
    assert "MAXIMUM NUMBER OF ITERATIONS FOR K AND EPSILON :" in txt


def test_cas_without_regions_has_no_source_keywords(tmp_path):
    from hydromate import steering

    cfg = _cfg(tmp_path)
    cas = steering.write_cas(cfg, _liquids(), inflow_q=2.4, outflow_wse=815.1,
                             turbulence_model=6)
    txt = cas.read_text()
    assert "SOURCES" not in txt   # no source block at all


def test_cas_fortran_mode(tmp_path):
    from hydromate import steering
    from hydromate.config import Percolation

    zone = tmp_path / "zone.gpkg"
    zone.write_text("")   # existence is all Percolation.validate checks here
    cfg = _cfg(tmp_path, percolation=Percolation(faces="lines", zone=zone, mode="fortran"))
    cas = steering.write_cas(cfg, _liquids(), inflow_q=2.4, outflow_wse=815.1,
                             turbulence_model=6, source_regions=_regions())
    txt = cas.read_text()

    assert f"FORTRAN FILE : '{cfg.user_fortran_dir}'" in txt
    assert "RAIN OR EVAPORATION : YES" in txt
    assert "RAIN OR EVAPORATION IN MM PER DAY : 0." in txt
    # USER_RAIN carries the whole exchange - regions would double-count it
    assert "SOURCE REGIONS DATA FILE" not in txt
    assert "WATER DISCHARGE OF SOURCES" not in txt


def test_fortran_template(tmp_path):
    from hydromate import fortran
    from hydromate.config import Percolation

    zone = tmp_path / "zone.gpkg"
    zone.write_text("")
    cfg = _cfg(tmp_path, percolation=Percolation(faces="lines", zone=zone, mode="fortran",
                                                 min_depth=0.05, taper_depth=0.05))
    out_dir = fortran.write_user_fortran(cfg, _regions())
    assert out_dir.name == cfg.user_fortran_dir
    src = (out_dir / "user_rain.f").read_text()
    lines = src.splitlines()

    # fixed-form: statements within 72 columns, continuation marker in column 6
    for ln in lines:
        if not ln.startswith("!"):
            assert len(ln) <= 72, f"line exceeds 72 columns: {ln!r}"
    assert any(ln.startswith("     &") for ln in lines), "continuation expected"

    assert "SUBROUTINE USER_RAIN" in src
    # signed target discharges and both polygons' vertices are baked in
    assert "HMP_QTG(1)=-0.065D0" in src
    assert "HMP_QTG(2)=0.065D0" in src
    n_lose = len(_regions()[0].polygon.exterior.coords) - 1
    assert src.count("HMP_XV(") >= n_lose
    # depth guards
    assert "HMP_HMN = 0.05D0" in src and "HMP_TAP = 0.05D0" in src
    assert "PLUIE%R(HMP_I)=-HMP_RAT" in src    # withdrawal is depth-capped
    assert "IF(NCSIZE.GT.1) HMP_QEX=P_SUM(HMP_QEX)" in src  # parallel mass closure

    # every local symbol must be HMP_-prefixed: USE DECLARATIONS_TELEMAC2D pulls in
    # a huge namespace and plain names (NREG, MAXV, HMIN, F, DEJA, I, RATE ...)
    # collide with it, which is a hard compile error. Guard the convention here so
    # the generator cannot regress without the (optional) compile test below.
    import re
    decl = [ln for ln in lines if re.search(r"^\s+(INTEGER|DOUBLE PRECISION|LOGICAL)", ln)]
    assert decl, "expected declarations"
    for ln in decl:
        names = re.findall(r"\b([A-Z][A-Z0-9_]*)\s*(?:=|\(|,|$)", ln.split("::")[-1])
        for n in names:
            if n in ("D0", "TRUE", "FALSE", "NPOIN"):
                continue
            assert n.startswith("HMP_"), f"unprefixed local {n!r} in: {ln!r}"

    # a lose-only or gain-only setup is a config error
    with pytest.raises(ValueError, match="losing and one gaining"):
        fortran.write_user_fortran(cfg, _regions()[:1])


def _patch_zone(tmp_path, poly=None):
    """A real one-polygon percolation-zone layer (patch_drain reads its geometry)."""
    import geopandas as gpd

    poly = poly or Polygon([(-5, -5), (60, -5), (60, 60), (-5, 60)])
    path = tmp_path / "zone.gpkg"
    gpd.GeoDataFrame({"Patch name": ["main-side"], "porous depth (m)": [0.5]},
                     geometry=[poly], crs="EPSG:25832").to_file(path, driver="GPKG")
    return path


def test_fortran_patch_drain_adds_region_last(tmp_path):
    """``percolation.patch_drain`` adds the patch as an EXTRA losing region.

    It must come last, so every node of the prescribed losing / gaining strips keeps
    its own region (each node joins the first region that contains it) and the
    prescribed exchange is not double-counted; its extraction must feed the same
    reinjection total, so the routine stays mass-exact.
    """
    from hydromate import fortran
    from hydromate.config import Percolation

    zone = _patch_zone(tmp_path)
    cfg = _cfg(tmp_path, percolation=Percolation(faces="lines", zone=zone, mode="fortran",
                                                 patch_drain=True))
    src = (fortran.write_user_fortran(cfg, _regions()) / "user_rain.f").read_text()

    # three regions: prescribed losing (1), gaining (2), drain (3) - in that order
    assert "HMP_NRG = 3" in src
    assert f"HMP_KND(1)={fortran.KIND_PRESCRIBED}" in src
    assert f"HMP_KND(2)={fortran.KIND_GAINING}" in src
    assert f"HMP_KND(3)={fortran.KIND_DRAIN}" in src
    assert "HMP_QTG(3)=0D0" in src or "HMP_QTG(3)=0.D0" in src  # a drain has no target
    # the drain feeds the same extracted total that the gaining region reinjects
    assert "HMP_QEX=HMP_QEX+HMP_RAT*VOLU2D%R(HMP_I)" in src
    assert f"IF(HMP_KND(HMP_R).EQ.{fortran.KIND_DRAIN})" in src
    assert "HMP_QDR=HMP_QDR+HMP_RAT*VOLU2D%R(HMP_I)" in src
    # ... and is still depth-limited against the taper floor, so it cannot dry a cell
    assert "0.5D0*MAX(HN%R(HMP_I)-HMP_FLO,0.D0)/DT)" in src
    assert "HMP_FLO=HMP_HMN" in src        # the floor is min_depth without a table

    for ln in src.splitlines():
        if not ln.startswith("!"):
            assert len(ln) <= 72, f"line exceeds 72 columns: {ln!r}"


def test_patch_drain_rate_is_capped_in_both_exchange_modes(tmp_path):
    """``patch_drain_max_rate`` must bind whether the exchange is prescribed or
    Green-Ampt driven: the Green-Ampt rate applies over the whole wet patch, so
    without the cap the drain keeps drawing from the (genuinely wet) patch toe."""
    from hydromate import fortran
    from hydromate.config import Percolation

    zone = _patch_zone(tmp_path)
    for conductivity in (None, 3.0e-4):
        cfg = _cfg(tmp_path, percolation=Percolation(faces="lines", 
            zone=zone, mode="fortran", patch_drain=True,
            patch_drain_max_rate=2.0e-5, conductivity=conductivity))
        src = (fortran.write_user_fortran(cfg, _regions()) / "user_rain.f").read_text()
        literal = re.search(r"HMP_DRN = (\S+)", src)
        assert literal, src
        assert float(literal.group(1).replace("D", "E")) == 2.0e-5
        drain = src.split(f"ELSEIF(HMP_KND(HMP_R).EQ.{fortran.KIND_DRAIN}) THEN")[1] \
                   .split("ENDIF")[0]
        assert "HMP_DRN" in drain, f"cap not applied (conductivity={conductivity})"


def test_fortran_patch_drain_skipped_when_patch_is_the_losing_region(tmp_path):
    """With ``losing_region: patch`` the prescribed withdrawal already covers the
    patch, so a drain region would double up on it."""
    from hydromate import fortran
    from hydromate.config import Percolation

    zone = _patch_zone(tmp_path)
    cfg = _cfg(tmp_path, percolation=Percolation(faces="lines", zone=zone, mode="fortran",
                                                 losing_region="patch",
                                                 patch_drain=True))
    assert fortran.patch_drain_regions(cfg, []) == []
    src = (fortran.write_user_fortran(cfg, _regions()) / "user_rain.f").read_text()
    assert "HMP_NRG = 2" in src


def test_patch_drain_requires_fortran_mode(tmp_path):
    from hydromate.config import Percolation

    zone = _patch_zone(tmp_path)
    with pytest.raises(ValueError, match="patch_drain needs gain_lose.mode: fortran"):
        Percolation(faces="lines", zone=zone, mode="region", patch_drain=True).validate()


def _plane():
    from hydromate.watertable import PhreaticPlane

    return PhreaticPlane(c0=817.0, cx=-0.005, cy=0.001, x0=100.0, y0=200.0,
                         levels={"losing": 817.3, "gaining": 816.8}, residual=0.02)


def test_water_table_becomes_the_drain_taper_floor(tmp_path):
    """With a water table the drain tapers to zero AT THE TABLE, not at min_depth,
    so it clears the bar top but cannot empty a pool cutting below the saturated
    zone. The plane is five numbers, so no per-node array is needed."""
    from hydromate import fortran
    from hydromate.config import Percolation

    cfg = _cfg(tmp_path, percolation=Percolation(faces="lines", 
        zone=_patch_zone(tmp_path), mode="fortran", patch_drain=True,
        water_table="phreatic"))
    src = (fortran.write_user_fortran(cfg, _regions(), plane=_plane())
           / "user_rain.f").read_text()

    for name, value in (("HMP_WC0", 817.0), ("HMP_WCX", -0.005), ("HMP_WCY", 0.001),
                        ("HMP_WX0", 100.0), ("HMP_WY0", 200.0)):
        m = re.search(rf"{name} = (\S+)", src)
        assert m, f"{name} not baked in"
        assert float(m.group(1).replace("D", "E")) == pytest.approx(value)
    # the floor is min_depth by default and the table only on the DRAIN region
    assert "HMP_FLO=HMP_HMN" in src
    assert "HMP_FLO=MAX(HMP_HMN,HMP_ZWT-ZF%R(HMP_I))" in src
    assert f"IF(HMP_KND(HMP_R).EQ.{fortran.KIND_DRAIN}) THEN" in src
    # ... and both the taper and the per-step cap use it
    assert "(HN%R(HMP_I)-HMP_FLO)/HMP_TAP))" in src
    assert "0.5D0*MAX(HN%R(HMP_I)-HMP_FLO,0.D0)/DT)" in src


def test_water_table_absent_without_a_drain_region(tmp_path):
    """No drain region means nothing consults the table, so it is not emitted."""
    from hydromate import fortran
    from hydromate.config import Percolation

    cfg = _cfg(tmp_path, percolation=Percolation(faces="lines", 
        zone=_patch_zone(tmp_path), mode="fortran", water_table="phreatic"))
    src = (fortran.write_user_fortran(cfg, _regions(), plane=_plane())
           / "user_rain.f").read_text()
    assert "HMP_ZWT" not in src


def test_film_infiltration_is_gated_and_reinjected(tmp_path):
    """The film term applies OUTSIDE every exchange region, is gated on depth AND
    velocity, and feeds the same total the gaining line reinjects - so it removes
    the film without opening a net sink that would floor the flux imbalance."""
    from hydromate import fortran
    from hydromate.config import Drying, Percolation

    perc = Percolation(faces="lines", zone=_patch_zone(tmp_path), mode="fortran", patch_drain=True)
    cfg = _cfg(tmp_path, percolation=perc)
    cfg.drying = Drying(film_infiltration=True, film_depth=0.01,
                        film_velocity=0.005, film_rate=1.0e-5)
    src = (fortran.write_user_fortran(cfg, _regions()) / "user_rain.f").read_text()

    assert "HMP_SPD=SQRT(UN%R(HMP_I)**2+VN%R(HMP_I)**2)" in src
    assert "HN%R(HMP_I).LT.HMP_FLD" in src and ".AND.HMP_SPD.LT.HMP_FLV" in src
    # it lives in the ELSE of "node belongs to a region", i.e. outside them all
    outside = src.split("ELSE")[-1]
    assert "HMP_QFI=HMP_QFI+HMP_RAT*VOLU2D%R(HMP_I)" in outside
    # the film feeds HMP_QEX, which is what the gaining region reinjects
    assert outside.count("HMP_QEX=HMP_QEX+HMP_RAT*VOLU2D%R(HMP_I)") == 1
    assert "PLUIE%R(HMP_I)=HMP_QEX*(HMP_QTG(HMP_R)/HMP_QGS)" in src

    # off by default: no film symbols at all
    cfg.drying = Drying()
    plain = (fortran.write_user_fortran(cfg, _regions()) / "user_rain.f").read_text()
    assert "HMP_QFI" not in plain and "HMP_FLD" not in plain


def test_film_infiltration_requires_fortran_percolation():
    from hydromate.config import Drying, Percolation

    with pytest.raises(ValueError, match="needs a gain-lose reach"):
        Drying(film_infiltration=True).validate(Percolation(faces="lines", mode="off"))
    # ... and a region-mode reach is equally unable to carry the term
    with pytest.raises(ValueError, match="needs a gain-lose reach"):
        Drying(film_infiltration=True).validate(
            Percolation(faces="lines", enabled=True, mode="region"))


def test_fortran_compiles(tmp_path):
    """The generated USER_RAIN must actually compile against TELEMAC's modules.

    Skipped where gfortran or a TELEMAC build is unavailable; where both exist this
    is the only check that catches namespace collisions with
    DECLARATIONS_TELEMAC2D (which cost a wasted solver run once).
    """
    import shutil
    import subprocess
    from pathlib import Path

    from hydromate import fortran
    from hydromate.config import Percolation

    if not shutil.which("gfortran"):
        pytest.skip("gfortran not available")
    mods = [p for p in Path("/home/modelling").glob("**/builds/*/modules")
            if (p / "declarations_telemac2d.mod").exists()]
    if not mods:
        pytest.skip("no compiled TELEMAC modules found")

    # every emitted variant: prescribed / Green-Ampt exchange, with and without the
    # patch drain (each takes a different branch of the generator)
    from hydromate.config import Drying

    variants = {
        # (percolation, water-table plane, drying)
        "prescribed": (Percolation(faces="lines", zone=tmp_path / "empty.gpkg", mode="fortran"),
                       None, Drying()),
        "greenampt": (Percolation(faces="lines", zone=tmp_path / "empty.gpkg", mode="fortran",
                                  conductivity=3.0e-4), None, Drying()),
        "prescribed+drain": (Percolation(faces="lines", zone=_patch_zone(tmp_path), mode="fortran",
                                         patch_drain=True), None, Drying()),
        "greenampt+drain": (Percolation(faces="lines", zone=_patch_zone(tmp_path), mode="fortran",
                                        conductivity=3.0e-4, patch_drain=True),
                            None, Drying()),
        "drain+watertable": (Percolation(faces="lines", zone=_patch_zone(tmp_path), mode="fortran",
                                         conductivity=3.0e-4, patch_drain=True,
                                         water_table="phreatic"),
                             _plane(), Drying()),
        "everything": (Percolation(faces="lines", zone=_patch_zone(tmp_path), mode="fortran",
                                   conductivity=3.0e-4, patch_drain=True,
                                   water_table="phreatic"),
                       _plane(), Drying(film_infiltration=True)),
        "film-only": (Percolation(faces="lines", zone=_patch_zone(tmp_path), mode="fortran"),
                      None, Drying(film_infiltration=True)),
    }
    (tmp_path / "empty.gpkg").write_text("")
    for label, (perc, plane, drying) in variants.items():
        case = tmp_path / label.replace("+", "-")
        case.mkdir(exist_ok=True)
        cfg = _cfg(tmp_path, percolation=perc)
        cfg.drying = drying
        cfg.model_dir = case
        out = fortran.write_user_fortran(cfg, _regions(), plane=plane) / "user_rain.f"
        proc = subprocess.run(
            ["gfortran", "-fsyntax-only", "-ffixed-form", "-ffixed-line-length-72",
             "-I", str(mods[0]), str(out)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, \
            f"generated Fortran ({label}) does not compile:\n{proc.stderr}"


def test_control_of_limits_toggle(tmp_path):
    from hydromate import steering
    from hydromate.config import Hydrodynamics

    cfg = _cfg(tmp_path)
    txt = steering.write_cas(cfg, _liquids(), inflow_q=2.4, outflow_wse=815.1,
                             turbulence_model=6).read_text()
    assert "CONTROL OF LIMITS : YES" in txt and "LIMIT VALUES :" in txt

    cfg2 = _cfg(tmp_path, hydrodynamics=Hydrodynamics(control_of_limits=False))
    txt2 = steering.write_cas(cfg2, _liquids(), inflow_q=2.4, outflow_wse=815.1,
                              turbulence_model=6).read_text()
    assert "CONTROL OF LIMITS : YES" not in txt2 and "LIMIT VALUES :" not in txt2
