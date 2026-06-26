# user-sources/ - your input data

Put **your** source data here. This folder is **gitignored** for real cases (it
holds large GeoTIFFs/GeoPackages that must stay out of version control - see the
20 MB CI guard); only this README is tracked, to document the expected layout.

All data must be in the project CRS set in `case-config.yml` (`project.crs_epsg`,
e.g. EPSG:25832 / a metric CRS). Layers in another CRS are reprojected on ingest,
but check the vertical datum of any elevation/gauge data yourself.

Expected layout (filenames are examples - point `case-config.yml` `inputs.*` at
whatever you actually use):

```
user-sources/
  geodata/
    dem-initial.tif            # [REQUIRED] baseline terrain (GeoTIFF)
    dem-target.tif             # optional 2nd DEM (morphodynamic / DoD target)
    roi.gpkg                   # [REQUIRED] ROI / max wetted-extent polygon
    liquid-boundaries.gpkg     # [REQUIRED for a build] inflow/outflow LINES,
                               #   field 'Type (inflow/outflow)', on the mesh-zone bounds
    mesh-zones.gpkg            # polygons 'Zone Name' channel/floodplain (anisotropic mesh)
    channel-centerline.gpkg    # line the channel cells are elongated along
    roughness-zones.gpkg       # polygons 'Zone ID' (friction zonation)
    roughness-table.csv        # zone_id,ks  -> per-zone roughness (calibrated by HBC)
    rating-curve.csv           # outlet Q,WSE rating curve (or generate via `hydromate rating`)
  hydraulics/
    inflow.csv                 # [REQUIRED for a build] upstream discharge value / series
  ground-truth/                # optional calibration targets (field measurements)
    hydraulics/                #   e.g. FlowTracker velocity/depth exports
    sediment/                  #   e.g. grain-size distributions
```

See `../USAGE.info` for the work steps and `../../example-Inn/` for a complete,
filled-in example.
