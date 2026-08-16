"""Concrete :class:`~hydromate.jobs.launcher.JobLauncher` implementations.

One module per mechanism, imported lazily by
:func:`hydromate.jobs.launcher.select_launcher` so that ``ctypes``/``kernel32`` work in
the Windows launcher never has to import on Linux and vice versa.
"""
