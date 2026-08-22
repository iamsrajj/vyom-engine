> **Update**: this now covers Sentinel-1 + Sentinel-2, six S2 indices (NDVI,
> NDWI, NDMI, MSAVI2, NDRE, SOC_VIS) plus two S1 indices (RVI, VV_VH_RATIO),
> pluggable storage (local disk or MinIO/S3), and a full web dashboard at
> `web/index.html` — "Vyom Engine - By AgriDoot" — for drawing farms anywhere
> in the world and viewing any index. See "Multi-index & Sentinel-1" and
> "Production storage" sections below. The instructions below still apply for
> initial setup; `deploy/README.md` has the systemd/nginx production path.

# Vyom Engine — Phase 1 Slice: Ingestion + Processing + Farm Web Mapping

This is a working, runnable slice of the full Vyom Engine architecture, scoped to what
you asked for first:

1. **Ingestion pipeline** — authenticate with Copernicus Data Space Ecosystem (CDSE),
   discover Sentinel-2 products over your farm polygons, download them, dedupe them.
2. **Processing pipeline** — cloud-mask, compute NDVI/NDWI, write Cloud-Optimized
   GeoTIFFs, and compute per-farm zonal statistics.
3. **Web mapping for farms** — a FastAPI service that lets you register farm
   boundaries, trigger processing, pull NDVI time series, and view an on-the-fly
   NDVI map tile for any farm in a browser/Leaflet map.

It follows the tile-first design principle from your doc: **a Sentinel-2 tile is
downloaded and processed exactly once**, and zonal statistics are computed for every
farm polygon intersecting that tile in a single pass — not once per farm.

This is intentionally a Phase-1 **monolith** (per your own roadmap: "Monolith-leaning
... split into microservices only once a specific component needs independent
scaling"). Everything runs as one FastAPI app + one Celery worker/beat process.

## What's NOT in this slice (by design, deferred to later phases)

- Sentinel-1/3/5P, DEM
- MinIO / cloud object storage (raw + processed files go to local disk for now —
  swapping to MinIO later is a config change in `vyom/storage.py`, not a rewrite)
- Multi-tenancy, billing, auth/API keys
- Microservices split, Kubernetes
- ML inference

## Architecture (this slice)

```
Copernicus CDSE (OAuth2 / OData / STAC)
        |
        v
auth_broker.py  -->  discovery.py  -->  catalog_products (Postgres)
                                              |
                                              v
                                     download_manager.py
                                              |
                                              v
                                     data/raw/<product>.zip
                                              |
                                              v
                              processing/pipeline.py
                    cloud_mask -> NDVI/NDWI -> COG -> zonal_stats
                                              |
                                              v
                     data/processed/*.tif        zonal_stats (Postgres)
                                              |
                                              v
                                    FastAPI: /farms, /tiles
                                              |
                                              v
                                     Browser / Leaflet map
```

## Setup

### 1. Requirements

- Python 3.11+
- PostgreSQL 14+ with PostGIS extension
- GDAL system libraries (required by `rasterio`)
- A Copernicus Data Space Ecosystem account (you have this already)

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# edit .env with your CDSE client_id/secret and DB connection string
```

To get CDSE OAuth2 client credentials (if you registered with just a username/password
instead): go to https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings
and create an OAuth client, or use the password-grant flow — `auth_broker.py` supports
both (see comments in that file).

**Download endpoint**: `CDSE_DOWNLOAD_URL` points at
`https://download.dataspace.copernicus.eu/odata/v1` (the old `zipper.dataspace...`
host used for the same job is retired). This endpoint 302-redirects to a signed
node/object-storage URL, so `download_manager.py` follows redirects manually and
re-attaches the `Authorization` header on every hop — the same thing CDSE's own
docs do with curl's `--location-trusted` flag. Plain `requests.get(..., allow_redirects=True)`
would silently drop the token on that redirect and fail.

### 3. Create the database

```bash
createdb vyom
psql vyom -c "CREATE EXTENSION postgis;"
psql vyom -f migrations/schema.sql
```

### 4. Run it

Terminal 1 — API:
```bash
uvicorn vyom.api.main:app --reload --port 8000
```

Terminal 2 — Celery worker:
```bash
celery -A vyom.celery_app worker --loglevel=info -Q download,process,stats
```

Terminal 3 — Celery beat (scheduled discovery polling):
```bash
celery -A vyom.celery_app beat --loglevel=info
```

Redis must be running locally (`redis-server`) as the Celery broker.

## Using it end-to-end

1. **Register a farm** (POST a GeoJSON polygon):
   ```bash
   curl -X POST http://localhost:8000/farms \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Ramesh Field 1",
       "user_id": "00000000-0000-0000-0000-000000000001",
       "geometry": {"type": "Polygon", "coordinates": [[[77.0,28.5],[77.01,28.5],[77.01,28.51],[77.0,28.51],[77.0,28.5]]]}
     }'
   ```

2. **Trigger ingestion + processing** for that farm (discovers + downloads + processes
   the latest available Sentinel-2 scene covering it):
   ```bash
   curl -X POST http://localhost:8000/farms/{farm_id}/refresh
   ```
   This runs asynchronously via Celery — discovery finds the product, download_manager
   fetches it, the processing pipeline computes NDVI/NDWI/COG, and zonal_stats.py
   computes the farm's mean NDVI/NDWI for that date.

3. **Get the NDVI time series** for the farm (for a chart on your web dashboard):
   ```bash
   curl http://localhost:8000/farms/{farm_id}/timeseries?metric=NDVI_mean
   ```

4. **View the map**. Open `web/map.html` in a browser (or serve it statically) — it's
   a minimal Leaflet page that loads farm boundaries from `/farms` and NDVI tiles from
   `/tiles/{farm_id}/{date}/{z}/{x}/{y}.png`.

## Files

- `vyom/config.py` — all settings, loaded from `.env`
- `vyom/db.py` — SQLAlchemy engine/session + PostGIS-aware base
- `vyom/models.py` — `catalog_products`, `polygons` (farms), `polygon_tile_map`, `zonal_stats`
- `vyom/auth_broker.py` — Copernicus OAuth2 token acquisition + caching
- `vyom/discovery.py` — STAC query against CDSE, writes to `catalog_products`
- `vyom/download_manager.py` — downloads + checksums + dedupes products
- `vyom/tile_grid.py` — MGRS tile id extraction, polygon<->tile intersection mapping
- `vyom/processing/cloud_mask.py` — SCL-based cloud mask (L2A)
- `vyom/processing/indices.py` — NDVI, NDWI formulas
- `vyom/processing/cog_writer.py` — writes internally-tiled COGs with overviews
- `vyom/processing/pipeline.py` — orchestrates the full per-tile processing loop
- `vyom/zonal_stats.py` — per-farm zonal statistics using `exactextract`
- `vyom/celery_app.py`, `vyom/tasks.py` — task queue wiring, beat schedule
- `vyom/api/main.py`, `vyom/api/farms.py`, `vyom/api/tiles.py` — the web-facing layer
- `web/map.html` — minimal Leaflet demo page
- `migrations/schema.sql` — the Postgres/PostGIS schema
- `docker-compose.dev.yml` — Postgres+PostGIS and Redis for local dev

## Multi-index & Sentinel-1

Indices are configured, not hardcoded — `S2_INDICES`/`S1_INDICES` in `.env`
control what gets computed. Adding a new index later is: implement the formula
in `vyom/processing/indices.py` (or `sar_indices.py` for S1), add its name to
the relevant `_INDEX_BAND_REQUIREMENTS` dict in the pipeline file, list it in
`.env` — no database migration, since `processed_indices` is stored as JSONB.

Currently computed:

| Index | Platform | What it's for |
|---|---|---|
| NDVI | S2 | General vegetation vigor/density |
| NDWI | S2 | Surface water / waterlogging |
| NDMI | S2 | Canopy moisture — irrigation stress, ahead of visible wilting |
| MSAVI2 | S2 | Vegetation index corrected for bare-soil brightness, useful early season |
| NDRE | S2 | Chlorophyll/nitrogen status in dense canopy where NDVI saturates |
| SOC_VIS | S2 | **Experimental** visible-band soil organic carbon proxy — see caveat in `indices.py`, not a lab-grade measurement |
| RVI | S1 | Radar vegetation index — cloud-independent, works through monsoon |
| VV_VH_RATIO | S1 | Backscatter ratio — flags flooding/harvest, cloud-independent |

**Sentinel-1 calibration caveat**: the S1 pipeline currently reads raw GRD
digital numbers rather than radiometrically calibrated backscatter. See the
docstring at the top of `vyom/processing/pipeline_s1.py` for what's needed
(applying the product's calibration LUT, ideally terrain correction) before S1
values are directly comparable across fields/time in production — right now
they're internally consistent enough to show relative change on one field over
time, but not absolute cross-field comparison.

## Production storage

`STORAGE_BACKEND` in `.env` switches between:
- `local` — files on this server's disk (what you've been running)
- `s3` — MinIO or any S3-compatible store (`vyom/storage.py`). This is what
  lets you run more than one worker machine sharing the same raw/processed
  files, and lets tile-serving read COGs via partial range-requests (GDAL's
  `/vsis3/`) without a full download. `docker-compose.dev.yml` includes a
  MinIO service for local testing — start it, set `STORAGE_BACKEND=s3` and the
  `S3_*` vars in `.env`, nothing else in the pipeline code changes.

## The web dashboard

`web/index.html` — "Vyom Engine - By AgriDoot" — is a full dashboard, not just
a demo page:
- Draw a field boundary anywhere in the world (Leaflet + Leaflet.draw)
- Search any place name to jump the map there
- Pick Sentinel-2 or Sentinel-1, then any index for that platform
- See the current value for every index at a glance, and a trend chart for
  the selected one
- Pick a specific past date, not just "latest"

Serve it with any static file server (`python3 -m http.server` for local
testing, or nginx alongside the API in production) — it talks to the API over
plain `fetch()`, no build step.

## Next slices (per your roadmap)

Once this is running against real farms, the natural next additions, in order, are:
Sentinel-1 (flood/all-weather for farms during monsoon cloud cover), MinIO for object
storage, multi-tenancy + auth, then splitting `discovery`/`download_manager`/
`tile_processor` into independently-scaled services once one of them becomes a
bottleneck — not before.
