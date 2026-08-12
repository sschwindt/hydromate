"""Seed an OpenFOAM case from the converged TELEMAC 2D result.

Starting a VOF run from a dry bed, or from a flat block of water, means paying for
the whole filling transient in the most expensive model in the chain. The 2D result
already knows where the water is, how deep it is and how fast it moves, so the 3D
run can start from that state and spend its time on what only 3D can give -
the vertical structure, the secondary currents, the local surface deformation.

The same state is used for three separate jobs, which is why it lives in one class:

* the **footprint** - the 3D mesh only needs to cover the wetted corridor plus a
  margin, not the whole ROI (:meth:`State2D.wet_footprint`);
* the **lid** - clamped a fixed freeboard above the 2D free surface, which is what
  keeps the air phase small (:meth:`State2D.sample_surface`);
* the **initial fields** - ``alpha.water`` from the free-surface elevation and ``U``
  from the depth-averaged velocity (:meth:`State2D.sample_columns`).

Sampling is nearest-node on a KD-tree rather than P1 interpolation on the element
table - see :attr:`State2D.tree` for why a merged parallel SELAFIN leaves no usable
triangulation, and why that costs nothing when the 2D mesh is finer than the 3D
lattice it seeds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("hydromate")

# SELAFIN variable names, in the order they are looked for
_DEPTH = ("WATER DEPTH", "HAUTEUR D'EAU")
_SURFACE = ("FREE SURFACE", "SURFACE LIBRE")
_BOTTOM = ("BOTTOM", "FOND")
_U = ("VELOCITY U", "VITESSE U")
_V = ("VELOCITY V", "VITESSE V")


def _pick(values: dict, names: tuple[str, ...]) -> np.ndarray | None:
    for name in names:
        if name in values:
            return np.asarray(values[name], dtype=float)
    return None


@dataclass
class State2D:
    """A depth-averaged flow state on the TELEMAC mesh."""

    x: np.ndarray
    y: np.ndarray
    triangles: np.ndarray
    depth: np.ndarray
    surface: np.ndarray
    bottom: np.ndarray
    u: np.ndarray
    v: np.ndarray
    time: float = 0.0
    source: Path | None = None

    _tree: object = None     # cached scipy cKDTree over the nodes
    _spacing: float | None = None   # cached median node spacing [m]

    # ------------------------------------------------------------------ load
    @classmethod
    def from_slf(cls, path: str | Path, *, frame: int = -1) -> "State2D":
        """Read *path* (a TELEMAC ``r2d.slf``), by default its last frame."""
        from hydromate.selafin import read_slf

        path = Path(path)
        data = read_slf(path, frame=frame)
        values = data["values"]
        depth = _pick(values, _DEPTH)
        bottom = _pick(values, _BOTTOM)
        surface = _pick(values, _SURFACE)
        if depth is None and surface is not None and bottom is not None:
            depth = surface - bottom
        if surface is None and depth is not None and bottom is not None:
            surface = bottom + depth
        if depth is None or surface is None:
            raise ValueError(
                f"{path.name} carries neither WATER DEPTH nor FREE SURFACE+BOTTOM "
                f"(has {data['var_names']}); it cannot seed an OpenFOAM case."
            )
        if bottom is None:
            bottom = surface - depth
        u = _pick(values, _U)
        v = _pick(values, _V)
        zeros = np.zeros_like(depth)
        state = cls(
            x=data["x"], y=data["y"], triangles=np.asarray(data["ikle"]),
            depth=depth, surface=surface, bottom=bottom,
            u=zeros if u is None else u, v=zeros if v is None else v,
            time=float(data["time"]), source=path,
        )
        log.info("2D hotstart: %s frame at t=%g s, %d nodes, %d wet",
                 path.name, state.time, state.x.size, int((depth > 0).sum()))
        return state

    # ------------------------------------------------------------ properties
    @property
    def tree(self):
        """KD-tree over the 2D nodes, built once.

        Sampling is **nearest-node**, deliberately, not P1 interpolation on the
        element table. A SELAFIN merged from a parallel run is not a clean
        triangulation - the isar result carries 4,393 coincident nodes and 66,759
        zero-area triangles out of 460,314, artefacts of the subdomain interfaces
        that TELEMAC ignores but that make matplotlib's ``TrapezoidMapTriFinder``
        reject the mesh outright. Repairing that into something a trifinder accepts
        is guesswork about which overlapping element is authoritative.

        Nearest-node needs none of it and costs nothing in accuracy *here*, because
        the 2D mesh is **finer than the 3D lattice** it is being sampled onto (isar:
        0.3 m channel cells into a 0.5 m lattice): this is downsampling, and the
        residual error is the surface slope over one 2D cell - on the order of a
        millimetre. :meth:`resolution_check` warns if that relationship ever
        reverses.
        """
        if self._tree is None:
            from scipy.spatial import cKDTree

            self._tree = cKDTree(np.column_stack([self.x, self.y]))
        return self._tree

    @property
    def node_spacing(self) -> float:
        """Median distance to a node's nearest neighbour [m]."""
        if self._spacing is None:
            sample = self.x.size if self.x.size <= 20000 else 20000
            idx = np.linspace(0, self.x.size - 1, sample).astype(int)
            pts = np.column_stack([self.x[idx], self.y[idx]])
            dist, _ = self.tree.query(pts, k=2)
            positive = dist[:, 1][dist[:, 1] > 0]
            self._spacing = float(np.median(positive)) if positive.size else 1.0
        return self._spacing

    def resolution_check(self, cell_size: float) -> str | None:
        """Warn when the 3D lattice is finer than the 2D mesh that seeds it."""
        if cell_size < self.node_spacing:
            return (f"the OpenFOAM lattice ({cell_size:g} m) is finer than the 2D mesh "
                    f"that seeds it (~{self.node_spacing:.2f} m node spacing): the "
                    "hotstart is being upsampled, so the initial free surface is "
                    "piecewise constant over several 3D cells. Harmless as a seed, "
                    "but do not read the first few time steps as a result.")
        return None

    def wet(self, wet_depth: float = 0.01) -> np.ndarray:
        return self.depth > wet_depth

    def velocity_scale(self, wet_depth: float = 0.01) -> float:
        """Representative water speed [m/s]: the 95th percentile over wet nodes.

        Used to size the ``limitVelocity`` cap and the turbulence seed. A percentile
        rather than the maximum, because a single partially-wet cell in a 2D result
        can carry an absurd velocity that would set a useless cap.
        """
        wet = self.wet(wet_depth)
        if not wet.any():
            return 1.0
        speed = np.hypot(self.u[wet], self.v[wet])
        return float(max(np.percentile(speed, 95), 0.05))

    def depth_scale(self, wet_depth: float = 0.01) -> float:
        """Representative flow depth [m]: the median over wet nodes."""
        wet = self.wet(wet_depth)
        if not wet.any():
            return 1.0
        return float(max(np.median(self.depth[wet]), wet_depth))

    # -------------------------------------------------------------- sampling
    def _sample(self, values: np.ndarray, xy: np.ndarray,
                max_distance: float | None = None) -> np.ndarray:
        """Nearest-node value at *xy*; NaN beyond *max_distance* of any 2D node.

        The cutoff is what makes "outside the 2D model" distinguishable from "at the
        edge of it" - without it a KD-tree happily reports the nearest node hundreds
        of metres away and the lid would inherit a water level from another part of
        the reach.
        """
        xy = np.asarray(xy, dtype=float)
        cutoff = max_distance if max_distance is not None else 5.0 * self.node_spacing
        dist, idx = self.tree.query(xy, k=1, distance_upper_bound=cutoff)
        out = np.full(xy.shape[0], np.nan)
        inside = np.isfinite(dist) & (idx < self.x.size)
        out[inside] = np.asarray(values, dtype=float)[idx[inside]]
        return out

    def sample_surface(self, xy: np.ndarray) -> np.ndarray:
        """Free-surface elevation [m a.s.l.] at arbitrary plan points.

        Where the 2D model is dry the free surface *is* the bed, so the lid derived
        from this naturally hugs the ground over dry bars instead of flying over
        them - which is the whole point of following the surface.
        """
        return self._sample(self.surface, xy)

    def sample_columns(self, xy: np.ndarray, *, wet_depth: float = 0.01
                       ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """``(free surface, depth, [u v])`` at the plan points of the mesh columns.

        Velocity is zeroed wherever the 2D depth is below *wet_depth*: a
        depth-averaged velocity carried by a millimetre of water is numerically
        large and physically meaningless, and seeding it into a 3D run puts a
        spurious jet on the bank at t = 0.
        """
        xy = np.asarray(xy)
        surface = self._sample(self.surface, xy)
        depth = self._sample(self.depth, xy)
        uu = self._sample(self.u, xy)
        vv = self._sample(self.v, xy)
        depth = np.where(np.isfinite(depth), depth, 0.0)
        depth = np.maximum(depth, 0.0)
        dry = depth <= wet_depth
        uv = np.column_stack([np.where(np.isfinite(uu), uu, 0.0),
                              np.where(np.isfinite(vv), vv, 0.0)])
        uv[dry] = 0.0
        return surface, depth, uv

    # ------------------------------------------------------------- footprint
    def wet_footprint(self, *, wet_depth: float = 0.01, buffer: float = 5.0,
                      resolution: float | None = None):
        """Plan polygon of the wetted area, dilated by *buffer* metres.

        Built by rasterising the wet nodes and polygonising the mask rather than by
        unioning wet triangles: this mesh has 460k of them, and a shapely union over
        that many polygons is minutes of work for a footprint that only needs to be
        accurate to a cell. Small holes are closed first, so an isolated dry node
        mid-channel does not punch a hole through the 3D domain.

        Returns the largest resulting polygon, or ``None`` when nothing is wet.
        """
        import rasterio.features
        import rasterio.transform
        from scipy import ndimage
        from shapely.geometry import shape
        from shapely.ops import unary_union

        wet = self.wet(wet_depth)
        if not wet.any():
            return None
        res = float(resolution) if resolution else max(buffer / 5.0, 0.5)
        pad = buffer + 3 * res
        x0, x1 = self.x[wet].min() - pad, self.x[wet].max() + pad
        y0, y1 = self.y[wet].min() - pad, self.y[wet].max() + pad
        nx = int(np.ceil((x1 - x0) / res))
        ny = int(np.ceil((y1 - y0) / res))
        mask = np.zeros((ny, nx), dtype=bool)
        cols = np.clip(((self.x[wet] - x0) / res).astype(int), 0, nx - 1)
        rows = np.clip(((y1 - self.y[wet]) / res).astype(int), 0, ny - 1)
        mask[rows, cols] = True

        mask = ndimage.binary_closing(mask, structure=np.ones((3, 3)), iterations=2)
        mask = ndimage.binary_fill_holes(mask)
        grow = int(np.ceil(buffer / res))
        if grow > 0:
            mask = ndimage.binary_dilation(mask, structure=np.ones((3, 3)),
                                           iterations=grow)

        transform = rasterio.transform.from_origin(x0, y1, res, res)
        polys = [shape(geom) for geom, value in
                 rasterio.features.shapes(mask.astype(np.uint8), mask=mask,
                                          transform=transform) if value]
        if not polys:
            return None
        merged = unary_union(polys)
        if merged.geom_type == "MultiPolygon":
            merged = max(merged.geoms, key=lambda g: g.area)
        log.info("2D wetted footprint: %.0f m2 (H > %g m, buffered %g m)",
                 merged.area, wet_depth, buffer)
        return merged

    # ---------------------------------------------------------------- report
    def summary(self, *, wet_depth: float = 0.01) -> list[str]:
        wet = self.wet(wet_depth)
        lines = [f"2D hotstart {self.source.name if self.source else '(in memory)'} "
                 f"at t = {self.time:g} s"]
        if not wet.any():
            lines.append("  the result is entirely dry - it cannot seed a 3D run")
            return lines
        speed = np.hypot(self.u[wet], self.v[wet])
        lines.append(f"  wet nodes   : {int(wet.sum()):,} of {self.x.size:,}")
        lines.append(f"  depth       : median {np.median(self.depth[wet]):.3f} m, "
                     f"max {self.depth[wet].max():.3f} m")
        lines.append(f"  speed       : median {np.median(speed):.3f} m/s, "
                     f"p95 {np.percentile(speed, 95):.3f} m/s, "
                     f"max {speed.max():.3f} m/s")
        lines.append(f"  free surface: {self.surface[wet].min():.3f} .. "
                     f"{self.surface[wet].max():.3f} m a.s.l.")
        return lines


def load_hotstart(cfg, path: str | Path | None = None) -> State2D | None:
    """Best-effort 2D hotstart for *cfg*: the case's own ``r2d.slf`` unless given.

    Returns ``None`` (with a warning) rather than raising when no 2D result exists -
    a case can still be meshed and run without one, it just carries a flat lid and
    a cold start, and the caller reports that.
    """
    candidate = Path(path) if path is not None else cfg.model_path(cfg.results_slf)
    if not candidate.exists():
        log.warning("no 2D result at %s: the OpenFOAM case will use a flat lid and "
                    "a cold start. Run initial_run.py first for a hotstart.",
                    candidate)
        return None
    return State2D.from_slf(candidate)
