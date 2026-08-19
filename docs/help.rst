Help: troubleshooting and tips
==============================

Tips that save time
-------------------

* **Iterate on a coarse mesh.** The defaults are high-resolution - a ~0.3 km² reach yields on the order of half a million elements. Raise ``Max Edge Length (m)`` on the mesh zones (or the ``mesh`` block fallbacks) while you are still checking boundaries, roughness and the initial condition, and only refine once the model runs.
* **Validate before you build.** ``axqua <config> --check`` loads the configuration, checks that every input exists and that the TELEMAC environment can be sourced, and exits. It costs a second and catches most of what would otherwise fail several minutes into a build.
* **Look at the build log before spending compute.** Everything that depends on the geometry - the initial condition, the outflow rating, the water table of a gain-lose reach - is decided and logged during preprocessing, so it can be checked before any solver starts.
* **Reach a working OpenFOAM run through the rigid lid.** ``openfoam.mode: rigid-lid`` with a ``cell_size_factor`` of 3 to 5 turns a run of ~100,000 time steps into one of ~1,000, which is how a mesh problem or a misplaced inlet is found in minutes rather than days (:ref:`the rigid lid <usage-rigid-lid>`).
* **Filter ParaView at ``hydrodynamics.wet_depth``.** The wetted-extent report uses that threshold (0.01 m by default), so a picture drawn without it will disagree with the report for no real reason.
* **Debug a failing job in the foreground.** ``axqua execute <JOB_ID>`` runs exactly the code path a detached job takes, but synchronously and in your terminal.
* **Draw over a hillshade.** It makes the channel banks, bars and structures visible, and is the difference between guessing a centerline and tracing one (see :doc:`preparation`).

Installation
------------

ModuleNotFoundError: No module named 'axqua'
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If the terminal says

.. code-block:: bash

   from axqua import ...
   ModuleNotFoundError: No module named 'axqua'

then the package is not importable because it was never installed into the environment. aXqua uses a ``src/`` layout - the code lives in ``src/axqua/``, not ``axqua/`` - so being in the repository root does not put the package on Python's path, and the scripts only work after the package is pip-installed into the environment. With ``axqua-env`` activated:

.. code-block:: bash

   cd /path/to/axqua
   pip install -e .

Alternatively ``pip install -e ".[dev,gui]"``. The ``-e`` (editable) install links the environment to ``src/axqua/``, so any edits or git pulls in that folder take effect immediately without reinstalling. One quick sanity check afterwards:

.. code-block:: bash

   python -c "import axqua; print(axqua.__file__)"

ImportError: libjxl / pyogrio / fiona
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This has nothing to do with "pyogrio/fiona missing". They are present, but fail to import because a native shared library is missing:

.. code-block:: bash

   libjxl.so.0.11: cannot open shared object file

``libjxl`` is the JPEG XL library, pulled in through the GDAL raster/image drivers. GeoPandas ``read_file()`` uses either ``pyogrio`` or ``fiona`` as its engine, and both depend on GDAL-native libraries.

On Debian 12 (bookworm) the system package is ``libjxl0.7``, not ``libjxl0.11``, so installing Debian's normal ``libjxl`` package will not fix this. Debian 13 (trixie) has ``libjxl0.11``, but Debian 12 does not. Activate the environment and reinstall the whole geospatial binary stack from conda-forge, with strict channel priority:

.. code-block:: bash

   mamba activate axqua-env

   mamba install -c conda-forge --strict-channel-priority \
     geopandas pyogrio fiona gdal libgdal libjxl

If that does not solve it, force a consistent rebuild:

.. code-block:: bash

   mamba config --env --set channel_priority strict
   mamba config --env --add channels conda-forge

   mamba update --all
   mamba install --force-reinstall \
     geopandas pyogrio fiona gdal libgdal libjxl

Then check whether the needed library exists - you want to see something like ``$CONDA_PREFIX/lib/libjxl.so.0.11``:

.. code-block:: bash

   find "$CONDA_PREFIX" -name 'libjxl.so*'

To diagnose the broken link, find the extension modules and run ``ldd`` on them:

.. code-block:: bash

   python - <<'PY'
   import importlib.util
   for mod in ["pyogrio._io", "fiona.ogrext"]:
       spec = importlib.util.find_spec(mod)
       print(mod, "=>", spec.origin if spec else "not found")
   PY

   ldd /path/to/pyogrio/_io*.so | grep -E 'jxl|not found|gdal'
   ldd /path/to/fiona/ogrext*.so | grep -E 'jxl|not found|gdal'

If you see ``libjxl.so.0.11 => not found``, the binary package is linked against ``libjxl 0.11`` but your environment does not contain that matching shared library. The most reliable fix is often a clean environment, pinning ``libjxl`` back to 0.11:

.. code-block:: bash

   mamba create -n geo312 -c conda-forge --strict-channel-priority \
     python=3.12 geopandas pyogrio fiona gdal rasterio shapely pyproj rtree
   mamba activate geo312
   mamba install -c conda-forge --strict-channel-priority "libjxl=0.11.*"
   find "$CONDA_PREFIX" -name 'libjxl.so*' -print

Then test it:

.. code-block:: bash

   python - <<'PY'
   import geopandas as gpd
   import pyogrio
   import fiona
   print("OK")
   PY

Jobs
----

A job is stuck in ``RUNNING``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

It will not stay that way. Any status or list reconciles a recorded process that has gone - after a crash, a reboot, an OOM kill, or ``wsl --shutdown`` - and reports ``FAILED`` with an explanation rather than leaving a job that can never finish:

.. code-block:: bash

   axqua status <JOB_ID>
   axqua list --rebuild        # also re-indexes moved or restored job folders

A job fails the moment it starts
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read ``runner.log`` first - it carries the error with a stable ``code`` and a suggested remedy. Almost always this is the solver environment:

.. code-block:: bash

   axqua logs <JOB_ID>
   axqua profiles validate

Watch for the *ambient* warning. A variable that came from your interactive shell (say ``/etc/profile.d/openfoam9.sh``) is inherited by anything you launch from a terminal but **not** by a detached job - so the same case works by hand and fails when submitted. The fix is to make the setup script itself export it.

To see the failure under a terminal, run the job synchronously - the same code path a detached job takes:

.. code-block:: bash

   axqua execute <JOB_ID>

``the systemd launcher is not available``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A bare SSH session without lingering has ``systemd-run`` on ``PATH`` but no user manager at all. Either enable lingering (``loginctl enable-linger $USER``) or use the fallback, which needs nothing:

.. code-block:: bash

   axqua submit ... --launcher posix

Jobs vanish after ``wsl --shutdown``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Expected, and not something aXqua can prevent: the distro's lifetime is not ours. ``wsl --shutdown``, a Windows restart, or WSL2 idling the VM out terminates everything running inside it. Those jobs recover to ``FAILED`` rather than a stuck ``RUNNING``, and can be resubmitted.

The QGIS plugin
---------------

``axqua could not be found``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The message names all three places it looked - the plugin setting, ``$AXQUA_EXE`` and ``PATH``. Set the path in *aXqua ▸ Settings* and press *Test*. There is deliberately no silent fallback to another interpreter, because a wrong one fails obscurely much later. Remember that axqua belongs in the environment that reaches your **solver**, not in QGIS's Python.

The panel shows only Setup and Jobs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The capability tabs are generated from the case, so there is no case selected yet, or aXqua could not read it. Check the same thing the plugin does:

.. code-block:: bash

   axqua case-status <config.yml> --json

A layer loaded but is not styled
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The dataset name in the result did not match what the manifest expected. The file itself is fine - style it from Layer Properties.

Reporting a problem
~~~~~~~~~~~~~~~~~~~

Include the aXqua version, the QGIS version, your platform, and the relevant log. The plugin's messages are written to the QGIS message log under the *aXqua* tab, and every job keeps its own ``runner.log`` next to its outputs (see :doc:`jobs`).
