"""Loading a job's results into QGIS, from the manifest the runner wrote.

The runner produces files and a small ``results/results.json`` describing them; the
plugin reads that and decides what to add. Neither side ever hands the other an array
(plan §26), and the runner never imports PyQGIS (plan §14).

Discovery is **manifest-first, glob-second**. A manifest says what a result *is* - which
variable to style, whether it is a mesh or a table - and a glob can only guess from a file
extension. The glob fallback exists for a job that predates the manifest or whose
postprocessing failed, so the user can still reach their data.

Layers go into a ``hydromate/<job id>`` group and nothing else about the project is
touched. A plugin that quietly reordered layers or changed the CRS would be a plugin
people stop trusting with their work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from qgis.core import (QgsMeshLayer, QgsProject, QgsRasterLayer, QgsVectorLayer)

from . import styles

#: Extensions QGIS can open, and how. Used only by the glob fallback.
MESH_SUFFIXES = {".slf", ".sslf", ".nc", ".2dm", ".msh", ".vtu", ".vtk", ".pvd"}
RASTER_SUFFIXES = {".tif", ".tiff", ".asc", ".vrt"}
VECTOR_SUFFIXES = {".gpkg", ".shp", ".geojson", ".json"}
TABLE_SUFFIXES = {".csv", ".xlsx"}

GROUP_NAME = "hydromate"


@dataclass
class ResultItem:
    """One loadable thing, from the manifest or discovered."""

    name: str
    path: Path
    kind: str = "file"
    style: str = ""
    variable: str = ""
    description: str = ""

    @property
    def loadable(self) -> bool:
        """Whether QGIS can put this on the map at all.

        A CSV of fluxes and an xlsx convergence report are real results and are listed,
        but they open in a spreadsheet, not the canvas - so they are offered as files to
        open rather than silently skipped or wrongly added as an empty table layer.
        """
        return self.kind in {"mesh", "raster", "vector"}


@dataclass
class JobResults:
    job_id: str
    root: Path
    items: list[ResultItem] = field(default_factory=list)
    objective: float | None = None
    summary: dict = field(default_factory=dict)
    from_manifest: bool = True

    @property
    def layers(self) -> list[ResultItem]:
        return [i for i in self.items if i.loadable]

    @property
    def documents(self) -> list[ResultItem]:
        return [i for i in self.items if not i.loadable]


def discover(job_root: str | os.PathLike) -> JobResults:
    """What this job produced. Never raises - an unreadable job shows as empty."""
    root = Path(job_root)
    manifest = root / "results" / "results.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            return _from_manifest(root, payload)
    return _by_globbing(root)


def _from_manifest(root: Path, payload: dict) -> JobResults:
    items = []
    for entry in payload.get("results") or []:
        raw = Path(str(entry.get("path", "")))
        # Manifest paths are relative to the job directory so a restored archive under a
        # different mount still opens.
        path = raw if raw.is_absolute() else (root / raw)
        if not path.exists():
            continue
        items.append(ResultItem(
            name=str(entry.get("name") or path.name),
            path=path,
            kind=str(entry.get("kind") or _kind_of(path)),
            style=str(entry.get("style") or ""),
            variable=str(entry.get("variable") or ""),
            description=str(entry.get("description") or ""),
        ))
    return JobResults(job_id=str(payload.get("job_id") or root.name), root=root,
                      items=items, objective=payload.get("objective"),
                      summary=dict(payload.get("summary") or {}))


def _by_globbing(root: Path) -> JobResults:
    """Fallback for a job with no manifest, so the data is still reachable."""
    items: list[ResultItem] = []
    seen: set[Path] = set()
    for folder in (root / "results", root / "results" / "qgis", root / "input"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path in seen:
                continue
            kind = _kind_of(path)
            if kind == "file":
                continue
            seen.add(path)
            items.append(ResultItem(name=path.stem, path=path, kind=kind,
                                    style=_guess_style(path)))
    return JobResults(job_id=root.name, root=root, items=items, from_manifest=False)


def _kind_of(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MESH_SUFFIXES:
        return "mesh"
    if suffix in RASTER_SUFFIXES:
        return "raster"
    if suffix in VECTOR_SUFFIXES:
        return "vector"
    if suffix in TABLE_SUFFIXES:
        return "table"
    if suffix in {".png", ".jpg", ".pdf", ".svg"}:
        return "figure"
    return "file"


def _guess_style(path: Path) -> str:
    name = path.name.lower()
    if "depth" in name:
        return "water-depth"
    if "veloc" in name:
        return "velocity"
    return ""


# ------------------------------------------------------------------------- loading


class ResultLoader:
    """Puts a job's results on the map, grouped and styled."""

    def __init__(self, project: QgsProject | None = None, *,
                 min_depth: float = styles.DEFAULT_MIN_DEPTH,
                 velocity_cap: float = styles.DEFAULT_VELOCITY_MAX) -> None:
        self.project = project or QgsProject.instance()
        self.min_depth = float(min_depth)
        self.velocity_cap = float(velocity_cap)
        self.warnings: list[str] = []

    def load(self, results: JobResults, *, items=None) -> list:
        """Add *results* to the layer tree. Returns the layers actually added."""
        self.warnings = []
        targets = list(items if items is not None else results.layers)
        if not targets:
            return []
        group = self._group(results.job_id)
        added = []
        for item in targets:
            layer = self._layer_for(item)
            if layer is None:
                self.warnings.append(f"{item.name}: QGIS could not open {item.path.name}")
                continue
            # Validity is checked before adding, or an invalid layer sits in the tree
            # looking like a result that failed rather than one that never opened.
            if not layer.isValid():
                self.warnings.append(
                    f"{item.name}: {item.path.name} did not load "
                    f"({layer.error().summary() if hasattr(layer, 'error') else 'invalid'})")
                continue
            self._style(layer, item)
            self.project.addMapLayer(layer, False)
            group.addLayer(layer)
            added.append(layer)
        return added

    def _group(self, job_id: str):
        root = self.project.layerTreeRoot()
        parent = root.findGroup(GROUP_NAME) or root.insertGroup(0, GROUP_NAME)
        return parent.findGroup(job_id) or parent.addGroup(job_id)

    def _layer_for(self, item: ResultItem):
        path = str(item.path)
        if item.kind == "mesh":
            # MDAL reads SELAFIN natively, which is exactly why the runner converts
            # nothing: a copy of a multi-hundred-megabyte result would buy nothing.
            return QgsMeshLayer(path, item.name, "mdal")
        if item.kind == "raster":
            return QgsRasterLayer(path, item.name)
        if item.kind == "vector":
            return QgsVectorLayer(path, item.name, "ogr")
        return None

    def _style(self, layer, item: ResultItem) -> None:
        if not isinstance(layer, QgsMeshLayer) or not item.style:
            return
        try:
            self._style_mesh(layer, item)
        except Exception as exc:  # noqa: BLE001
            # Styling is a nicety; an unstyled layer the user can restyle beats no layer.
            self.warnings.append(f"{item.name}: default styling failed ({exc})")

    def _style_mesh(self, layer: QgsMeshLayer, item: ResultItem) -> None:
        index = _dataset_index(layer, item.variable)
        if index is None:
            self.warnings.append(
                f"{item.name}: no dataset called {item.variable!r} in this mesh")
            return
        renderer = layer.rendererSettings()
        if item.style == "water-depth":
            maximum = _dataset_maximum(layer, index) or 1.0
            style = styles.DepthStyle(minimum=self.min_depth, maximum=maximum)
            renderer.setActiveScalarDatasetGroup(index)
            renderer.setScalarSettings(index, styles.depth_settings(style))
        elif item.style == "velocity":
            observed = _dataset_maximum(layer, index)
            style = styles.clamp_velocity(observed, self.velocity_cap)
            if style.warning:
                self.warnings.append(f"{item.name}: {style.warning}")
            renderer.setActiveScalarDatasetGroup(index)
            renderer.setScalarSettings(index, styles.velocity_scalar_settings(style))
            vector_index = _vector_index(layer)
            if vector_index is not None:
                renderer.setActiveVectorDatasetGroup(vector_index)
                renderer.setVectorSettings(vector_index,
                                           styles.velocity_vector_settings(style))
        else:
            return
        layer.setRendererSettings(renderer)
        layer.triggerRepaint()


def _dataset_index(layer: QgsMeshLayer, variable: str) -> int | None:
    """Find a dataset group by name, tolerantly.

    SELAFIN names are fixed-width and often arrive padded ('WATER DEPTH     '), and a
    3D result may prefix them. An exact match would fail on files that are perfectly
    fine, so the comparison is trimmed and case-insensitive.
    """
    if not variable:
        return None
    wanted = variable.strip().upper()
    for index in layer.datasetGroupsIndexes():
        meta = layer.datasetGroupMetadata(index)
        name = (meta.name() or "").strip().upper()
        if name == wanted:
            return index
    for index in layer.datasetGroupsIndexes():
        name = (layer.datasetGroupMetadata(index).name() or "").strip().upper()
        if wanted in name:
            return index
    return None


def _vector_index(layer: QgsMeshLayer) -> int | None:
    for index in layer.datasetGroupsIndexes():
        meta = layer.datasetGroupMetadata(index)
        if meta.isVector():
            return index
    return None


def _dataset_maximum(layer: QgsMeshLayer, index: int) -> float | None:
    """The dataset's own maximum, so the scale fits the data rather than a guess."""
    try:
        meta = layer.datasetGroupMetadata(index)
        value = meta.maximum()
    except Exception:  # noqa: BLE001
        return None
    return float(value) if value is not None and value > 0 else None
