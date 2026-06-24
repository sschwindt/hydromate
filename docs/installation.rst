Installation
============

Requirements
------------

``hydromate`` builds TELEMAC cases; running and calibrating them additionally needs external software that is **not** a Python dependency:

* **Python ≥ 3.10**
* A working **TELEMAC** installation (the solver, e.g. ``telemac2d``), reachable through its ``pysource.*.sh`` environment script. ``hydromate`` does not import TELEMAC's Python; it *sources* that script when the solver or SELAFIN tooling is needed (see :doc:`usage`).
* **HydroBayesCal** for the calibration step (`github.com/Ecohydraulics/hydrobayescal <https://github.com/Ecohydraulics/hydrobayescal>`_).

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

Verify
------

.. code-block:: bash

   hydromate --version
   mamba run -n hydromate-env pytest tests/   # end-to-end test on synthetic inputs

The test suite builds a complete synthetic case (mesh, boundary, steering, friction, calibration CSV) without invoking the TELEMAC solver, so it runs anywhere the environment is installed.

Configure the TELEMAC link
--------------------------

In your case configuration (``config/inn.yml``), point ``telemac.pysource`` at the real environment script of your TELEMAC installation, for example:

.. code-block:: yaml

   telemac:
     pysource: /home/user/opt/telemac/configs/pysource.gfortran.sh
     solver: telemac2d
     n_processors: 4

Run ``hydromate config/your-case.yml --check`` to validate that the configuration and the TELEMAC environment resolve before building anything.
