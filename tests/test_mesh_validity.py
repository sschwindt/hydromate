"""Mesh-validity maths (y+ / ks+ / dx-vs-ks / turbulence consistency).

Pure python - no solver, no geodata. Run via:
    mamba run -n axqua-env pytest tests/test_mesh_validity.py
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from axqua import mesh_validity
from axqua.mesh_validity import (
    channel_ks, check_level, manning_to_ks, roughness_reynolds,
    shear_velocity_loglaw, wall_y_plus,
)


def _hydro_cfg():
    """Just enough config for the turbulence auto-pick (L = 1 m, U = 1 m/s)."""
    return SimpleNamespace(
        hydrodynamics=SimpleNamespace(initial_velocity_guess=1.0,
                                      turbulence_length_scale=1.0),
        initialization=SimpleNamespace(prewet_depth=None),
    )


def test_shear_velocity_and_dimensionless_numbers():
    # h=1.5 m, U=1 m/s, ks=0.2 m: u* = 0.41 / ln(11*1.5/0.2) ~ 0.0929 m/s
    u_star = shear_velocity_loglaw(1.5, 1.0, 0.2)
    assert abs(u_star - 0.41 / math.log(82.5)) < 1e-12
    assert abs(wall_y_plus(u_star, 0.4) - u_star * 0.4 / 1e-6) < 1e-6
    assert roughness_reynolds(u_star, 0.2) > mesh_validity.KS_PLUS_ROUGH
    # degenerate inputs -> NaN, not an exception
    assert math.isnan(shear_velocity_loglaw(0.0, 1.0, 0.2))
    assert math.isnan(shear_velocity_loglaw(1.5, 1.0, 20.0))   # 11h/ks <= 1
    assert abs(manning_to_ks(0.03) - (26 * 0.03) ** 6) < 1e-12


def test_check_level_flags_dx_below_ks():
    cfg = _hydro_cfg()
    ok = check_level(cfg, dx=0.4, depth=1.5, velocity=1.0, ks=0.2)
    assert ok.ks_regime == "fully rough" and ok.y_plus_ok and not ok.dx_below_ks
    assert not any("double-counting" in w for w in ok.warnings)

    fine = check_level(cfg, dx=0.15, depth=1.5, velocity=1.0, ks=0.2)
    assert fine.dx_below_ks and fine.dx_over_ks < 1.0
    assert any("double-counting" in w for w in fine.warnings)

    unknown = check_level(cfg, dx=0.4, depth=1.5, velocity=1.0, ks=None)
    # SimpleNamespace has no friction/geodata -> ks stays unresolved
    assert unknown.ks_regime == "unknown"
    assert any("ks unknown" in w for w in unknown.warnings)


def test_turbulence_pick_and_regime_change():
    from axqua.steering import turbulence_pick_for_dx

    cfg = _hydro_cfg()                       # L = 1 m
    assert turbulence_pick_for_dx(cfg, 0.05)[0] == 4    # dx/L <= 0.0894 -> LES
    assert turbulence_pick_for_dx(cfg, 0.20)[0] == 3    # >= 4 cells/L -> k-epsilon
    assert turbulence_pick_for_dx(cfg, 0.50)[0] == 6    # coarse -> Spalart-Allmaras

    v = check_level(cfg, dx=0.20, depth=1.5, velocity=1.0, ks=0.05, pinned_model=6)
    assert v.turbulence_pick == 3 and v.regime_change
    assert any("regime change" in w for w in v.warnings)
    same = check_level(cfg, dx=0.20, depth=1.5, velocity=1.0, ks=0.05, pinned_model=3)
    assert not same.regime_change


def test_channel_ks_from_friction_zones():
    zones = [SimpleNamespace(matid=2, name="floodplain", law=5, coefficient=0.5),
             SimpleNamespace(matid=1, name="riverbed channel", law=5, coefficient=0.2)]
    cfg = SimpleNamespace(friction=SimpleNamespace(zones=zones),
                          geodata=SimpleNamespace(roughness_zones=None,
                                                  roughness_table=None))
    assert channel_ks(cfg) == 0.2
    # a Manning channel zone converts via the Strickler relation
    zones[1].law, zones[1].coefficient = 4, 0.03
    assert abs(channel_ks(cfg) - (26 * 0.03) ** 6) < 1e-12
    # no channel-ish zone and no roughness geodata -> None
    cfg2 = SimpleNamespace(friction=SimpleNamespace(zones=[]),
                           geodata=SimpleNamespace(roughness_zones=None,
                                                   roughness_table=None))
    assert channel_ks(cfg2) is None


def test_validity_lines_render():
    v = check_level(_hydro_cfg(), dx=0.15, depth=1.5, velocity=1.0, ks=0.2,
                    pinned_model=3)
    lines = v.lines()
    assert "dx=0.150 m" in lines[0] and "ks+=" in lines[0]
    assert any(ln.startswith("!") for ln in lines[1:])
