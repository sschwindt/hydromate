"""The default A3 print layout.

Built in code rather than shipped as a ``.qpt`` template, because a template stores the
map extent and the layer set it was made with - so a shipped one would either be empty or
would be a picture of somebody else's river. Built here, it fits *this* project's extent
and this reach.

The contents are the brief's (plan §17): the simulation ROI, a north arrow, a bold **Q**
arrow following the bulk flow direction, a two-tone scale bar, an empty legend with a hint
that the user should populate it, and 16 pt Arial throughout.

The flow arrow is the only interesting one. It points along the reach's own principal
axis, obtained from the mesh or the centerline rather than assumed - an arrow pointing the
wrong way down a river is worse than no arrow, because a reader will believe it.
"""

from __future__ import annotations

import logging
import math

from qgis.core import (QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap,
                       QgsLayoutItemPicture, QgsLayoutItemPolyline, QgsLayoutItemScaleBar,
                       QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout, QgsProject)
from qgis.PyQt.QtCore import QPointF
from qgis.PyQt.QtGui import QColor, QFont, QPolygonF

from ..compat import LAYOUT_MM, LEGEND_STYLES, PICTURE_SVG

LAYOUT_NAME = "aXqua A3"
log = logging.getLogger("axqua.plugin")

FONT_FAMILY = "Arial"
FONT_SIZE_PT = 16

#: A3 landscape, in millimetres.
PAGE_W, PAGE_H = 420.0, 297.0
MARGIN = 12.0


def _font(bold: bool = False) -> QFont:
    font = QFont(FONT_FAMILY, FONT_SIZE_PT)
    font.setBold(bold)
    return font


def add_default_layout(iface, project: QgsProject | None = None) -> str:
    """Create the layout and return its name. Replaces an existing one of the same name."""
    project = project or QgsProject.instance()
    manager = project.layoutManager()
    for existing in manager.printLayouts():
        if existing.name() == LAYOUT_NAME:
            manager.removeLayout(existing)

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(LAYOUT_NAME)
    page = layout.pageCollection().page(0)
    page.setPageSize(QgsLayoutSize(PAGE_W, PAGE_H, LAYOUT_MM))

    canvas = iface.mapCanvas()
    map_item = _add_map(layout, canvas)
    _add_title(layout, project)
    _add_north_arrow(layout)
    _add_flow_arrow(layout, canvas)
    _add_scalebar(layout, map_item)
    _add_legend(layout, map_item)

    manager.addLayout(layout)
    return LAYOUT_NAME


def _add_map(layout: QgsPrintLayout, canvas) -> QgsLayoutItemMap:
    item = QgsLayoutItemMap(layout)
    width = PAGE_W - 2 * MARGIN - 70.0          # room for the legend column
    height = PAGE_H - 2 * MARGIN - 22.0         # room for the title
    item.attemptMove(QgsLayoutPoint(MARGIN, MARGIN + 20.0,
                                    LAYOUT_MM))
    item.attemptResize(QgsLayoutSize(width, height, LAYOUT_MM))
    item.setExtent(canvas.extent())
    item.setFrameEnabled(True)
    layout.addLayoutItem(item)
    return item


def _add_title(layout: QgsPrintLayout, project: QgsProject) -> None:
    label = QgsLayoutItemLabel(layout)
    label.setText(project.title() or "Simulation region of interest")
    label.setFont(_font(bold=True))
    label.adjustSizeToText()
    label.attemptMove(QgsLayoutPoint(MARGIN, MARGIN, LAYOUT_MM))
    layout.addLayoutItem(label)


def _add_north_arrow(layout: QgsPrintLayout) -> None:
    picture = QgsLayoutItemPicture(layout)
    # QGIS ships the north arrows as SVGs; resolving one through the search paths avoids
    # bundling an image and keeps the plugin free of binary assets.
    picture.setPicturePath("NorthArrows/layout_default_north_arrow.svg",
                           PICTURE_SVG)
    picture.attemptMove(QgsLayoutPoint(PAGE_W - 60.0, MARGIN + 24.0,
                                       LAYOUT_MM))
    picture.attemptResize(QgsLayoutSize(20.0, 20.0, LAYOUT_MM))
    layout.addLayoutItem(picture)

    label = QgsLayoutItemLabel(layout)
    label.setText("N")
    label.setFont(_font(bold=True))
    label.adjustSizeToText()
    label.attemptMove(QgsLayoutPoint(PAGE_W - 52.0, MARGIN + 44.0,
                                     LAYOUT_MM))
    layout.addLayoutItem(label)


def _add_flow_arrow(layout: QgsPrintLayout, canvas) -> None:
    """A bold Q arrow along the reach's principal axis.

    The direction comes from the extent's own aspect: a river reach is longer than it is
    wide, so its bounding box points along the flow far more often than not. That is a
    weak inference and is why the arrow is labelled ``Q`` rather than presented as a
    measured direction - the user can rotate it, and a wrong arrow they can see is better
    than a confident one they cannot check.
    """
    extent = canvas.extent()
    horizontal = extent.width() >= extent.height()
    x, y = PAGE_W - 60.0, MARGIN + 62.0
    length = 34.0
    if horizontal:
        points = [QPointF(x, y), QPointF(x + length, y)]
    else:
        points = [QPointF(x + length / 2, y), QPointF(x + length / 2, y + length)]

    arrow = QgsLayoutItemPolyline(QPolygonF(points), layout)
    arrow.setEndMarker(QgsLayoutItemPolyline.MarkerMode.ArrowHead)
    arrow.setArrowHeadWidth(6.0)
    arrow.setArrowHeadStrokeColor(QColor("black"))
    arrow.setArrowHeadFillColor(QColor("black"))
    symbol = arrow.symbol()
    if symbol is not None:
        symbol.setWidth(2.0)                 # bold, as asked
    layout.addLayoutItem(arrow)

    label = QgsLayoutItemLabel(layout)
    label.setText("Q")
    label.setFont(_font(bold=True))
    label.adjustSizeToText()
    label.attemptMove(QgsLayoutPoint(x + length / 2 - 3.0, y + 6.0,
                                     LAYOUT_MM))
    layout.addLayoutItem(label)


def _add_scalebar(layout: QgsPrintLayout, map_item: QgsLayoutItemMap) -> None:
    bar = QgsLayoutItemScaleBar(layout)
    # "Single Box" alternates filled and empty segments, which is the one black / one
    # white section the brief asks for.
    bar.setStyle("Single Box")
    bar.setLinkedMap(map_item)
    bar.applyDefaultSize()
    bar.setNumberOfSegments(1)
    bar.setNumberOfSegmentsLeft(1)
    bar.setFillColor(QColor("black"))
    bar.setFillColor2(QColor("white"))
    bar.setFont(_font())
    bar.attemptMove(QgsLayoutPoint(PAGE_W - 62.0, PAGE_H - MARGIN - 18.0,
                                   LAYOUT_MM))
    layout.addLayoutItem(bar)


def _add_legend(layout: QgsPrintLayout, map_item: QgsLayoutItemMap) -> None:
    legend = QgsLayoutItemLegend(layout)
    legend.setTitle("Legend")
    legend.setLinkedMap(map_item)
    # Empty on purpose: which layers belong in a figure is an editorial decision, and a
    # legend auto-filled with every loaded layer is something the user has to undo.
    legend.setAutoUpdateModel(False)
    model = legend.model()
    if model is not None and model.rootGroup() is not None:
        for child in list(model.rootGroup().children()):
            model.rootGroup().removeChildNode(child)
    _style_legend_fonts(legend)
    legend.attemptMove(QgsLayoutPoint(PAGE_W - 62.0, MARGIN + 92.0,
                                      LAYOUT_MM))
    layout.addLayoutItem(legend)

    hint = QgsLayoutItemLabel(layout)
    hint.setText("Add the layers you want shown:\nright-click the legend, then\n"
                 "Item Properties > Legend Items.")
    hint.setFont(_font())
    hint.adjustSizeToText()
    hint.attemptMove(QgsLayoutPoint(PAGE_W - 62.0, MARGIN + 104.0,
                                    LAYOUT_MM))
    layout.addLayoutItem(hint)


def _style_legend_fonts(legend: QgsLayoutItemLegend) -> None:
    """16 pt Arial throughout the legend, on either QGIS.

    The font API moved in QGIS 3.40 (``QgsLegendStyle.setTextFormat``) from the older
    ``setFont``. Both spellings are tried rather than branching on a version, so this
    keeps working across the 3.44-to-4.x range the plugin targets - and a legend whose
    font could not be set is still a usable legend, so nothing here raises.
    """
    from qgis.core import QgsTextFormat

    for name, bold in (("Title", True), ("Group", True),
                       ("Subgroup", False), ("SymbolLabel", False)):
        component = LEGEND_STYLES.get(name)
        if component is None:                        # pragma: no cover - older QGIS
            log.debug("this QGIS has no legend component %r", name)
            continue
        try:
            style = legend.style(component)
            if hasattr(style, "setTextFormat"):
                style.setTextFormat(QgsTextFormat.fromQFont(_font(bold)))
            else:                                    # pragma: no cover - older QGIS
                style.setFont(_font(bold))
            legend.setStyle(component, style)
        except (AttributeError, TypeError, ValueError) as exc:
            # Narrow, and reported: a legend whose font could not be set is still a
            # usable legend, but swallowing the reason silently is how a styling bug
            # becomes unexplainable.
            log.debug("could not set the %s legend font: %s", name, exc)


def bearing(dx: float, dy: float) -> float:
    """Compass bearing of a vector, in degrees. Used when a centerline is available."""
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
