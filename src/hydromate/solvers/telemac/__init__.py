"""TELEMAC-2D / TELEMAC-3D (+ GAIA) backend.

Kept deliberately empty of imports: :mod:`hydromate.solvers.telemac.spec` must be
importable without pulling in gmsh, the SELAFIN writer or TELEMAC's Python, so that a
capability listing stays cheap. The implementation modules move in here in a later
phase; until then they live at their historic ``hydromate.<module>`` paths.
"""
