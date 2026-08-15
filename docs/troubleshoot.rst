Troubleshooting
===============

ModuleNotFoundError: No module named 'hydromate'
------------------------------------------------

If terminal says

.. code-block:: bash

   from hydromate import ...                                                                                                    
   ModuleNotFoundError: No module named 'hydromate'                         

then the package is not importable because it was never installed into the new environment. hydromate uses a src/ layout (the code lives in src/hydromate/, not hydromate/), so being in the repo root does not put the package on Python's path, which means the scripts only work after the package is pip-installed into the env. But while developing, with the hydromate-env activated, make sure to setup dependencies:

.. code-block:: bash

   cd /path/to/hydromate
   pip install -e .

Alternatively ``pip install -e ".[dev,gui]"``. The ``-e`` (editable) install just links the env to src/hydromate/, so any edits or git pulls in that folder take effect immediately without reinstalling.  One quick sanity check afterwards:

.. code-block:: bash

   python -c "import hydromate; print(hydromate.__file__)"



ImportError: libjxl / pyogrio / fiona
-------------------------------------

This has nothing to do with "pyogrio/fiona missing". They are present, but fail to import because a native shared library is missing:

.. code-block:: bash

   libjxl.so.0.11: cannot open shared object file


``libjxl`` is the JPEG XL library. In the geospatial stack, it is probably pulled in through GDAL/raster/image drivers. GeoPandas ``read_file()`` uses either ``pyogrio`` or ``fiona`` as its engine, and both depend on GDAL-native libraries. GeoPandas officially supports ``pyogrio`` and ``fiona`` as the two ``read_file()`` engines.

On Debian 12/bookworm, the system package is ``libjxl0.7``, not ``libjxl0.11``, so installing Debian’s normal ``libjxl`` package will not fix this. Debian 13/trixie has ``libjxl0.11``, but Debian 12 does not.

So on Debian12, aActivate the env and reinstall the whole geospatial binary stack from conda-forge, with strict channel priority:

.. code-block:: bash

   mamba activate hydromate-env

   mamba install -c conda-forge --strict-channel-priority \
     geopandas pyogrio fiona gdal libgdal libjxl


If that does not solve it, force a consistent rebuild:

.. code-block:: bash

   mamba config --env --set channel_priority strict
   mamba config --env --add channels conda-forge

   mamba update --all
   mamba install --force-reinstall \
     geopandas pyogrio fiona gdal libgdal libjxl


Then check whether the needed library exists:

.. code-block:: bash

   find "$CONDA_PREFIX" -name 'libjxl.so*'
 

You want to see something like:

.. code-block:: bash

   $CONDA_PREFIX/lib/libjxl.so.0.11


To diagnose the broken link run:

.. code-block:: bash

   python - <<'PY'
   import importlib.util
   for mod in ["pyogrio._io", "fiona.ogrext"]:
       spec = importlib.util.find_spec(mod)
       print(mod, "=>", spec.origin if spec else "not found")
   PY

Then run ``ldd`` on the printed ``.so`` files, for instance:

.. code-block:: bash

   ldd /path/to/pyogrio/_io*.so | grep -E 'jxl|not found|gdal'
   ldd /path/to/fiona/ogrext*.so | grep -E 'jxl|not found|gdal'

If you see:

.. code-block:: bash

   libjxl.so.0.11 => not found


then the binary package is linked against ``libjxl 0.11``, but your env does not contain that matching shared library. The most reliable fix is often a clean env:

.. code-block:: bash

   mamba create -n geo312 -c conda-forge --strict-channel-priority \
     python=3.12 geopandas pyogrio fiona gdal rasterio shapely pyproj rtree
   mamba activate geo312


The pin ``libjxl`` back to 0.11 and test it works:


.. code-block:: bash

   mamba install -c conda-forge --strict-channel-priority \
     "libjxl=0.11.*"
   find "$CONDA_PREFIX" -name 'libjxl.so*' -print

This should show something like ``$CONDA_PREFIX/lib/libjxl.so.0.11``

Test:

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

It will not stay that way. Any status or list reconciles a recorded process that has gone -
after a crash, a reboot, an OOM kill, or ``wsl --shutdown`` - and reports ``FAILED`` with an
explanation rather than leaving a job that can never finish:

.. code-block:: bash

   hydromate status <JOB_ID>
   hydromate list --rebuild        # also re-indexes moved or restored job folders

A job fails the moment it starts
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read ``runner.log`` first - it carries the error with a stable ``code`` and a suggested
remedy. Almost always this is the solver environment:

.. code-block:: bash

   hydromate logs <JOB_ID>
   hydromate profiles validate

Watch for the *ambient* warning. A variable that came from your interactive shell (say
``/etc/profile.d/openfoam9.sh``) is inherited by anything you launch from a terminal but
**not** by a detached job - so the same case works by hand and fails when submitted. The
fix is to make the setup script itself export it.

To see the failure under a terminal, run the job synchronously - the same code path a
detached job takes:

.. code-block:: bash

   hydromate execute <JOB_ID>

``the systemd launcher is not available``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A bare SSH session without lingering has ``systemd-run`` on ``PATH`` but no user manager at
all. Either enable lingering (``loginctl enable-linger $USER``) or use the fallback, which
needs nothing:

.. code-block:: bash

   hydromate submit ... --launcher posix

Jobs vanish after ``wsl --shutdown``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Expected, and not something hydromate can prevent: the distro's lifetime is not ours.
``wsl --shutdown``, a Windows restart, or WSL2 idling the VM out terminates everything
running inside it. Those jobs recover to ``FAILED`` rather than a stuck ``RUNNING``, and can
be resubmitted.

The QGIS plugin
---------------

``hydromate could not be found``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The message names all three places it looked - the plugin setting, ``$HYDROMATE_EXE`` and
``PATH``. Set the path in *HydroMate ▸ Settings* and press *Test*. There is deliberately no
silent fallback to another interpreter, because a wrong one fails obscurely much later.

Remember that hydromate belongs in the environment that reaches your **solver**, not in
QGIS's Python.

The panel shows only Setup and Jobs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The capability tabs are generated from the case, so there is no case selected yet, or
hydromate could not read it. Check the same thing the plugin does:

.. code-block:: bash

   hydromate case-status <config.yml> --json

A layer loaded but is not styled
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The dataset name in the result did not match what the manifest expected. The file itself is
fine - style it from Layer Properties.

.. toctree::
   :hidden:

   hbc
