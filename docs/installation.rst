Installation
============

Requirements
------------

``hydromate`` builds and runs solver cases; the solvers themselves are external software that is **not** a Python dependency:

* **Python ≥ 3.10**
* A working **TELEMAC** installation (e.g. ``telemac2d``), reachable through its ``pysource.*.sh`` environment script - or, on Windows, its ``pysource.bat``. ``hydromate`` does not import TELEMAC's Python; it *enters* that environment when the solver or SELAFIN tooling is needed (see :doc:`usage`).
* Optionally **OpenFOAM**, for the two-phase free-surface path. hydromate targets the **Foundation** build (`openfoam.org <https://openfoam.org/>`_), which is what its dictionary writer is verified against; on Windows that means running it **through WSL**, since Foundation ships no native Windows build.
* **HydroBayesCal** for the calibration step, installed with the ``calibration`` extra (see below).

The case-building pipeline itself depends on a geospatial / meshing stack:

* ``numpy``, ``pandas``, ``scipy``
* ``pyproj``, ``shapely`` (≥ 2.0), ``geopandas``, ``rasterio`` (with GDAL)
* ``gmsh`` (≥ 4.13) for mesh generation
* ``pyyaml`` for the configuration

Environment
-----------

The pipeline runs in its **own** conda/mamba environment, kept separate from the TELEMAC installation and from any other project environment. This isolation is deliberate: the geospatial/meshing stack and TELEMAC's interpreter never share a process. Create it from the provided ``environment.yml``:

.. code-block:: bash

   mamba env create -f environment.yml
   mamba activate hydromate-env

The environment is named ``hydromate-env`` and pulls ``gdal``, ``geopandas``, ``rasterio``, ``shapely``, ``pyproj`` from conda-forge and ``gmsh`` from PyPI.

Install the package
-------------------

Install ``hydromate`` into that environment in editable mode from a clone:

.. code-block:: bash

   git clone https://github.com/sschwindt/hydromate.git
   cd hydromate
   pip install -e .

This exposes the ``hydromate`` command-line entry point and the importable ``hydromate`` package.

Optional developer tools (tests, linting):

.. code-block:: bash

   pip install -e ".[dev]"

The optional browser-based configuration editor (see :doc:`usage`) needs Streamlit, installed via the ``gui`` extra:

.. code-block:: bash

   pip install -e ".[gui]"

The calibration step pulls in a surrogate-modelling stack (bayesvalidrox, gpytorch,
scikit-learn) that is irrelevant to building or running a case, so it is a separate extra:

.. code-block:: bash

   pip install -e ".[calibration]"

Verify
------

.. code-block:: bash

   hydromate --version
   mamba run -n hydromate-env pytest tests/   # end-to-end test on synthetic inputs

The test suite builds a complete synthetic case (mesh, boundary, steering, friction, calibration CSV) without invoking the TELEMAC solver, so it runs anywhere the environment is installed.

Configure the TELEMAC link
--------------------------

In your case configuration (``cases/example-Inn/case-config.yml``), point ``telemac.pysource`` at the real environment script of your TELEMAC installation, for example:

.. code-block:: yaml

   telemac:
     pysource: /home/user/opt/telemac/configs/pysource.gfortran.sh
     solver: telemac2d
     n_processors: 4

Run ``hydromate config/your-case.yml --check`` to validate that the configuration and the TELEMAC environment resolve before building anything.

For OpenFOAM, point ``openfoam.bashrc`` at the install's ``etc/bashrc`` in the same way.

Solver profiles (optional)
--------------------------

If you use more than one solver installation - or want a scratch volume and a core count
recorded once rather than per case - describe them in ``~/.config/hydromate/profiles.yml``
and check them with:

.. code-block:: bash

   hydromate profiles validate

This is worth doing **when you write the profile** rather than when you submit a long job.
It also warns when a solver variable came from your interactive shell rather than from the
setup script - a detached job does not inherit an interactive environment, which is the
classic "works in my terminal, fails in the runner" trap. See :doc:`jobs`.

The QGIS plugin
---------------

The **HydroMate** QGIS plugin is a frontend for the same tool: it submits jobs, monitors
them and loads results, and runs no solver itself.

Install it from the QGIS plugin repository (*Plugins ▸ Manage and Install Plugins*, with
*Show also experimental plugins* ticked), or from the zip attached to a
`GitHub release <https://github.com/sschwindt/hydromate/releases>`_.

.. important::

   **QGIS's Python does not need to be - and should not be - the solver's Python.** The
   plugin talks to the ``hydromate`` command-line tool as a subprocess, so install
   hydromate in the environment that reaches your solver and simply tell the plugin where
   it is. Nothing about QGIS has to change.

Requires QGIS 3.44 or newer, including QGIS 4.x. See :doc:`qgis_plugin`.
