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



.. toctree::
   :hidden:

   hbc
