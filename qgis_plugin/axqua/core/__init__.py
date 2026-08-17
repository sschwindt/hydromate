"""Solver-agnostic plugin logic: the runner client, the project file, results.

Nothing in here imports ``axqua`` - the plugin talks to it over the CLI and over
files, which is what lets QGIS's Python and the solver's Python be different
installations entirely.
"""
