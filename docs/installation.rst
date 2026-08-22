Installation
============

Three things get installed, in this order: the **simulation software** you intend to run, the **axqua** package that builds and drives cases for it, and the **QGIS plugin** that is the normal way to use both. The plugin is a frontend only - it runs no solver and does no numerics - so the first two are what actually do the work, and the plugin can be added or skipped at any time.

Simulation software
-------------------

The solvers are external software and **not** Python dependencies. Install at least one; TELEMAC is the one to start with, because the OpenFOAM path uses a TELEMAC run to seed itself.

**TELEMAC** (required for the depth-averaged path, and for anything that follows it)
    A working installation reachable through its ``pysource.*.sh`` environment script - or, on Windows, its ``pysource.bat``. axqua does not import TELEMAC's Python: it *enters* that environment in a subshell whenever the solver or the SELAFIN tooling is needed, which is what keeps the geospatial stack and TELEMAC's interpreter out of one process. GAIA comes with TELEMAC and needs no separate install.

**OpenFOAM** (optional, for the two-phase free-surface path)
    axqua targets the **Foundation** build (`openfoam.org <https://openfoam.org/>`_), which is what its dictionary writer is verified against. On Windows that means running it **through WSL**, since Foundation ships no native Windows build.

**HydroBayesCal** (optional, for the Bayesian calibration step)
    Installed as a Python extra, see below. It pulls in a surrogate-modelling stack (bayesvalidrox, gpytorch, scikit-learn) that is irrelevant to building or running a case, which is why it is not installed by default.

**QGIS 3.44 or newer** (including QGIS 4.x), for the plugin.

You also need **Python ≥ 3.10** for axqua itself. The case-building pipeline depends on a geospatial and meshing stack - ``numpy``, ``pandas``, ``scipy``, ``pyproj``, ``shapely`` (≥ 2.0), ``geopandas``, ``rasterio`` (with GDAL), ``gmsh`` (≥ 4.13) and ``pyyaml`` - all of which come with the environment below.

The axqua environment
---------------------

The pipeline runs in its **own** conda/mamba environment, kept separate from the TELEMAC installation and from any other project environment. Create it from the provided ``environment.yml``:

.. code-block:: bash

   mamba env create -f environment.yml
   mamba activate axqua-env

The environment is named ``axqua-env`` and pulls ``gdal``, ``geopandas``, ``rasterio``, ``shapely`` and ``pyproj`` from conda-forge, and ``gmsh`` from PyPI.

Install the package
-------------------

Install ``axqua`` into that environment in editable mode from a clone:

.. code-block:: bash

   git clone https://github.com/sschwindt/aXqua.git
   cd axqua
   pip install -e .

This exposes the ``axqua`` command-line entry point and the importable ``axqua`` package. Four extras are available, and they compose (``pip install -e ".[gui,calibration]"``):

``dev``
    tests and linting.
``gui``
    the browser-based configuration editor (Streamlit), see :doc:`preprocessing`.
``calibration``
    HydroBayesCal and its surrogate-modelling stack, see :doc:`hbc`.
``mesh``
    extra mesh and geospatial IO used by some post-processing utilities.

Verify the installation:

.. code-block:: bash

   axqua --version
   mamba run -n axqua-env pytest tests/   # end-to-end test on synthetic inputs

The test suite builds a complete synthetic case - mesh, boundary, steering, friction, calibration CSV - without invoking a solver, so it runs anywhere the environment is installed.

Point axqua at the solvers
--------------------------

In your case configuration, point ``telemac.pysource`` at the real environment script of your TELEMAC installation:

.. code-block:: yaml

   telemac:
     pysource: /home/user/opt/telemac/configs/pysource.gfortran.sh
     solver: telemac2d
     n_processors: 4

For OpenFOAM, point ``openfoam.bashrc`` at the install's ``etc/bashrc`` in the same way. Then validate before building anything:

.. code-block:: bash

   axqua <your-case>/case-config.yml --check

If you use more than one solver installation - or want a scratch volume and a core count recorded once rather than per case - describe them as **solver profiles** in ``~/.config/axqua/profiles.yml`` and check them with ``axqua profiles validate``. This is worth doing when you *write* the profile rather than when you submit a long job: it also warns when a solver variable came from your interactive shell rather than from the setup script, which is the classic "works in my terminal, fails in the runner" trap (see :doc:`advanced`).

The QGIS plugin
---------------

The plugin drives everything above from QGIS: choose a case, build it, submit runs, watch them, and load the results as styled layers. It is the intended way to use aXqua day to day; :doc:`qgis_plugin` is the usage guide.

1. **Install axqua first**, in the environment that already reaches your solver - the steps above, or straight from the repository:

   .. code-block:: bash

      pip install git+https://github.com/sschwindt/aXqua.git
      axqua --version

2. **Install the plugin.** From the QGIS plugin repository (*Plugins ▸ Manage and Install Plugins*, with *Show also experimental plugins* ticked), or from the zip attached to a `GitHub release <https://github.com/sschwindt/aXqua/releases>`_ via *Install from ZIP*.

   For development, symlink the folder into your QGIS profile instead:

   .. code-block:: bash

      ln -s /path/to/axqua/qgis_plugin/axqua \
            ~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/axqua

3. **Point the plugin at axqua.** Open the aXqua panel and press *Check* on the Setup tab. If ``axqua`` is not on ``PATH``, set the path in *Settings…* - it is validated immediately, so a wrong path is caught here rather than when you submit a long run.

.. important::

   **QGIS's Python does not need to be - and should not be - the solver's Python.** The plugin talks to the ``axqua`` command-line tool as a subprocess, so install axqua in the environment that reaches your solver and simply tell the plugin where it is. Nothing about QGIS has to change, and the two can be updated independently.

Nothing is bundled with the plugin: no solver, no MPI, no scientific Python stack.
