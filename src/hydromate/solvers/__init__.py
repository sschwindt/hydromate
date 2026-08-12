"""Simulation backends.

Each subpackage is one simulation code. A backend supplies only what is genuinely its
own - its mesh format, its steering/dictionary files, its runtime and its result
readers - and takes the geodata, boundary conditions, ground truth and convergence
maths from :mod:`hydromate.core`.

Every backend exposes a ``spec`` module holding a
:class:`~hydromate.core.registry.BackendSpec`. That module is **import-light on
purpose**: it is what ``hydromate status`` and a future QGIS plugin read to learn what
the backend can do, and it must not pull in the solver's own dependencies to answer.
"""
