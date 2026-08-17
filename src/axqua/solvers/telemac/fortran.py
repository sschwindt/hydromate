"""Generated TELEMAC user-Fortran for percolation (``percolation.mode: fortran``).

Emits a ``USER_RAIN`` routine (the TELEMAC-2D hook for spatialised rain /
infiltration, called from ``prosou.f`` whenever ``RAIN OR EVAPORATION : YES``)
that models the losing-gaining exchange of a porous patch **depth-limited**:

* over each *losing* region (the percolation patch polygon) it withdraws the
  target discharge as a uniform depth rate ``Q/area``, tapered linearly to zero
  as the local depth approaches ``percolation.min_depth`` and additionally capped
  at half the water available above that floor per time step - so the sink can
  **never dry a cell** (TELEMAC's own source terms have no such guard, which is
  what collapses the CFL-adaptive time step when a sink node dries);
* the discharge **actually** extracted (which may be less than the target while
  the patch is still shallow) is summed - in parallel via ``P_SUM``, using the
  same partial-``VOLU2D`` idiom as ``telemac2d_init.F`` - and reinjected exactly
  over the *gaining* region(s), so the exchange is mass-exact by construction;
* ``PLUIE`` is *assigned*, not accumulated, so the double ``CALL USER_RAIN`` in
  ``prosou.f`` (once inside the RAIN block, once unconditionally) is harmless.

The generated file lands in ``<model_dir>/<user_fortran_dir>/user_rain.f`` and is
referenced by ``FORTRAN FILE`` in the ``.cas`` (see :func:`steering.write_cas`);
TELEMAC compiles it into the executable at run time.
"""

from __future__ import annotations

import logging
from pathlib import Path

from axqua.config import Config

log = logging.getLogger("axqua")

FIXED_FORM_LIMIT = 72   # fixed-form Fortran statement field ends at column 72


def _dbl(value: float) -> str:
    """Format a double-precision Fortran literal (D exponent)."""
    text = f"{value:.10G}"
    if "E" in text:
        return text.replace("E", "D")
    return text + "D0"


#: region kinds dispatched inside the generated routine
KIND_PRESCRIBED = 1   # losing: deliver the target discharge over the wet region
KIND_DRAIN = 2        # losing: drain whatever stands here, no target discharge
KIND_GAINING = 3      # inject the total that was extracted


def patch_drain_regions(cfg: Config, used: list) -> list:
    """Percolation patches to drain, as extra :class:`InternalSourceRegion` regions.

    Standing surface water on a porous gravel bar infiltrates instead of ponding, so
    it is an artefact of a 2D model that has no subsurface. With
    ``percolation.losing_region: line`` the prescribed exchange is taken from the
    channel at the losing line and nothing removes water sitting on the bar; these
    regions do (see ``percolation.patch_drain``).

    Returns an empty list when the drain is off, or when the patch is already the
    losing region (``losing_region: patch``), where the prescribed withdrawal
    already covers it.
    """
    from axqua.solvers.telemac.boundary import (
        InternalSourceRegion, _percolation_patches, _simplify_region,
    )

    if not cfg.percolation.patch_drain:
        return []
    if cfg.percolation.losing_region == "patch":
        log.info("  percolation.patch_drain ignored: the patch is already the "
                 "losing region, so the prescribed withdrawal covers it")
        return []
    drains = []
    for patch in _percolation_patches(cfg):
        polygon, _ = _simplify_region(patch["geom"])
        drains.append(InternalSourceRegion(
            name=f"drain:{patch['name']}",
            discharge=0.0,          # a drain has no target - it takes what is there
            polygon=polygon,
            area=float(polygon.area),
            porous_depth=patch["porous_depth"],
        ))
    if drains:
        log.info("  percolation patch drain: %d patch(es), %.0f m2 total - standing "
                 "surface water is infiltrated and reinjected at the gaining line",
                 len(drains), sum(r.area for r in drains))
    return drains


def write_user_fortran(cfg: Config, regions: list, *, plane=None) -> Path:
    """Generate ``user_fortran/user_rain.f`` for the given source regions.

    *regions* is the :func:`axqua.boundary.load_internal_source_regions` list
    (signed discharges; in fortran mode the losing region polygon is the
    percolation patch). *plane* is an optional
    :class:`axqua.watertable.PhreaticPlane`: when given, the patch drain tapers
    to zero at the water table instead of at ``percolation.min_depth``, so it cannot
    empty a pool that cuts below the bar's saturated zone. A plane is five numbers,
    so it is baked in as coefficients - no per-node array is needed.

    Returns the user-fortran directory (the ``FORTRAN FILE`` target).
    """
    from axqua.solvers.telemac.steering import _region_coords

    if cfg.gain_lose.active and cfg.gain_lose.faces == "water-table":
        return write_water_table_fortran(cfg, plane)

    losing = [r for r in regions if r.discharge < 0]
    gaining = [r for r in regions if r.discharge > 0]
    film = cfg.drying.film_infiltration
    if not losing or not gaining:
        raise ValueError(
            "percolation.mode: fortran needs at least one losing and one gaining "
            f"internal line (got {len(losing)} losing / {len(gaining)} gaining)."
        )
    # ORDER MATTERS: each node is assigned to the FIRST region that contains it, and
    # the patch overlaps both the losing strip and the gaining strip. Putting the
    # drain last therefore leaves the prescribed exchange untouched and drains only
    # the rest of the patch - no double counting.
    drains = patch_drain_regions(cfg, losing)
    ordered = losing + gaining + drains
    kinds = ([KIND_PRESCRIBED] * len(losing) + [KIND_GAINING] * len(gaining)
             + [KIND_DRAIN] * len(drains))
    coords = [_region_coords(r) for r in ordered]
    n_reg = len(ordered)
    max_v = max(len(c) for c in coords)
    hmin = float(cfg.percolation.min_depth)
    htap = max(float(cfg.percolation.taper_depth), 1e-6)
    rmax = float(cfg.percolation.max_rate)
    drain_rate = float(cfg.percolation.patch_drain_max_rate
                       if cfg.percolation.patch_drain_max_rate is not None else rmax)
    diag_every = 500   # report the delivered discharge every N calls
    conductivity = cfg.percolation.conductivity
    suction = float(cfg.percolation.suction)
    # porous layer thickness: the losing patch's own attribute, else a drained
    # patch's, else the config fallback
    porous_depth = next((r.porous_depth for r in regions
                         if r.discharge < 0 and r.porous_depth), None)
    if porous_depth is None:
        porous_depth = next((r.porous_depth for r in drains if r.porous_depth), None)
    if porous_depth is None:
        porous_depth = float(cfg.percolation.porous_depth)
    use_table = plane is not None and bool(drains)
    film_depth = float(cfg.drying.film_depth)
    film_velocity = float(cfg.drying.film_velocity)
    film_rate = float(cfg.drying.film_rate)

    body: list[str] = []

    def stmt(text: str) -> None:
        line = "      " + text
        if len(line) > FIXED_FORM_LIMIT:   # pragma: no cover - guarded by tests
            raise ValueError(f"generated Fortran line exceeds 72 columns: {line!r}")
        body.append(line)

    def comment(text: str) -> None:
        body.append(f"!     {text}" if text else "!")

    comment("axqua-generated USER_RAIN: percolation through a porous patch")
    if conductivity is None:
        comment("  exchange: PRESCRIBED discharge (from the int-* flow column)")
    else:
        comment("  exchange: COMPUTED from Green-Ampt's saturated limit")
        comment(f"    f = kf*(h + Lz + hf)/Lz  with kf={conductivity:g} m/s,")
        comment(f"    Lz={porous_depth:g} m (porous depth), hf={suction:g} m (suction)")
        comment("    -> the exchange RESPONDS to water level; it is not fixed.")
    labels = {KIND_PRESCRIBED: "losing", KIND_DRAIN: "drain", KIND_GAINING: "gaining"}
    for r, kind in zip(ordered, kinds):
        target = ("drains standing water" if kind == KIND_DRAIN
                  else f"target {r.discharge:+g} m3/s")
        comment(f"  region {labels[kind]}: {r.name}  {target}  ~{r.area:.0f} m2")
    if drains:
        comment("  the drain region is LAST, so nodes of the prescribed losing and")
        comment("  gaining strips keep their own region: no double counting.")
        if conductivity is None:
            comment(f"    drain rate: capped at {drain_rate:g} m/s (no conductivity)")
    if use_table:
        comment("  WATER TABLE (phreatic plane through the two channel levels):")
        for key, level in sorted(plane.levels.items()):
            comment(f"    {key} line at {level:.3f} m")
        comment(f"    gradient {1000 * plane.gradient:.2f} permille, "
                f"fit residual {plane.residual:.3f} m")
        comment("    the drain tapers to zero AT THE TABLE, so a pool cutting below")
        comment("    the bar's saturated zone keeps its water.")
    if film:
        comment("  FILM INFILTRATION: water shallower than "
                f"{film_depth:g} m AND slower than")
        comment(f"    {film_velocity:g} m/s infiltrates at {film_rate:g} m/s. Such")
        comment("    water sits inside the grain roughness, not over the bed. What")
        comment("    it takes is reinjected at the gaining line (mass-exact), so no")
        comment("    net sink puts a floor under the boundary flux imbalance.")
    comment("depth-limited withdrawal over the losing region(s); the extracted")
    comment("total is reinjected exactly over the gaining region(s) (mass-exact).")
    comment("PLUIE is ASSIGNED (not accumulated): prosou.f calls USER_RAIN twice.")
    body.append("      SUBROUTINE USER_RAIN")
    stmt("USE BIEF")
    stmt("USE DECLARATIONS_TELEMAC2D")
    stmt("USE INTERFACE_PARALLEL, ONLY : P_SUM")
    stmt("USE DECLARATIONS_SPECIAL")
    stmt("IMPLICIT NONE")
    # every local carries the HMP_ prefix: USE DECLARATIONS_TELEMAC2D pulls in a
    # very large namespace, and plain names collide with it (NREG is TELEMAC's own
    # source-region count, MAXV a derived type, and F/HMIN/DEJA are module
    # variables) - such a clash is a hard compile error, so do not shorten these.
    stmt(f"INTEGER, PARAMETER :: HMP_NRG = {n_reg}")
    stmt(f"INTEGER, PARAMETER :: HMP_MXV = {max_v}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_HMN = {_dbl(hmin)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_TAP = {_dbl(htap)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_RMX = {_dbl(rmax)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_DRN = {_dbl(drain_rate)}")
    if use_table:
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WC0 = {_dbl(plane.c0)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WCX = {_dbl(plane.cx)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WCY = {_dbl(plane.cy)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WX0 = {_dbl(plane.x0)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WY0 = {_dbl(plane.y0)}")
    if film:
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_FLD = {_dbl(film_depth)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_FLV = {_dbl(film_velocity)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_FLR = {_dbl(film_rate)}")
    if conductivity is not None:
        # kf is a VARIABLE assigned at run time, not a PARAMETER, so that
        # HydroBayesCal can calibrate it: its `f.` mechanism rewrites the line whose
        # text left of "=" matches the parameter name, which a
        # "DOUBLE PRECISION, PARAMETER :: HMP_KF = ..." declaration would not expose
        # (its first ":" sits in the "::"). See targets.PARAMETER_CATALOG.
        stmt("DOUBLE PRECISION HMP_KF")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_LZ = {_dbl(porous_depth)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_HF = {_dbl(suction)}")
    stmt("INTEGER, SAVE :: HMP_NV(HMP_NRG)")
    stmt("DOUBLE PRECISION, SAVE :: HMP_XV(HMP_MXV,HMP_NRG)")
    stmt("DOUBLE PRECISION, SAVE :: HMP_YV(HMP_MXV,HMP_NRG)")
    stmt("DOUBLE PRECISION, SAVE :: HMP_QTG(HMP_NRG),HMP_ARE(HMP_NRG)")
    stmt("INTEGER, SAVE :: HMP_KND(HMP_NRG)")
    stmt("INTEGER, ALLOCATABLE, SAVE :: HMP_IRG(:)")
    stmt("LOGICAL, SAVE :: HMP_DEJA = .FALSE.")
    stmt("INTEGER, SAVE :: HMP_CNT = 0")
    stmt("INTEGER HMP_I,HMP_R")
    stmt("DOUBLE PRECISION HMP_QEX,HMP_QGS,HMP_RAT,HMP_F,HMP_QA,HMP_WET")
    stmt("DOUBLE PRECISION HMP_QDR,HMP_FLO")
    if use_table:
        stmt("DOUBLE PRECISION HMP_ZWT")
    if film:
        stmt("DOUBLE PRECISION HMP_QFI,HMP_SPD")
    if conductivity is not None:
        comment("")
        comment("calibratable: HydroBayesCal rewrites this line for `f.HMP_KF`")
        stmt(f"HMP_KF = {_dbl(conductivity)}")
    comment("")
    comment("one-off setup: region vertices, node->region map, region areas")
    stmt("IF(.NOT.HMP_DEJA) THEN")
    for i, (region, verts, kind) in enumerate(zip(ordered, coords, kinds), start=1):
        stmt(f"  HMP_NV({i})={len(verts)}")
        stmt(f"  HMP_QTG({i})={_dbl(float(region.discharge))}")
        stmt(f"  HMP_KND({i})={kind}")
        for j, (x, y) in enumerate(verts, start=1):
            stmt(f"  HMP_XV({j},{i})={_dbl(round(x, 3))}")
            stmt(f"  HMP_YV({j},{i})={_dbl(round(y, 3))}")
    stmt("  ALLOCATE(HMP_IRG(NPOIN))")
    stmt("  DO HMP_I=1,NPOIN")
    stmt("    HMP_IRG(HMP_I)=0")
    stmt("    DO HMP_R=1,HMP_NRG")
    stmt("      IF(INPOLY(X(HMP_I),Y(HMP_I),")
    body.append("     &   HMP_XV(1:HMP_NV(HMP_R),HMP_R),")
    body.append("     &   HMP_YV(1:HMP_NV(HMP_R),HMP_R),HMP_NV(HMP_R))) THEN")
    stmt("        HMP_IRG(HMP_I)=HMP_R")
    stmt("        EXIT")
    stmt("      ENDIF")
    stmt("    ENDDO")
    stmt("  ENDDO")
    comment("  region areas: partial VOLU2D summed across subdomains (the")
    comment("  telemac2d_init.F source-region idiom, exact at interface nodes)")
    stmt("  DO HMP_R=1,HMP_NRG")
    stmt("    HMP_ARE(HMP_R)=0.D0")
    stmt("  ENDDO")
    stmt("  DO HMP_I=1,NPOIN")
    stmt("    IF(HMP_IRG(HMP_I).GT.0) THEN")
    stmt("      HMP_ARE(HMP_IRG(HMP_I))=HMP_ARE(HMP_IRG(HMP_I))")
    body.append("     &     +VOLU2D%R(HMP_I)")
    stmt("    ENDIF")
    stmt("  ENDDO")
    stmt("  IF(NCSIZE.GT.1) THEN")
    stmt("    DO HMP_R=1,HMP_NRG")
    stmt("      HMP_ARE(HMP_R)=P_SUM(HMP_ARE(HMP_R))")
    stmt("    ENDDO")
    stmt("  ENDIF")
    stmt("  DO HMP_R=1,HMP_NRG")
    stmt("    IF(HMP_ARE(HMP_R).LE.0.D0) THEN")
    stmt("      WRITE(LU,*) 'USER_RAIN: EMPTY PERCOLATION REGION',HMP_R")
    stmt("      CALL PLANTE(1)")
    stmt("      STOP")
    stmt("    ENDIF")
    stmt("  ENDDO")
    stmt("  HMP_DEJA=.TRUE.")
    stmt("ENDIF")
    comment("")
    comment("PASS 1: how much taper-weighted WET area does the losing patch have?")
    comment("Normalising by the wet area (not the whole polygon) is what lets the")
    comment("full target discharge be delivered when part of the patch is dry - a")
    comment("uniform Q/area over the whole polygon silently loses the dry share.")
    stmt("HMP_WET=0.D0")
    stmt("DO HMP_I=1,NPOIN")
    stmt("  HMP_R=HMP_IRG(HMP_I)")
    stmt("  IF(HMP_R.GT.0) THEN")
    stmt(f"    IF(HMP_KND(HMP_R).EQ.{KIND_PRESCRIBED}) THEN")
    stmt("      HMP_F=MIN(1.D0,MAX(0.D0,")
    body.append("     &   (HN%R(HMP_I)-HMP_HMN)/HMP_TAP))")
    stmt("      HMP_WET=HMP_WET+HMP_F*VOLU2D%R(HMP_I)")
    stmt("    ENDIF")
    stmt("  ENDIF")
    stmt("ENDDO")
    stmt("IF(NCSIZE.GT.1) HMP_WET=P_SUM(HMP_WET)")
    comment("")
    comment("PASS 2: withdraw, normalised over that wet area and capped three ways -")
    comment("by the taper, by an absolute ceiling, and by half the water available")
    comment("above HMP_HMN this step - so a cell can never be drained dry.")
    stmt("HMP_QEX=0.D0")
    stmt("HMP_QDR=0.D0")
    if film:
        stmt("HMP_QFI=0.D0")
    stmt("DO HMP_I=1,NPOIN")
    stmt("  HMP_R=HMP_IRG(HMP_I)")
    stmt("  IF(HMP_R.GT.0) THEN")
    stmt("    HMP_QA=-1.D0")
    comment("    taper floor: min_depth, or the WATER TABLE where it stands higher,")
    comment("    so the drain clears the bar top but cannot empty a pool whose bed")
    comment("    cuts below the bar's saturated zone.")
    stmt("    HMP_FLO=HMP_HMN")
    if use_table:
        stmt(f"    IF(HMP_KND(HMP_R).EQ.{KIND_DRAIN}) THEN")
        stmt("      HMP_ZWT=HMP_WC0+HMP_WCX*(X(HMP_I)-HMP_WX0)")
        body.append("     &       +HMP_WCY*(Y(HMP_I)-HMP_WY0)")
        stmt("      HMP_FLO=MAX(HMP_HMN,HMP_ZWT-ZF%R(HMP_I))")
        stmt("    ENDIF")
    stmt("    HMP_F=MIN(1.D0,MAX(0.D0,")
    body.append("     &   (HN%R(HMP_I)-HMP_FLO)/HMP_TAP))")
    stmt(f"    IF(HMP_KND(HMP_R).EQ.{KIND_PRESCRIBED}")
    body.append("     &     .AND.HMP_WET.GT.0.D0) THEN")
    if conductivity is None:
        stmt("      HMP_QA=-HMP_QTG(HMP_R)*HMP_F/HMP_WET")
    else:
        # Green-Ampt saturated limit: the wetting front sits at the base of the
        # porous layer (thickness HMP_LZ), ponded depth h above it and suction
        # HMP_HF at the front, so the Darcy gradient is (h + Lz + hf)/Lz.
        stmt("      HMP_QA=HMP_KF*(HN%R(HMP_I)+HMP_LZ+HMP_HF)/HMP_LZ")
        stmt("      HMP_QA=HMP_QA*HMP_F")
    stmt(f"    ELSEIF(HMP_KND(HMP_R).EQ.{KIND_DRAIN}) THEN")
    comment("      drain: no target discharge, just infiltrate what stands here,")
    comment("      capped at HMP_DRN (percolation.patch_drain_max_rate) - the")
    comment("      Green-Ampt rate applies over the whole WET patch, so without a")
    comment("      cap the drain keeps drawing from the patch toe (which is real")
    comment("      channel) and not only from the standing water on the bar.")
    if conductivity is None:
        stmt("      HMP_QA=HMP_DRN*HMP_F")
    else:
        stmt("      HMP_QA=HMP_KF*(HN%R(HMP_I)+HMP_LZ+HMP_HF)/HMP_LZ")
        stmt("      HMP_QA=MIN(HMP_QA,HMP_DRN)*HMP_F")
    stmt("    ENDIF")
    stmt("    IF(HMP_QA.GE.0.D0) THEN")
    stmt("      HMP_RAT=MIN(HMP_QA,HMP_RMX,")
    body.append("     &   0.5D0*MAX(HN%R(HMP_I)-HMP_FLO,0.D0)/DT)")
    stmt("      PLUIE%R(HMP_I)=-HMP_RAT")
    stmt("      HMP_QEX=HMP_QEX+HMP_RAT*VOLU2D%R(HMP_I)")
    stmt(f"      IF(HMP_KND(HMP_R).EQ.{KIND_DRAIN})")
    body.append("     &     HMP_QDR=HMP_QDR+HMP_RAT*VOLU2D%R(HMP_I)")
    stmt("    ENDIF")
    if film:
        comment("  outside every exchange region: FILM INFILTRATION. Water thinner")
        comment("  than HMP_FLD and slower than HMP_FLV is not flowing over the bed,")
        comment("  it is standing within the grain roughness, and physically it")
        comment("  drains into the substrate. The velocity gate is what keeps this")
        comment("  off the active wet/dry margin, where flow delivers water.")
        stmt("  ELSE")
        stmt("    HMP_SPD=SQRT(UN%R(HMP_I)**2+VN%R(HMP_I)**2)")
        stmt("    IF(HN%R(HMP_I).GT.0.D0.AND.HN%R(HMP_I).LT.HMP_FLD")
        body.append("     &     .AND.HMP_SPD.LT.HMP_FLV) THEN")
        comment("      no floor here - the point is to take it to zero; the")
        comment("      half-the-available-water cap still forbids drying in one step")
        stmt("      HMP_RAT=MIN(HMP_FLR,0.5D0*HN%R(HMP_I)/DT)")
        stmt("      PLUIE%R(HMP_I)=-HMP_RAT")
        stmt("      HMP_QEX=HMP_QEX+HMP_RAT*VOLU2D%R(HMP_I)")
        stmt("      HMP_QFI=HMP_QFI+HMP_RAT*VOLU2D%R(HMP_I)")
        stmt("    ENDIF")
    stmt("  ENDIF")
    stmt("ENDDO")
    stmt("IF(NCSIZE.GT.1) HMP_QEX=P_SUM(HMP_QEX)")
    stmt("IF(NCSIZE.GT.1) HMP_QDR=P_SUM(HMP_QDR)")
    if film:
        stmt("IF(NCSIZE.GT.1) HMP_QFI=P_SUM(HMP_QFI)")
    comment("")
    comment("gaining region(s): reinject exactly what was extracted (mass-exact)")
    stmt("HMP_QGS=0.D0")
    stmt("DO HMP_R=1,HMP_NRG")
    stmt(f"  IF(HMP_KND(HMP_R).EQ.{KIND_GAINING})")
    body.append("     &   HMP_QGS=HMP_QGS+HMP_QTG(HMP_R)")
    stmt("ENDDO")
    stmt("IF(HMP_QGS.GT.0.D0) THEN")
    stmt("  DO HMP_I=1,NPOIN")
    stmt("    HMP_R=HMP_IRG(HMP_I)")
    stmt("    IF(HMP_R.GT.0) THEN")
    stmt(f"      IF(HMP_KND(HMP_R).EQ.{KIND_GAINING}) THEN")
    stmt("        PLUIE%R(HMP_I)=HMP_QEX*(HMP_QTG(HMP_R)/HMP_QGS)")
    body.append("     &       /HMP_ARE(HMP_R)")
    stmt("      ENDIF")
    stmt("    ENDIF")
    stmt("  ENDDO")
    stmt("ENDIF")
    comment("")
    comment("Report the DELIVERED discharge periodically. The taper means this is")
    comment("less than the target while the patch is shallow, so it MUST be checked -")
    comment("a silently-zero exchange would look perfectly stable and move no water.")
    comment("Throttled by an own SAVEd counter rather than LT/LISPRD: this routine is")
    comment("called from prosou.f, where those did not reliably line up with the")
    comment("listing printouts (an earlier LT-based guard never fired at all).")
    stmt("HMP_CNT=HMP_CNT+1")
    stmt(f"IF(HMP_CNT.EQ.1.OR.MOD(HMP_CNT,{diag_every}).EQ.0) THEN")
    stmt("  WRITE(LU,*) 'USER_RAIN PERCOLATION: CALL ',HMP_CNT,")
    body.append("     &   ' DELIVERED ',HMP_QEX,' M3/S OF TARGET ',")
    body.append("     &   -HMP_QTG(1),' M3/S  WET AREA ',HMP_WET,' M2'")
    if drains:
        stmt("  WRITE(LU,*) 'USER_RAIN PATCH DRAIN: ',HMP_QDR,")
        body.append("     &   ' M3/S OFF THE POROUS PATCH (INCLUDED ABOVE)'")
    if film:
        stmt("  WRITE(LU,*) 'USER_RAIN FILM INFILTRATION: ',HMP_QFI,")
        body.append("     &   ' M3/S (INCLUDED ABOVE; SHOULD DECAY TOWARDS 0)'")
    stmt("ENDIF")
    stmt("RETURN")
    stmt("END")

    out_dir = cfg.model_path(cfg.user_fortran_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "user_rain.f"
    path.write_text("\n".join(body) + "\n")
    log.info("  wrote USER_RAIN percolation routine -> %s (%d regions, "
             "HMIN=%.3g m)", path, n_reg, hmin)
    return out_dir


def write_water_table_fortran(cfg: Config, plane) -> Path:
    """Generate ``user_rain.f`` for a gain-lose reach whose faces come from the
    **water table** (``gain_lose.faces: water-table``).

    Unlike the ``lines`` route, nothing about *where* the exchange happens is baked
    in. Only the zone polygon and the five plane coefficients are; each call then
    classifies every zone node from its own head:

    * free surface above the table -> **losing** (head pushes water into the body);
    * table above the bed -> **gaining** (the saturated zone cuts the ground).

    So the faces move with the stage - a rising river widens the losing face, which
    is what actually happens and what a build-time mask cannot represent.

    Magnitude is Green-Ampt at ``conductivity``, or ``discharge`` normalised over the
    taper-weighted wet losing area when a measured total is prescribed. Whatever is
    withdrawn is reinjected over the gaining face in the same step, so the routine is
    mass-exact by construction and opens no net sink in the boundary budget.
    """
    from axqua.solvers.telemac.boundary import _percolation_patches, _simplify_region

    if plane is None:
        raise ValueError(
            "gain_lose.faces: water-table needs a water table, but none could be "
            "fitted. Set gain_lose.water_table_levels, give the case a "
            "geodata.channel_centerline, or switch to faces: lines."
        )
    patches = _percolation_patches(cfg)
    if not patches:
        raise ValueError("gain_lose.faces: water-table needs gain_lose.zone "
                         "(the porous body polygon)")
    from shapely.ops import unary_union

    poly = unary_union([p["geom"] for p in patches])
    buffer = cfg.gain_lose.zone_buffer
    if buffer is None:
        buffer = float(cfg.mesh.channel_size) * float(cfg.mesh.size_scale)
    if buffer:
        poly = poly.buffer(float(buffer))
    poly, _ = _simplify_region(poly)
    verts = [(float(x), float(y)) for x, y in list(poly.exterior.coords)[:-1]]

    gl = cfg.gain_lose
    hmin = float(gl.min_depth)
    htap = max(float(gl.taper_depth), 1e-6)
    rmax = float(gl.max_rate)
    kf = gl.conductivity
    lz = next((p["porous_depth"] for p in patches if p["porous_depth"]), None)
    lz = float(lz if lz else gl.porous_depth)
    hf = float(gl.suction)
    target = gl.discharge
    if target is None and kf is None:
        raise ValueError(
            "gain_lose.faces: water-table needs a magnitude - set `conductivity` "
            "(kf) or `discharge`"
        )
    film = cfg.drying.film_infiltration
    diag_every = 500

    body: list[str] = []

    def stmt(text: str) -> None:
        line = "      " + text
        if len(line) > FIXED_FORM_LIMIT:   # pragma: no cover - guarded by tests
            raise ValueError(f"generated Fortran line exceeds 72 columns: {line!r}")
        body.append(line)

    def cont(text: str) -> None:
        body.append("     &" + text)

    def comment(text: str) -> None:
        body.append(f"!     {text}" if text else "!")

    comment("axqua-generated USER_RAIN: gain-lose reach, faces from the")
    comment("WATER TABLE (no exchange lines are drawn anywhere).")
    comment(f"  porous body: {len(patches)} polygon(s), {poly.area:.0f} m2 "
            f"(buffered {buffer:.2f} m)")
    for key, level in sorted(plane.levels.items()):
        comment(f"    {key} end at {level:.3f} m")
    comment(f"  water table gradient {1000 * plane.gradient:.2f} permille, "
            f"residual {plane.residual:.3f} m")
    comment("  each call classifies every zone node from its own head:")
    comment("    surface above the table -> LOSING;  table above bed -> GAINING")
    if target is not None:
        comment(f"  magnitude: PRESCRIBED {target:g} m3/s over the wet losing face")
    else:
        comment(f"  magnitude: Green-Ampt at kf={kf:g} m/s, Lz={lz:g} m, hf={hf:g} m")
    comment("  what is withdrawn is reinjected over the gaining face in the SAME")
    comment("  step: mass-exact, so no net sink appears in the boundary budget.")
    comment("PLUIE is ASSIGNED (not accumulated): prosou.f calls USER_RAIN twice.")

    body.append("      SUBROUTINE USER_RAIN")
    stmt("USE BIEF")
    stmt("USE DECLARATIONS_TELEMAC2D")
    stmt("USE INTERFACE_PARALLEL, ONLY : P_SUM")
    stmt("USE DECLARATIONS_SPECIAL")
    stmt("IMPLICIT NONE")
    # every local carries the HMP_ prefix - DECLARATIONS_TELEMAC2D pulls in a very
    # large namespace and plain names collide with it (a hard compile error)
    stmt(f"INTEGER, PARAMETER :: HMP_NV = {len(verts)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_HMN = {_dbl(hmin)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_TAP = {_dbl(htap)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_RMX = {_dbl(rmax)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_TOL = {_dbl(0.01)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WC0 = {_dbl(plane.c0)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WCX = {_dbl(plane.cx)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WCY = {_dbl(plane.cy)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WX0 = {_dbl(plane.x0)}")
    stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_WY0 = {_dbl(plane.y0)}")
    if target is not None:
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_QTG = {_dbl(float(target))}")
    else:
        # a VARIABLE, not a PARAMETER - see the note in write_user_fortran: this is
        # what lets HydroBayesCal calibrate kf through its `f.` mechanism
        stmt("DOUBLE PRECISION HMP_KF")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_LZ = {_dbl(lz)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_HF = {_dbl(hf)}")
    if film:
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_FLD = {_dbl(cfg.drying.film_depth)}")
        stmt("DOUBLE PRECISION, PARAMETER :: HMP_FLV = "
             f"{_dbl(cfg.drying.film_velocity)}")
        stmt(f"DOUBLE PRECISION, PARAMETER :: HMP_FLR = {_dbl(cfg.drying.film_rate)}")
    stmt("DOUBLE PRECISION, SAVE :: HMP_XV(HMP_NV),HMP_YV(HMP_NV)")
    stmt("LOGICAL, ALLOCATABLE, SAVE :: HMP_INZ(:)")
    stmt("LOGICAL, SAVE :: HMP_DEJA = .FALSE.")
    stmt("INTEGER, SAVE :: HMP_CNT = 0")
    stmt("INTEGER HMP_I")
    stmt("DOUBLE PRECISION HMP_ZWT,HMP_ZS,HMP_F,HMP_QA,HMP_RAT")
    stmt("DOUBLE PRECISION HMP_QEX,HMP_ALO,HMP_AGA,HMP_HED")
    if film:
        stmt("DOUBLE PRECISION HMP_QFI,HMP_SPD")
    if target is None:
        comment("")
        comment("calibratable: HydroBayesCal rewrites this line for `f.HMP_KF`")
        stmt(f"HMP_KF = {_dbl(float(kf))}")
    comment("")
    comment("one-off: which nodes lie inside the porous body")
    stmt("IF(.NOT.HMP_DEJA) THEN")
    for j, (vx, vy) in enumerate(verts, start=1):
        stmt(f"  HMP_XV({j})={_dbl(round(vx, 3))}")
        stmt(f"  HMP_YV({j})={_dbl(round(vy, 3))}")
    stmt("  ALLOCATE(HMP_INZ(NPOIN))")
    stmt("  DO HMP_I=1,NPOIN")
    stmt("    HMP_INZ(HMP_I)=INPOLY(X(HMP_I),Y(HMP_I),")
    cont("     HMP_XV,HMP_YV,HMP_NV)")
    stmt("  ENDDO")
    stmt("  HMP_DEJA=.TRUE.")
    stmt("ENDIF")
    comment("")
    comment("PASS 1: classify at the CURRENT water level and measure both faces.")
    comment("The taper-weighted wet losing area is what a prescribed discharge is")
    comment("spread over; the gaining area is what the extraction is returned to.")
    stmt("HMP_ALO=0.D0")
    stmt("HMP_AGA=0.D0")
    stmt("DO HMP_I=1,NPOIN")
    stmt("  IF(HMP_INZ(HMP_I)) THEN")
    stmt("    HMP_ZWT=HMP_WC0+HMP_WCX*(X(HMP_I)-HMP_WX0)")
    cont("       +HMP_WCY*(Y(HMP_I)-HMP_WY0)")
    stmt("    HMP_ZS=ZF%R(HMP_I)+HN%R(HMP_I)")
    stmt("    IF(HN%R(HMP_I).GT.HMP_TOL.AND.")
    cont("       HMP_ZS.GT.HMP_ZWT+HMP_TOL) THEN")
    stmt("      HMP_F=MIN(1.D0,MAX(0.D0,")
    cont("       (HN%R(HMP_I)-HMP_HMN)/HMP_TAP))")
    stmt("      HMP_ALO=HMP_ALO+HMP_F*VOLU2D%R(HMP_I)")
    stmt("    ELSEIF(HMP_ZWT.GT.ZF%R(HMP_I)+HMP_TOL) THEN")
    stmt("      HMP_AGA=HMP_AGA+VOLU2D%R(HMP_I)")
    stmt("    ENDIF")
    stmt("  ENDIF")
    stmt("ENDDO")
    stmt("IF(NCSIZE.GT.1) HMP_ALO=P_SUM(HMP_ALO)")
    stmt("IF(NCSIZE.GT.1) HMP_AGA=P_SUM(HMP_AGA)")
    comment("")
    comment("PASS 2: withdraw over the losing face, capped by the taper, an")
    comment("absolute ceiling and half the water available above HMP_HMN this")
    comment("step - so a cell can never be drained dry.")
    stmt("HMP_QEX=0.D0")
    if film:
        stmt("HMP_QFI=0.D0")
    stmt("DO HMP_I=1,NPOIN")
    stmt("  IF(HMP_INZ(HMP_I)) THEN")
    stmt("    HMP_ZWT=HMP_WC0+HMP_WCX*(X(HMP_I)-HMP_WX0)")
    cont("       +HMP_WCY*(Y(HMP_I)-HMP_WY0)")
    stmt("    HMP_ZS=ZF%R(HMP_I)+HN%R(HMP_I)")
    stmt("    IF(HN%R(HMP_I).GT.HMP_TOL.AND.")
    cont("       HMP_ZS.GT.HMP_ZWT+HMP_TOL) THEN")
    stmt("      HMP_HED=HMP_ZS-HMP_ZWT")
    stmt("      HMP_F=MIN(1.D0,MAX(0.D0,")
    cont("       (HN%R(HMP_I)-HMP_HMN)/HMP_TAP))")
    if target is not None:
        stmt("      IF(HMP_ALO.GT.0.D0) THEN")
        stmt("        HMP_QA=HMP_QTG*HMP_F/HMP_ALO")
        stmt("      ELSE")
        stmt("        HMP_QA=0.D0")
        stmt("      ENDIF")
    else:
        comment("      Green-Ampt saturated limit at the local head")
        stmt("      HMP_QA=HMP_KF*(HMP_HED+HMP_LZ+HMP_HF)/HMP_LZ")
        stmt("      HMP_QA=HMP_QA*HMP_F")
    stmt("      HMP_RAT=MIN(HMP_QA,HMP_RMX,")
    cont("       0.5D0*MAX(HN%R(HMP_I)-HMP_HMN,0.D0)/DT)")
    stmt("      PLUIE%R(HMP_I)=-HMP_RAT")
    stmt("      HMP_QEX=HMP_QEX+HMP_RAT*VOLU2D%R(HMP_I)")
    stmt("    ENDIF")
    stmt("  ENDIF")
    if film:
        comment("  outside the porous body: FILM INFILTRATION. Water thinner than")
        comment("  HMP_FLD and slower than HMP_FLV stands within the grain")
        comment("  roughness, not over the bed; it joins the same reinjected total.")
        stmt("  IF(.NOT.HMP_INZ(HMP_I)) THEN")
        stmt("    HMP_SPD=SQRT(UN%R(HMP_I)**2+VN%R(HMP_I)**2)")
        stmt("    IF(HN%R(HMP_I).GT.0.D0.AND.HN%R(HMP_I).LT.HMP_FLD")
        cont("       .AND.HMP_SPD.LT.HMP_FLV) THEN")
        stmt("      HMP_RAT=MIN(HMP_FLR,0.5D0*HN%R(HMP_I)/DT)")
        stmt("      PLUIE%R(HMP_I)=-HMP_RAT")
        stmt("      HMP_QEX=HMP_QEX+HMP_RAT*VOLU2D%R(HMP_I)")
        stmt("      HMP_QFI=HMP_QFI+HMP_RAT*VOLU2D%R(HMP_I)")
        stmt("    ENDIF")
        stmt("  ENDIF")
    stmt("ENDDO")
    stmt("IF(NCSIZE.GT.1) HMP_QEX=P_SUM(HMP_QEX)")
    if film:
        stmt("IF(NCSIZE.GT.1) HMP_QFI=P_SUM(HMP_QFI)")
    comment("")
    comment("PASS 3: return exactly what was taken, spread over the gaining face")
    stmt("IF(HMP_AGA.GT.0.D0.AND.HMP_QEX.GT.0.D0) THEN")
    stmt("  DO HMP_I=1,NPOIN")
    stmt("    IF(HMP_INZ(HMP_I)) THEN")
    stmt("      HMP_ZWT=HMP_WC0+HMP_WCX*(X(HMP_I)-HMP_WX0)")
    cont("         +HMP_WCY*(Y(HMP_I)-HMP_WY0)")
    stmt("      HMP_ZS=ZF%R(HMP_I)+HN%R(HMP_I)")
    stmt("      IF(.NOT.(HN%R(HMP_I).GT.HMP_TOL.AND.")
    cont("         HMP_ZS.GT.HMP_ZWT+HMP_TOL).AND.")
    cont("         HMP_ZWT.GT.ZF%R(HMP_I)+HMP_TOL) THEN")
    stmt("        PLUIE%R(HMP_I)=HMP_QEX/HMP_AGA")
    stmt("      ENDIF")
    stmt("    ENDIF")
    stmt("  ENDDO")
    stmt("ENDIF")
    comment("")
    comment("Report what is actually delivered. The taper means this is less than")
    comment("any target while the faces are shallow, so it MUST be checked - a")
    comment("silently-zero exchange looks perfectly stable and moves no water.")
    stmt("HMP_CNT=HMP_CNT+1")
    stmt(f"IF(HMP_CNT.EQ.1.OR.MOD(HMP_CNT,{diag_every}).EQ.0) THEN")
    stmt("  WRITE(LU,*) 'USER_RAIN GAIN-LOSE: CALL ',HMP_CNT,")
    cont("     ' EXCHANGE ',HMP_QEX,' M3/S  LOSING AREA ',HMP_ALO,")
    cont("     ' M2  GAINING AREA ',HMP_AGA,' M2'")
    if film:
        stmt("  WRITE(LU,*) 'USER_RAIN FILM INFILTRATION: ',HMP_QFI,")
        cont("     ' M3/S (INCLUDED ABOVE; SHOULD DECAY TOWARDS 0)'")
    stmt("ENDIF")
    stmt("RETURN")
    stmt("END")

    out_dir = cfg.model_path(cfg.user_fortran_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "user_rain.f"
    path.write_text("\n".join(body) + "\n")
    log.info("  wrote USER_RAIN gain-lose routine (water-table faces) -> %s "
             "(%d zone vertices, HMIN=%.3g m)", path, len(verts), hmin)
    return out_dir
