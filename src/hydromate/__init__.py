"""hydromate — automated setup of calibration-ready TELEMAC + GAIA cases.

The package turns user-provided geodata and hydraulic data (DEM, ROI boundary,
inflow/outflow, measurements) into a production-ready TELEMAC-2D (optionally
+GAIA) case plus a HydroBayesCal ``config_Telemac.py``, so a surrogate-assisted
Bayesian calibration can be launched with ``bal_telemac.py``.

Pipeline stages (see :mod:`hydromate.pipeline`):

1. DEM ingest + clip to the region of interest        (:mod:`hydromate.dem`)
2. mesh + bathymetry + SELAFIN geometry               (:mod:`hydromate.mesh`)
3. boundary conditions ``.cli``                       (:mod:`hydromate.boundary`)
4. steering ``.cas`` + friction ``.tbl``              (:mod:`hydromate.steering`)
5. calibration CSV + HydroBayesCal config emit        (:mod:`hydromate.calibration`)

Everything is driven by a single YAML config (:mod:`hydromate.config`).
"""

__version__ = "0.1.0"

from hydromate.config import Config, load_config
from hydromate.dem import clip_dem_to_roi, clip_to_roi

__all__ = ["Config", "load_config", "clip_to_roi", "clip_dem_to_roi", "__version__"]
