"""OpenFOAM ``interFoam`` free-surface backend.

Kept deliberately empty of imports, for the same reason as the TELEMAC backend: the
:mod:`hydromate.solvers.openfoam.spec` module is read to build a capability table and
must not import the extension itself. The implementation currently lives at
``hydromate.openfoam`` and moves here in a later phase.
"""
