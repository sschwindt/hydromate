"""Physical validity checks for a mesh resolution (y+, roughness, turbulence).

Standard companion checks for the mesh-convergence study: grid refinement is only
meaningful while the turbulence closure and the bed-roughness parameterization
remain applicable at the finer cell size, so every level (and every *candidate*
refinement offered to the user) gets:

* the **shear velocity** from the fully-rough log law,
  ``u* = kappa * U / ln(11 h / ks)``, built from that level's mean probe depth
  ``h``, mean probe velocity ``U`` and the channel Nikuradse roughness ``ks``;
* the **dimensionless wall distance** ``y+ = u* dx / nu`` of the lateral wall
  functions (first grid point ~ one channel cell off the bank); the log-law
  window needs ``y+ >= ~30``. In a river model y+ is O(1e4), so this passes for
  any realistic dx - it is reported as standard practice, not as the binding
  constraint;
* the **roughness Reynolds number** ``ks+ = u* ks / nu``: ``>= 70`` fully rough
  (the Nikuradse friction law's regime), ``5..70`` transitional (law marginal),
  ``< 5`` hydraulically smooth;
* the **cell size vs. ks** ratio: once ``dx < ks`` the mesh resolves bed scales
  the roughness closure already parameterizes (double-counting risk) - usually
  the first check to fire under refinement;
* the **turbulence-model consistency**: what
  :func:`axqua.steering.turbulence_pick_for_dx` *would* auto-select at this
  ``dx`` versus the model the study is pinned to - a regime change means the
  pinned closure's validity assumptions no longer hold at this resolution.

Pure maths - no solver, no gmsh; unit-testable standalone.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from axqua.config import Config
from axqua.core.geodata import dataset

log = logging.getLogger("axqua")

KAPPA = 0.41                  # von Karman constant
NU_WATER = 1.0e-6             # kinematic viscosity of water [m2/s] (~20 degC)
Y_PLUS_MIN = 30.0             # lower bound of the log layer (wall functions)
KS_PLUS_ROUGH = 70.0          # ks+ above which the flow is fully rough
KS_PLUS_SMOOTH = 5.0          # ks+ below which the wall is hydraulically smooth


def shear_velocity_loglaw(depth: float, velocity: float, ks: float,
                          kappa: float = KAPPA) -> float:
    """Shear velocity u* [m/s] from the depth-averaged fully-rough log law.

    Inverts ``U/u* = ln(11 h / ks) / kappa`` (log law integrated over the depth,
    zero-velocity height ``z0 = ks/30`` -> the classic ``11 h / ks`` argument).
    NaN when the inputs are unusable (h, U or ks non-positive, or ``11h/ks <= 1``,
    i.e. roughness height of the order of the flow depth).
    """
    if not (depth > 0.0 and velocity > 0.0 and ks and ks > 0.0):
        return float("nan")
    arg = 11.0 * depth / ks
    if arg <= 1.0:
        return float("nan")
    return kappa * velocity / math.log(arg)


def wall_y_plus(u_star: float, dx: float, nu: float = NU_WATER) -> float:
    """Dimensionless wall distance y+ = u* * dx / nu of the first grid point
    (one channel cell off a lateral wall)."""
    return u_star * dx / nu


def roughness_reynolds(u_star: float, ks: float, nu: float = NU_WATER) -> float:
    """Roughness Reynolds number ks+ = u* * ks / nu."""
    return float("nan") if not (ks and ks > 0.0) else u_star * ks / nu


def manning_to_ks(n: float) -> float:
    """Nikuradse ks [m] from a Manning n via the Strickler relation
    ``Kst = 1/n = 26 / ks^(1/6)``, i.e. ``ks = (26 n)**6``."""
    return (26.0 * n) ** 6


def _ks_from_law(law: int, coefficient: float) -> float | None:
    """Nikuradse ks [m] from a friction (law, coefficient) pair, or None."""
    if law == 5:                                   # NIKU: coefficient IS ks
        return float(coefficient)
    if law == 4:                                   # Manning n
        return manning_to_ks(float(coefficient))
    if law == 3 and coefficient:                   # Strickler Kst = 1/n
        return manning_to_ks(1.0 / float(coefficient))
    return None


def channel_ks(cfg: Config) -> float | None:
    """The channel-bed Nikuradse roughness ks [m] from the case config.

    Resolution order mirrors :func:`axqua.steering._friction_rows`:
    explicit ``friction.zones`` (a zone named ``*channel*``/``*riverbed*``) >
    ``geodata.roughness_zones`` + ``roughness_table`` (the Zone ID overlapping the
    channel mesh-zone union the most) > None (checks needing ks are skipped).
    Manning/Strickler coefficients are converted via the Strickler relation.
    """
    for z in cfg.friction.zones or []:
        name = str(z.name).lower()
        if "channel" in name or "riverbed" in name:
            return _ks_from_law(z.law, z.coefficient)
    if cfg.geodata.roughness_zones is not None and cfg.geodata.roughness_table is not None:
        try:
            table = dataset(cfg).roughness_table()
            gdf = dataset(cfg).roughness_zones()
            id_field = next((c for c in gdf.columns
                             if str(c).lower() == cfg.mesh.roughness_zone_field.lower()),
                            None)
            if id_field is None:
                return None
            # was mesh_mod._channel_union(cfg): a reach into another
            # module's private helper, which the shared Dataset removes
            channel = dataset(cfg).channel_union()
            overlap: dict[int, float] = {}
            for zid, geom in zip(gdf[id_field], gdf.geometry.values):
                if geom is None:
                    continue
                a = geom.intersection(channel).area
                if a > 0:
                    zid = int(zid)
                    overlap[zid] = overlap.get(zid, 0.0) + a
            if overlap:
                best = max(overlap, key=overlap.get)
                ks = table.get(best)
                if ks is not None:
                    return _ks_from_law(cfg.friction.roughness_law, ks)
        except Exception as exc:
            log.debug("channel_ks unavailable from roughness zones: %s", exc)
    return None


@dataclass
class MeshValidity:
    """Validity of the turbulence + roughness modelling at one cell size."""

    dx: float                       # channel cell size [m]
    depth: float                    # mean probe water depth [m]
    velocity: float                 # mean probe scalar velocity [m/s]
    ks: float | None                # channel Nikuradse roughness [m] (None: unknown)
    u_star: float = float("nan")    # shear velocity [m/s]
    y_plus: float = float("nan")    # u* dx / nu (lateral wall functions)
    ks_plus: float = float("nan")   # u* ks / nu
    dx_over_ks: float | None = None
    y_plus_ok: bool = True          # y+ >= 30 -> log-layer wall functions valid
    ks_regime: str = "unknown"      # fully rough / transitional / smooth / unknown
    dx_below_ks: bool = False       # dx < ks -> roughness double-counting risk
    turbulence_pick: int | None = None       # auto-selection at this dx
    turbulence_pick_name: str = ""
    pinned_model: int | None = None          # the model the study runs with
    regime_change: bool = False              # auto pick != pinned model
    estimated: bool = False         # depth/velocity were placeholders, not sampled
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        """Human-readable summary lines (report block / extension prompt)."""
        def num(v, fmt="{:.3f}"):
            return fmt.format(v) if v is not None and math.isfinite(v) else "-"

        parts = [f"dx={num(self.dx)} m", f"h={num(self.depth, '{:.2f}')} m",
                 f"U={num(self.velocity, '{:.2f}')} m/s",
                 f"u*={num(self.u_star)} m/s",
                 f"y+={num(self.y_plus, '{:.0f}')}",
                 f"ks+={num(self.ks_plus, '{:.0f}')} ({self.ks_regime})",
                 f"dx/ks={num(self.dx_over_ks, '{:.2f}')}"]
        if self.turbulence_pick is not None:
            pin = (f" [pinned: {_turb_name(self.pinned_model)}]"
                   if self.pinned_model is not None else "")
            parts.append(f"turb-auto={self.turbulence_pick_name}{pin}")
        if self.estimated:
            parts.append("(h/U estimated)")
        out = ["  ".join(parts)]
        out.extend(f"! {w}" for w in self.warnings)
        return out


def _turb_name(model: int | None) -> str:
    from axqua.steering import TURB_NAMES

    return TURB_NAMES.get(model, str(model)) if model is not None else "-"


def check_level(cfg: Config, *, dx: float, depth: float, velocity: float,
                ks: float | None = None, pinned_model: int | None = None,
                estimated: bool = False) -> MeshValidity:
    """Run the validity checks for one cell size *dx* (see the module docstring).

    *depth*/*velocity* are the level's mean probe values (or the finest completed
    level's, for a candidate refinement; then set ``estimated=True`` when they are
    placeholders). *ks* defaults to :func:`channel_ks`; *pinned_model* is the
    turbulence model the study is pinned to (None skips the consistency check).
    The turbulence auto-pick deliberately ignores any explicit
    ``hydrodynamics.turbulence_model`` (the study pins that on each level copy).
    """
    if ks is None:
        try:
            ks = channel_ks(cfg)
        except Exception as exc:                 # bare test configs
            log.debug("channel_ks unavailable: %s", exc)
    v = MeshValidity(dx=dx, depth=depth, velocity=velocity, ks=ks,
                     pinned_model=pinned_model, estimated=estimated)

    v.u_star = shear_velocity_loglaw(depth, velocity, ks) if ks else float("nan")
    if math.isfinite(v.u_star):
        v.y_plus = wall_y_plus(v.u_star, dx)
        v.ks_plus = roughness_reynolds(v.u_star, ks)
        v.y_plus_ok = v.y_plus >= Y_PLUS_MIN
        if not v.y_plus_ok:
            v.warnings.append(
                f"y+ = {v.y_plus:.0f} < {Y_PLUS_MIN:.0f}: the lateral wall functions "
                "sit below the log layer at this refinement")
        if v.ks_plus >= KS_PLUS_ROUGH:
            v.ks_regime = "fully rough"
        elif v.ks_plus >= KS_PLUS_SMOOTH:
            v.ks_regime = "transitional"
            v.warnings.append(
                f"ks+ = {v.ks_plus:.0f} is transitional ({KS_PLUS_SMOOTH:.0f}-"
                f"{KS_PLUS_ROUGH:.0f}): the fully-rough Nikuradse friction law "
                "is marginal here")
        else:
            v.ks_regime = "smooth"
            v.warnings.append(
                f"ks+ = {v.ks_plus:.0f} < {KS_PLUS_SMOOTH:.0f}: hydraulically "
                "smooth - a roughness-based friction law is not applicable")

    if ks and ks > 0.0:
        v.dx_over_ks = dx / ks
        v.dx_below_ks = dx < ks
        if v.dx_below_ks:
            v.warnings.append(
                f"channel cell size {dx:.3f} m < ks {ks:.3f} m: the mesh resolves "
                "bed scales the roughness closure already parameterizes "
                "(double-counting risk) - reconsider ks before refining further")
    else:
        v.warnings.append("channel ks unknown: y+/ks+ and dx-vs-ks checks skipped")

    try:
        from axqua import steering

        v.turbulence_pick, _ = steering.turbulence_pick_for_dx(cfg, dx)
        v.turbulence_pick_name = _turb_name(v.turbulence_pick)
        if pinned_model is not None and v.turbulence_pick != pinned_model:
            v.regime_change = True
            v.warnings.append(
                f"turbulence-model regime change: auto-selection at dx={dx:.3f} m "
                f"would pick {v.turbulence_pick_name}, but the study is pinned to "
                f"{_turb_name(pinned_model)} - the pinned closure's validity "
                "assumptions no longer match this resolution")
    except Exception as exc:  # config without hydrodynamics knobs (tests)
        log.debug("turbulence consistency check skipped: %s", exc)

    return v
