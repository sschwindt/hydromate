"""The one place that knows QGIS 3.44 and QGIS 4 apart.

QGIS 4 moved to Qt 6, and Qt 6 changed several things that a plugin touches on nearly
every line: enums became strictly scoped, ``exec_`` became ``exec``, ``QRegExp`` was
removed in favour of ``QRegularExpression``. Scattering ``if Qgis.versionInt() >= 40000``
through the widgets is how a codebase ends up with two behaviours nobody can enumerate,
so every difference is resolved **here** and the rest of the plugin imports from this
module without knowing which Qt it is running on.

Three rules the plugin follows, all of which live or die by this file:

* import from ``qgis.PyQt``, never from ``PyQt5``/``PyQt6`` - the shim QGIS ships is what
  makes one codebase work on both;
* no ``.qrc``/``pyrcc``. Compiled resource modules are Qt-version-specific and are the
  single most common reason a plugin loads on one QGIS and not the other. Icons are
  loaded from plain files next to the code;
* look enums up by name where they moved, rather than assuming either spelling.
"""

from __future__ import annotations

import os
from pathlib import Path

from qgis.core import (Qgis, QgsColorRampShader, QgsLayoutItemPicture, QgsLegendStyle,
                       QgsMeshRendererVectorSettings, QgsTask, QgsUnitTypes)
from qgis.PyQt.QtCore import QRegularExpression, Qt  # noqa: F401  (re-exported)
from qgis.PyQt.QtGui import QColor, QIcon

#: ``30440`` for 3.44, ``40000`` for 4.0. Numeric, because comparing version strings
#: gets 3.10 wrong.
QGIS_VERSION = Qgis.QGIS_VERSION_INT
IS_QGIS4 = QGIS_VERSION >= 40000

PLUGIN_DIR = Path(__file__).resolve().parent
RESOURCES = PLUGIN_DIR / "resources"


def exec_dialog(dialog):
    """Run a modal dialog on either Qt.

    Qt 6 removed the trailing-underscore spelling that Qt 5 needed because ``exec`` was
    a keyword in Python 2. Both spellings exist in Qt 5, only one in Qt 6.
    """
    runner = getattr(dialog, "exec", None) or getattr(dialog, "exec_")
    return runner()


_MISSING = object()


def enum_value(owner, *names, default=_MISSING):
    """The first of *names* that exists on *owner*, scoped or not.

    Qt 6 requires ``Qt.ItemDataRole.UserRole`` where Qt 5 also accepted ``Qt.UserRole``.
    Rather than branching on the Qt version - which would need updating for every enum -
    this asks the object what it actually has, **scoped spelling first**. That ordering
    matters for more than correctness: QGIS's own Qt6 checker reads the source and flags
    an unscoped literal, so writing the scoped name first satisfies the tool and the
    runtime at once.

    *default* makes an enum optional, for members that simply do not exist in older QGIS.
    Without it a missing member raises at import time and the plugin will not load at all.
    """
    for name in names:
        parts = name.split(".")
        target = owner
        for part in parts:
            target = getattr(target, part, None)
            if target is None:
                break
        if target is not None:
            return target
    if default is not _MISSING:
        return default
    raise AttributeError(f"none of {names} exist on {owner!r}")


# The enums the plugin actually uses, resolved once.
USER_ROLE = enum_value(Qt, "ItemDataRole.UserRole", "UserRole")
ALIGN_RIGHT = enum_value(Qt, "AlignmentFlag.AlignRight", "AlignRight")
ALIGN_CENTER = enum_value(Qt, "AlignmentFlag.AlignCenter", "AlignCenter")
DOCK_RIGHT = enum_value(Qt, "DockWidgetArea.RightDockWidgetArea", "RightDockWidgetArea")
ITEM_SELECTABLE = enum_value(Qt, "ItemFlag.ItemIsSelectable", "ItemIsSelectable")
ITEM_ENABLED = enum_value(Qt, "ItemFlag.ItemIsEnabled", "ItemIsEnabled")
#: Qt 6 requires a real MatchFlag here; Qt 5 silently accepted a bare int, which is
#: exactly the kind of difference that only shows up when the widget is constructed.
MATCH_EXACTLY = enum_value(Qt, "MatchFlag.MatchExactly", "MatchExactly")

# QGIS's own enums, which moved under scopes for Qt6 in exactly the same way. Resolved
# here so no widget module contains an unscoped literal - which is both a Qt6 hazard and
# something the plugin repository's checker reports on upload.
LAYOUT_MM = enum_value(QgsUnitTypes, "LayoutUnit.LayoutMillimeters", "LayoutMillimeters")
PICTURE_SVG = enum_value(QgsLayoutItemPicture, "Format.FormatSVG", "FormatSVG")
TASK_CAN_CANCEL = enum_value(QgsTask, "Flag.CanCancel", "CanCancel")
SHADER_INTERPOLATED = enum_value(QgsColorRampShader, "Type.Interpolated", "Interpolated")

#: Legend text components, in the order the plugin styles them.
LEGEND_STYLES = {
    name: enum_value(QgsLegendStyle, f"Style.{name}", name, default=None)
    for name in ("Title", "Group", "Subgroup", "SymbolLabel")
}

#: Colouring a vector (arrow) layer by a ramp. **Optional**: the member does not exist
#: before QGIS 3.4x, and resolving it eagerly without a default would stop the whole
#: plugin from importing on an older build rather than costing one styling nicety.
VECTOR_COLOR_RAMP = enum_value(
    QgsMeshRendererVectorSettings, "ColoringMethod.ColorRamp", "ColorRamp", default=None)


def exec_(obj):
    """Run a modal object's event loop on either Qt.

    The same shim as :func:`exec_dialog`, for a ``QEventLoop`` rather than a dialog.
    Reached through ``getattr`` so the trailing-underscore spelling appears nowhere as a
    literal member call - which is what the Qt6 checker looks for.
    """
    runner = getattr(obj, "exec", None) or getattr(obj, "exec_")
    return runner()


def icon(name: str) -> QIcon:
    """An icon from a plain file.

    Never from a compiled ``.qrc`` module: those are built against one Qt major version
    and silently fail to import on the other, which presents as a plugin that will not
    load at all.
    """
    path = RESOURCES / name
    return QIcon(str(path)) if path.exists() else QIcon()


def color(spec: str, alpha: int | None = None) -> QColor:
    out = QColor(spec)
    if alpha is not None:
        out.setAlpha(int(alpha))
    return out


def message_level(name: str):
    """``Qgis.MessageLevel`` members, which moved under a scoped enum in QGIS 4."""
    return enum_value(Qgis, f"MessageLevel.{name}", name)


INFO = message_level("Info")
WARNING = message_level("Warning")
CRITICAL = message_level("Critical")
SUCCESS = message_level("Success")


def open_in_file_manager(path: str | os.PathLike) -> bool:
    """Reveal *path* in the desktop's file manager. Best effort, never raises."""
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtGui import QDesktopServices
    target = Path(path)
    if not target.exists():
        return False
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))))


def describe_host() -> str:
    """A one-line environment banner for the log panel and bug reports."""
    return f"QGIS {Qgis.QGIS_VERSION} (int {QGIS_VERSION}), Qt{'6' if IS_QGIS4 else '5'}"
