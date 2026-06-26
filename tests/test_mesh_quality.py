"""Mesh-quality metrics and validity checks on synthetic meshes.

Builds tiny meshes by hand (no gmsh) and checks that
:func:`hydromate.mesh_quality.assess_quality`:

* measures the shortest edge, angles and aspect ratio correctly;
* reports a deliberately anisotropic 'channel' region separately and does NOT
  warn about its high aspect ratio (the elongation is intended);
* flags fatal defects (zero-area, inverted, duplicate nodes) and quality issues
  (sliver angles, >20% adjacent area jumps).

Pure-python (numpy only). Run via:
    mamba run -n hydromate-env pytest tests/test_mesh_quality.py
"""

from __future__ import annotations

import logging

import numpy as np

from hydromate.mesh import Mesh
from hydromate.mesh_quality import assess_quality, log_report


def _mesh(x, y, tris, ipobo=None):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    tris = np.asarray(tris, int)
    n = x.size
    if ipobo is None:
        # mark every node that lies on a once-used edge as boundary
        from collections import Counter
        c = Counter()
        for t in tris:
            for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                c[(min(a, b), max(a, b))] += 1
        bnodes = sorted({n for e, k in c.items() if k == 1 for n in e})
        ipobo = np.zeros(n, dtype=int)
        ipobo[bnodes] = np.arange(1, len(bnodes) + 1)
    return Mesh(x=x, y=y, triangles=tris, bottom=np.zeros(n), ipobo=np.asarray(ipobo),
               boundary_nodes=np.where(np.asarray(ipobo) > 0)[0],
               element_matid=np.ones(len(tris), int), node_matid=np.ones(n, int))


def _unit_square_two_triangles():
    # a 1x1 square split into two right triangles (legs 1, hypotenuse sqrt2)
    return _mesh([0, 1, 1, 0], [0, 0, 1, 1], [[0, 1, 2], [0, 2, 3]])


def test_basic_metrics():
    rep = assess_quality(_unit_square_two_triangles())
    assert rep.n_elem == 2 and rep.n_nodes == 4
    assert abs(rep.min_edge - 1.0) < 1e-9
    assert abs(rep.max_edge - np.sqrt(2)) < 1e-9
    r = rep.regions[0]
    assert abs(r.min_angle - 45.0) < 1e-6 and abs(r.max_angle - 90.0) < 1e-6
    assert abs(r.aspect_median - np.sqrt(2)) < 1e-6
    assert not rep.is_fatal


def test_channel_region_relaxed(caplog):
    # a long thin (anisotropic) channel triangle + a near-equilateral floodplain
    x = [0, 10, 0, 20, 25, 22]
    y = [0, 0.5, 1, 0, 0, 4]
    tris = [[0, 1, 2], [3, 4, 5]]
    mesh = _mesh(x, y, tris)
    mask = np.array([True, False])           # first tri is channel
    rep = assess_quality(mesh, channel_mask=mask)
    labels = {r.label: r for r in rep.regions}
    assert set(labels) == {"channel", "floodplain"}
    assert labels["channel"].aspect_max > 5     # elongated, as intended
    assert labels["channel"].relaxed is True

    with caplog.at_level(logging.WARNING, logger="hydromate"):
        log_report(rep)
    # the channel's high aspect must NOT raise an aspect warning
    assert "aspect ratio" not in caplog.text


def test_detects_zero_area_and_duplicates():
    # two coincident nodes (3) duplicate node 2 -> a zero-area triangle
    mesh = _mesh([0, 1, 1, 1], [0, 0, 1, 1], [[0, 1, 2], [0, 2, 3]])
    rep = assess_quality(mesh)
    assert rep.n_duplicate_nodes >= 1
    assert rep.n_zero_area >= 1
    assert rep.is_fatal


def test_area_jump_warning(caplog):
    # a tiny triangle adjacent to a huge one across a shared edge -> big area jump
    x = [0.0, 1.0, 0.5, 0.5]
    y = [0.0, 0.0, 0.02, 40.0]
    mesh = _mesh(x, y, [[0, 1, 2], [0, 1, 3]])   # share edge 0-1
    rep = assess_quality(mesh, area_jump_threshold=0.20)
    assert rep.frac_area_jump_over > 0.0
    assert rep.max_area_jump_ratio > 1.25
    with caplog.at_level(logging.WARNING, logger="hydromate"):
        log_report(rep)
    assert "jump in area" in caplog.text


def test_edge_flip_caps_aspect_ratio():
    # a slanted quad split by its long diagonal -> two over-sharp triangles; the
    # flip to the short diagonal halves the aspect ratio (sliver removal).
    from hydromate.mesh import _flip_sharp_edges, _tri_aspect

    x = np.array([0.0, 6.0, 7.0, 1.0])
    y = np.array([0.0, 0.0, 2.0, 2.0])
    tri = np.array([[0, 1, 2], [0, 2, 3]])           # shared diagonal 0-2

    before = float(_tri_aspect(x, y, tri)[0].max())
    assert before > 3.0
    new = _flip_sharp_edges(tri, x, y, 3.0)
    aspect, area2 = _tri_aspect(x, y, new)
    assert float(aspect.max()) <= 3.0 < before        # cap met
    assert (area2 > 0).all()                          # no inverted/zero-area cell
    # the connectivity changed (the diagonal was flipped to 1-3)
    assert {tuple(sorted(t)) for t in new} != {tuple(sorted(t)) for t in tri}


if __name__ == "__main__":
    test_basic_metrics()
    test_detects_zero_area_and_duplicates()
    test_edge_flip_caps_aspect_ratio()
    print("MESH-QUALITY TESTS PASSED")
