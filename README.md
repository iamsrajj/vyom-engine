# Vyom Engine

Satellite farm-monitoring platform for AgriDoot. Vyom Engine watches a
farmer's fields from space: it pulls Sentinel-1 (radar) and Sentinel-2
(optical) imagery from the Copernicus Data Space Ecosystem (CDSE) for every
registered farm polygon, computes vegetation/water/soil indices, and serves
the results through a web dashboard, in-app notifications, and a monetized
partner API that lets other businesses plot the same data on their own
products.

> **If you only read one section**, read [Architecture at a glance](#architecture-at-a-glance)
> and [Repository layout](#repository-layout) -- together they explain where
> everything lives and how a request actually flows through the system.

---

## Table of contents

- [Project overview: what we are building and why](#project-overview-what-we-are-building-and-why)
  - [Index catalogue](#index-catalogue-what-each-one-is-for)
- [What this system actually does](#what-this-system-actually-does)
- [Architecture at a glance](#architecture-at-a-glance)
- [Repository layout](#repository-layout)
- [Data model, in one paragraph](#data-model-in-one-paragraph)
- [Request flows](#request-flows-how-the-code-behaves-today)
- [Database tables](#database-tables)
- [Scheduled jobs](#scheduled-jobs-celery-beat-celery_apppy)
- [The imagery pipeline, end to end](#the-imagery-pipeline-end-to-end)
- [Real vs. filled data: interpolation](#real-vs-filled-data-interpolation)
- [Auth: three separate schemes](#auth-three-separate-schemes)
- [Billing & monetization](#billing--monetization)
- [The partner API](#the-partner-api)
- [Local development setup](#local-development-setup)
- [Configuration reference](#configuration-reference)
- [Deployment](#deployment)
- [Recurring gotchas](#recurring-gotchas)
- [Where to look for X](#where-to-look-for-x)

---

## Project overview: what we are building and why

### The problem

A farmer in India usually cannot see how a whole field is doing at any one
moment. Walking it takes hours, ground sensors are too expensive to put on
every plot, and by the time stress is visible to the eye the yield loss has
often already started. Advisory, insurance, credit and input-supply
businesses have the same blind spot: they need an objective, repeatable
signal for thousands of plots they will never visit.

Free public satellites already photograph every field on Earth every few
days. What is missing is the plumbing that turns those raw scenes into
something a farmer or an agri-business can act on: "which part of this field
is stressed, since when, and is it getting better or worse?"

**Vyom Engine is that plumbing.** It is AgriDoot's Earth-observation and GIS
backend: draw your field once, and it is monitored continuously from space,
with the results delivered as maps, time series, alerts and API responses.

### What "GIS" and "satellite" mean here

- **GIS (Geographic Information System)**: every field is stored as a
  _polygon_ (`Polygon.geom`, PostGIS geometry, WGS84 / EPSG:4326). Because a
  field is a real geometry and not just a name, we can intersect it with
  satellite scenes, clip rasters to its exact boundary, compute its area in
  acres, reverse-geocode its location, and serve map tiles for it.
- **Satellites** (both free, from the EU's Copernicus programme, fetched via
  the Copernicus Data Space Ecosystem, CDSE):
  - **Sentinel-2** (optical, like a very good camera with extra colours):
    13 spectral bands; we use 10 m bands (Blue B02, Green B03, Red B04, NIR
    B08) and 20 m bands (Red-edge B05/B06/B07/B8A, SWIR B11/B12). Revisit is
    about 5 days, but **clouds block it**, which is the whole story in the
    Indian monsoon (kharif) season.
  - **Sentinel-1** (C-band radar, VV and VH polarisation): sends its own
    microwave pulse and measures what bounces back, so it **sees through
    cloud, day or night**. It cannot tell "green" from "not green", but it
    is sensitive to canopy structure, water and soil roughness.
- **Why both**: Sentinel-2 gives the rich crop-health picture on clear days;
  Sentinel-1 keeps the timeline alive when Sentinel-2 is blind. Where neither
  has a pass, the system can fill the gap and always labels it as filled
  (see [Real vs. filled data](#real-vs-filled-data-interpolation)).

### From pixels to a number a farmer can use

A satellite scene is a grid of pixels, each a set of band reflectances. An
**index** is a small formula over bands that turns those raw reflectances
into one number tied to something agronomic (greenness, canopy water,
chlorophyll, bare soil...). For each field and each pass the pipeline:

1. clips the scene to the field polygon and masks clouds/shadows (Sentinel-2
   uses the SCL scene-classification layer, classes 0, 1, 3, 8, 9, 10 are
   rejected);
2. computes every enabled index per pixel and stores it as a
   Cloud-Optimized GeoTIFF (this is what the map shows);
3. reduces each index to **zonal statistics** over the field
   (`{INDEX}_mean`, `{INDEX}_std`, pixel count, cloud %), which is what the
   time-series chart, notifications and the partner API's numeric endpoints
   use.

### Index catalogue (what each one is for)

Formulas below are the ones actually implemented in
`vyom/processing/indices.py` (Sentinel-2) and `sar_indices.py`
(Sentinel-1). Names in `code` are the exact `S2_INDICES` / `s1_indices`
config values and the prefix of the metric name (e.g. `NDVI` gives
`NDVI_mean`).

#### Crop vigour and canopy (Sentinel-2)

| Index       | Formula (S2 bands)                               | Use it for                                                                                        | Caveat                                                                     |
| ----------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `NDVI`      | (NIR-Red)/(NIR+Red), B08, B04                    | Overall greenness, vigour, stand density; healthy crop is roughly 0.6 to 0.9. The headline index. | Saturates in dense canopy.                                                 |
| `EVI`       | 2.5(NIR-Red)/(NIR+6Red-7.5Blue+1), B08, B04, B02 | Dense canopy where NDVI flattens; corrects atmosphere and background.                             | Needs the Blue band.                                                       |
| `EVI2`      | 2.5(NIR-Red)/(NIR+2.4Red+1), B08, B04            | EVI-like response without Blue, so less haze noise.                                               |                                                                            |
| `NIRV`      | NIR x NDVI, B08, B04                             | Photosynthetic-capacity / productivity proxy.                                                     |                                                                            |
| `MSAVI2`    | (2NIR+1-sqrt((2NIR+1)^2-8(NIR-Red)))/2           | Early season, sparse canopy: suppresses bare-soil brightness with no tuning.                      |                                                                            |
| `OSAVI`     | 1.16(NIR-Red)/(NIR+Red+0.16)                     | Sparse canopy on bright soils.                                                                    |                                                                            |
| `SAVI`      | 1.5(NIR-Red)/(NIR+Red+0.5)                       | Soil-adjusted vigour (L = 0.5).                                                                   | L is a fixed guess.                                                        |
| `VARI`      | (Green-Red)/(Green+Red-Blue), B03, B04, B02      | Visible-only greenness when NIR is unreliable.                                                    | Sensitive to haze; prefer NDVI.                                            |
| `LAI_PROXY` | 3.618 x EVI - 0.118                              | Relative leaf-area / canopy-density trend.                                                        | **Experimental.** Not a true LAI in m2/m2; not calibrated to Indian crops. |

#### Chlorophyll, nitrogen and stress (Sentinel-2 red-edge)

| Index           | Formula                                                   | Use it for                                                                    | Caveat           |
| --------------- | --------------------------------------------------------- | ----------------------------------------------------------------------------- | ---------------- |
| `NDRE`          | (NIR-RedEdge)/(NIR+RedEdge), B08, B05                     | Chlorophyll / nitrogen status in mid-to-late season, when NDVI has saturated. |                  |
| `NDREX`         | (B8A-B06)/(B8A+B06)                                       | NDRE variant probing slightly deeper into the canopy.                         |                  |
| `NDRE_B7`       | (B8A-B07)/(B8A+B07)                                       | Dense-canopy discrimination.                                                  |                  |
| `CAR_RE` (CARI) | RedEdge/Red x sqrt((aRed+Red+b)^2/(a^2+1)), B03, B04, B05 | Chlorophyll absorption with a baseline correction.                            |                  |
| `ARI1`          | 1/Green - 1/RedEdge, B03, B05                             | Anthocyanin (stress / senescence pigment) rather than chlorophyll.            | Unbounded range. |

#### Water and moisture (Sentinel-2)

| Index                 | Formula                                                    | Use it for                                                             | Caveat                            |
| --------------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------- | --------------------------------- |
| `NDMI`                | (NIR-SWIR1)/(NIR+SWIR1), B08, B11                          | Water held in leaf tissue: irrigation stress _before_ visible wilting. |                                   |
| `MSI`                 | SWIR1/NIR                                                  | Simple moisture-stress ratio (higher = drier).                         | Unstable near shadow/water edges. |
| `NDWI`                | (Green-NIR)/(Green+NIR), B03, B08                          | Surface water / waterlogging / flooded paddy.                          |                                   |
| `MNDWI`               | (Green-SWIR1)/(Green+SWIR1), B03, B11                      | Open water with fewer built-up false positives.                        | Wet soil, shadows.                |
| `AWEI_SH`, `AWEI_NSH` | multi-band water extraction (shadow / non-shadow variants) | Ponds, tanks, flood extent.                                            | Needs good cloud mask.            |
| `WI2015`              | multi-band water regression                                | Water in complex scenes.                                               | Needs true 0..1 reflectance.      |
| `GREEN_BLUE_RATIO`    | Green/Blue                                                 | Qualitative turbidity of farm ponds.                                   | Weak over farmland.               |

#### Soil, land cover and events (Sentinel-2)

| Index                     | Formula                                                               | Use it for                                                                      | Caveat                                                                        |
| ------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `SOC_VIS`                 | 1 - Red/(Blue+Green+Red)                                              | Relative soil darkness, i.e. a hint of within-field organic-matter variability. | **Experimental.** Not a calibrated SOC %, confounded by moisture and texture. |
| `BSI`                     | ((SWIR1+Red)-(NIR+Blue))/((SWIR1+Red)+(NIR+Blue))                     | Fallow / uncultivated patches, bare soil.                                       | Cloud shadow can fake "soil".                                                 |
| `NDBI`, `IBI`             | SWIR1/NIR contrast; NDBI-NDVI combination                             | Built-up structures (sheds, paths, encroachment) inside a polygon.              | Also responds to dry bare soil.                                               |
| `NBR`, `NBR2`, `BAI`      | (NIR-SWIR2)/(NIR+SWIR2); (SWIR1-SWIR2)/(SWIR1+SWIR2); burn-area index | Post-harvest **residue (stubble) burning** detection; compare before/after.     | Best as a two-date change, not one date.                                      |
| `NDSI`, `SNOW_BRIGHTNESS` | (Green-SWIR1)/(Green+SWIR1) (identical to MNDWI); (Green+Blue)/2      | Snow cues; only relevant for hill-state farming.                                |                                                                               |

#### Radar, cloud-independent (Sentinel-1)

| Index         | Formula      | Use it for                                                              | Caveat                                           |
| ------------- | ------------ | ----------------------------------------------------------------------- | ------------------------------------------------ |
| `RVI`         | 4 VH/(VV+VH) | Cloud-proof stand-in for NDVI-style canopy density through the monsoon. | Needs calibrated, terrain-corrected backscatter. |
| `VV_VH_RATIO` | VV/VH        | Canopy development trend; sharp change can flag flooding or harvest.    | Coarser than RVI; no discrete legend yet.        |

**Which are on by default?** `Settings.s2_indices` in `vyom/config.py`
defaults to `NDVI, NDRE, NDWI, NDMI, EVI, MSAVI2, LAI_PROXY, ARI1, CAR_RE,
NDREX`; a deployment overrides it with `S2_INDICES` in `.env` (the shipped
`.env.example` enables the full set above). Only an index that is both listed
in `S2_INDICES` **and** implemented in `indices.py` is computed.
`NDVI, NDWI, NDMI, NDRE, MSAVI2, SOC_VIS, RVI` also have discrete, labelled
Low/Med/High colour legends (`processing/index_scale.py`); the rest use a
continuous colormap.

**Deliberately not implemented** (so nobody assumes they exist): `CCC`
(canopy chlorophyll content, needs a PROSAIL-style model inversion), `RSM`
(Sentinel-1 soil moisture, needs change detection against a per-pixel
dry/wet baseline) and `SOC_SWIR`. We prefer no number to a made-up number
shown to a farmer.

### A rule of thumb through the crop calendar

| Stage                      | Lead with                | Why                                            |
| -------------------------- | ------------------------ | ---------------------------------------------- |
| Sowing to early growth     | `MSAVI2`, `OSAVI`, `BSI` | Canopy is sparse, soil dominates the pixel.    |
| Vegetative peak            | `NDVI`, `EVI`            | Vigour and uniformity across the field.        |
| Late season / dense canopy | `NDRE`, `NDREX`          | NDVI saturates, red-edge keeps discriminating. |
| Any time, water worries    | `NDMI`, `MSI`, `NDWI`    | Drought stress vs waterlogging.                |
| Monsoon / cloudy weeks     | `RVI`, `VV_VH_RATIO`     | Radar is unaffected by cloud.                  |
| Post-harvest               | `NBR`, `BSI`             | Residue burning, fallow detection.             |

### Who it serves

- **Farmers** (web dashboard, `web/index.html`): draw a field, pick crop/soil
  and sowing date, pay per acre for a 3/6/12-month plan, then see maps,
  trends, notifications and advisory for it.
- **Businesses** (partner API, `/api/v1`): agri-input, insurance, lending and
  advisory companies register their own customers' fields via API key/secret,
  pull index readings and dates, and plot the same layers on their own maps.
- **Operators** (`web/admin/*`): error log, region pre-warming of imagery,
  coupon management.

---

## What this system actually does

A farmer (or, via the partner API, another business on the farmer's behalf)
draws a polygon around a field. From that point on, Vyom Engine:

1. **Discovers** every Sentinel-1/Sentinel-2 scene from CDSE that covers that
   polygon, going back as far as a year on first creation.
2. **Downloads** the raw scene, then **processes** it: cloud-masks Sentinel-2,
   reprojects Sentinel-1's ground-control-point radar geometry onto a clean
   grid, and computes whichever indices are enabled (NDVI, NDRE, NDWI, NDMI,
   MSAVI2, SOC_VIS, and ~25 more for S2; RVI and VV_VH_RATIO for S1 -- see
   `vyom/processing/indices.py` and `sar_indices.py` for the full, current
   list, since `S2_INDICES` is configurable per deployment).
3. **Stores** a Cloud-Optimized GeoTIFF (COG) per scene/index in S3-compatible
   object storage (Wasabi in production), and a per-farm scalar reading
   (`ZonalStat`) for each index/date.
4. **Serves** it back three ways: map tiles cropped to the farm's exact
   polygon (not the shared processing bounding box -- see
   [Recurring gotchas](#recurring-gotchas)), a time series per index, and
   (new) the same tiles to partner-API integrators via a signed, header-free
   token URL.
5. **Bills** for it: farms are metered per acre on a fixed-term plan
   (3/6/12 months), paid via wallet balance and/or Razorpay, with GST
   invoicing; separately, businesses pay a maintenance fee + per-request
   metering to use the partner API to manage farms on their own customers'
   behalf.

If a farm has no real satellite pass on the exact date you ask for, the
system can optionally fill the gap -- see
[Real vs. filled data](#real-vs-filled-data-interpolation). **Every** reading
this system returns, in the dashboard or the partner API, is labelled with
where it actually came from. Never assume a successful response is a real
satellite reading without checking that label.

---

## Architecture at a glance

```
                                   +--------------------------+
                                   |   Copernicus Data Space  |
                                   |   Ecosystem (CDSE)       |
                                   +------------+-------------+
                                                | OData / OAuth2
                              discovery.py, download_manager.py,
                              cdse_rate_limiter.py (Redis-leased
                              concurrency + fairness + retry)
                                                |
                     +------------------------- v -------------------------+
                     |                  Celery workers                     |
                     |  queues: download/discover  |  process/stats        |
                     |  tasks.py, billing_tasks.py, celery_app.py (beat)   |
                     +-----------+---------------------------+-------------+
                                 | raw .SAFE.zip               | COGs + stats
                     +-----------v------------+     +----------v-------------+
                     |  Object storage         |     |  PostgreSQL + PostGIS  |
                     |  (Wasabi S3, or local   |     |  models.py (22 tables) |
                     |  disk -- storage.py)    |     |                        |
                     +-------------------------+     +-----------+------------+
                                                                  |
                                            +---------------------v---------------------+
                                            |           FastAPI app (vyom/api/)          |
                                            |  session-cookie routes | partner-API       |
                                            |  (farms, tiles, auth,  | routes (API key/  |
                                            |  billing, notifications| secret headers)   |
                                            +--------+-------------------------+---------+
                                                     |                         |
                                    +-----------------v--------+   +-----------v-------------+
                                    |  web/index.html           |   | Partner's own product   |
                                    |  (farmer-facing dashboard |   | (via web/developers/    |
                                    |  -- single-file SPA)      |   | playground.html to test)|
                                    +---------------------------+   +-------------------------+
```

**Backend**: a FastAPI monolith (`vyom/`) plus Celery workers for anything
slow (CDSE discovery/download, raster processing, zonal stats, billing
sweeps, email/notification fan-out). PostgreSQL with PostGIS handles both
relational data (users, farms, invoices) and geometry (`Polygon.geom`).

**Frontend**: no build step, no framework. `web/index.html` is a single
~11,400-line file (vanilla JS, Google Maps JavaScript API, Chart.js) that IS
the entire farmer-facing dashboard. `web/admin/*.html` and
`web/developers/playground.html` are separate single-file pages for
internal/admin and partner-developer use respectively.

**Two live "planes" through the same codebase**: the dashboard plane (a
farmer's own session, cookie/JWT auth, one polygon at a time) and the
partner-API plane (a business's API key/secret, potentially thousands of
polygons, rate-limited and metered). They share the same underlying farm
records, processing pipeline, and index math -- see `vyom/api/partner_farms.py`
and `partner_tiles.py`, which deliberately reuse the dashboard's own
query/rendering functions (`render_index_tile`, the available-dates source
logic) rather than re-implementing them, specifically so the two surfaces
can't drift apart.

---

## Repository layout

```
vyom/                        Python package -- all backend logic
|-- api/                     FastAPI routers (one file per feature area)
|   |-- main.py              App factory; every router gets mounted here
|   |-- auth.py               Dashboard login: Google Sign-In + phone-OTP
|   |-- farms.py               Farm CRUD, timeseries, available-dates, status
|   |-- tiles.py               Dashboard map tile PNGs (session-cookie auth)
|   |-- reference.py            Crop/soil reference data (proxied from NovosEdge)
|   |-- notifications.py         In-app + email notifications
|   |-- contact.py                Public "Contact us" form
|   |-- errors.py                  Admin error-log panel API
|   |-- prewarm.py                  Admin: pre-fetch coverage for a region
|   |-- billing.py                   Wallet, farm plans, Razorpay, invoices
|   |-- business_onboarding.py        Business account signup + GST verification
|   |-- business_api_credentials.py    Issue/rotate/revoke partner API keys
|   |-- partner_farms.py                Partner API: farm CRUD, indices, dates
|   |-- partner_tiles.py                 Partner API: map-layer + tile PNGs
|   `-- developer_docs.py                 Serves the filtered "partner-api"
|                                         OpenAPI schema + Swagger UI
|-- processing/               Pure(ish) raster/index math, no DB/network I/O
|   |-- pipeline.py, pipeline_s1.py, pipeline_s2.py   Per-scene processing
|   |-- cloud_mask.py, cog_writer.py                  Supporting steps
|   |-- indices.py, sar_indices.py                    Index formulas (S2, S1)
|   `-- index_scale.py                                Discrete color legends
|-- models.py                 All SQLAlchemy models (22 tables)
|-- config.py                 Pydantic Settings -- every env var, in one place
|-- auth.py / api_auth.py     Dashboard session auth / partner API-key auth
|-- discovery.py              CDSE product search
|-- download_manager.py       CDSE product download (+ redirect auth fix)
|-- cdse_rate_limiter.py      Redis-leased concurrency + retry around CDSE
|-- zonal_stats.py            Per-farm scalar stats from a processed COG
|-- interpolation.py          Scalar-timeseries gap-fill
|-- raster_interpolation.py   Pixel-level gap-fill (interpolated tiles)
|-- reuse_check.py            Backfill new farms from existing coverage
|-- tile_grid.py              Shared bounding-box grouping for farms
|-- geometry_utils.py         Polygon sanitization (see gotchas below)
|-- wallet.py, farm_pricing.py, coupons.py,
|   invoicing.py, invoice_pdf.py, gst.py,
|   gst_verification.py       Billing internals
|-- razorpay_client.py        Razorpay order/signature verification
|-- otp_client.py, google_auth.py       Auth provider clients
|-- email_utils.py, notifications.py    Email templates + notification rules
|-- error_log.py              Central log_error() used everywhere
|-- prewarm.py                Admin region pre-fetch logic
|-- idempotency.py            Idempotency-Key handling (partner API)
|-- celery_app.py             Celery app + beat schedule
`-- tasks.py, billing_tasks.py   Celery task definitions

scripts/
`-- download_assets.py         Fetches logo + store badges into web/assets/img/

web/
|-- assets/img/                Local logo + Google Play / App Store badges
|-- index.html                 The farmer dashboard (single file, see below)
|-- config.js / config.js.example   Client-side config (API keys, base URLs)
|-- admin/                      Internal tools -- errors.html, prewarm.html,
|                                coupons.html
|-- developers/playground.html   Interactive partner-API tester
`-- legal/                       ToS, privacy, refund, billing-terms pages

migrations/                  Hand-written, sequentially-numbered SQL migrations
                              (schema.sql is the original base; run in order)
test/                        Standalone scripts (not a pytest suite) --
                              test_wasabi.py, check_cdse_auth.py
docs/                        Screenshots + a Word doc from earlier planning;
                              historical, not guaranteed current
docker-compose.dev.yml       Postgres + Redis for local development only
.env.example                 Every environment variable this app reads
requirements.txt             Pinned Python dependencies
```

### `web/index.html`'s internal structure

Because it's one file, here's how to navigate it:

- **CSS** (top of file): design tokens as CSS custom properties (`--canopy`
  for the AgriDoot green, `--bg-panel`, `--line`, etc.) so light/dark theme
  is just re-pointing the variables.
- **HTML**: a left nav + page shell (`#page-overview`, `#page-myfields`,
  `#page-imagery`, `#page-comparisons`, `#page-settings`), plus a set of
  `.modal-overlay` divs for everything modal (draw-new-field, item pickers,
  help/contact, notifications). The "Draw new field" modal
  (`#draw-field-modal`) is a 4-step wizard (Draw -> Field details -> Payment
  -> Done) -- see its own comment block at the top of that div for how the
  steps are wired.
- **JavaScript** (bottom of file): no modules, everything is a top-level
  `function` or `let`. Search for a DOM id or a function name directly --
  there's no other indirection to trace through.

---

## Data model, in one paragraph

`User` (dashboard account: Google/phone auth, `role` user/admin, wallet
balance) owns `Polygon` rows (a "farm" -- geometry, crop/soil/sowing-date
metadata, plan status). Each polygon accumulates `Product` rows (one per
discovered CDSE scene covering it, tracked through
discovered -> downloading -> downloaded -> processing -> processed/failed),
`ZonalStat` rows (one per product x index x farm -- the real scalar
readings), and optionally `InterpolatedStat`/`InterpolatedTile` rows (see
next section). Billing lives in `FarmPlan`, `WalletTransaction`,
`Coupon`/`CouponRedemption`, and `BusinessApiInvoice`. The partner-API side
adds `BusinessAccount`, `ApiCredential` (hashed secret, rate limits),
`ApiAuditLog`, and `BusinessSubscription`. Full definitions with comments
are in `vyom/models.py`; the migrations directory shows the order these
were introduced in, which is often useful context for _why_ a column exists
the way it does.

---

## Request flows (how the code behaves today)

### 1. A farmer adds a field (dashboard)

The "Draw new field" modal in `web/index.html` is a 4-step wizard driven by
`wizardGoToStep()`:

```
Step 1 Draw      map + search, Start drawing -> Finish shape -> area preview
                 (Next stays disabled until a shape exists)
Step 2 Details   name, auto reverse-geocoded location, crop/soil pickers
                 (proxied via /reference/*), sowing date (+ live crop age)
Step 3 Payment   3/6/12-month plan, coupon, GST breakdown -> "Save & pay"
Step 4 Done      summary; "View field" or "Draw another field"
```

Behind "Save & pay" (`saveDraft()`):

```
browser                         FastAPI                       Celery / DB
   | POST /farms (geometry, ...)   |                               |
   |------------------------------>| geometry_utils.sanitize       |
   |                               | INSERT polygons (draft)       |
   |                               | reuse_check: backfill from    |
   |                               |   already-processed coverage  |
   | POST /billing/... (plan)      |                               |
   |------------------------------>| wallet -> Razorpay order      |
   |<- Razorpay checkout           |                               |
   | payment success + webhook     | activate FarmPlan             |
   |                               | dispatch refresh_farm         |
   |                               |  (priority queues, 365 d,     |
   |                               |   85% cloud) ---------------->| discover -> download ->
   |                               |                               | process -> stats ->
   | poll GET /farms/{id}/status   |                               | fill_gaps_callback ->
   |<- ready + progress            |                               | notifications
```

### 2. Satellite data pipeline (Celery)

```
poll_all_farms (beat, every 6 h) / refresh_farm (on demand)
  -> vyom.discovery.*     CDSE OData search per farm bbox
  -> vyom.download.*      download_product_task   (raw .SAFE.zip -> storage)
  -> vyom.process.*       process_product_task    (S2 or S1 pipeline -> COGs)
  -> vyom.stats.*         compute_stats_task      (exactextract -> zonal_stats)
  -> vyom.stats.*         fill_gaps_callback      (interpolation + notifications)
Every stage has a "<queue>_priority" twin so a farmer waiting on a new field is
never stuck behind background sweeps (needs a dedicated worker; see setup).
```

### 3. A partner plots a farm on their own map

```
partner server                     Vyom API                       partner's browser map
  | GET /api/v1/farms/{id}/map-layer  |                                     |
  |   X-Api-Key / X-Api-Secret ------>| ownership check                     |
  |                                   | issue_map_tile_token(farm, cred)    |
  |<-- tile_url_template ?token=..., bounds, center, indices, legend        |
  |----------------------------------------------- template ---------------->|
  |                                   |<-- GET .../map/S2/NDVI/latest/z/x/y.png?token=
  |                                   | verify token (scoped to ONE farm)   |
  |                                   | render_index_tile() -> PNG          |
  |                                   |--- PNG + X-Vyom-Data-Source ------->|
```

The token is a 24 h JWT (`typ=partner_map_tile`) carrying the farm id and
credential id as **strings** (they are UUIDs; PyJWT cannot serialise raw UUID
objects, which is exactly the bug that `map-layer` originally hit).

## Database tables

`vyom/models.py` (22 tables), grouped by role:

| Group             | Tables                                                                                                                                                                     |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identity          | `users`, `otp_verifications`, `business_email_otps`                                                                                                                        |
| Farms and imagery | `polygons`, `catalog_products`, `polygon_tile_map`, `zonal_stats`, `interpolated_stats`, `interpolated_tiles`                                                              |
| Farm billing      | `farm_plans`, `wallet_transactions`, `coupons`, `coupon_redemptions`                                                                                                       |
| Partner API       | `api_credentials`, `api_idempotency_keys`, `api_access_log`, `business_subscriptions`, `business_api_invoices`, `business_api_invoice_farms`, `business_renewal_reminders` |
| Operations        | `notifications`, `error_logs`                                                                                                                                              |

## Scheduled jobs (Celery beat, `celery_app.py`)

| Task                                                    | Every  | Purpose                                                            |
| ------------------------------------------------------- | ------ | ------------------------------------------------------------------ |
| `vyom.discovery.poll_all_farms`                         | 6 h    | Incremental 30-day refresh of every farm; stale-data notifications |
| `vyom.billing.expire_farm_plans`                        | 24 h   | End plans past their term                                          |
| `vyom.billing.reconcile_abandoned_farm_plans`           | 24 h   | Clean up farms whose payment never completed                       |
| `vyom.billing.generate_monthly_business_api_invoices`   | 24 h   | Idempotent per month (unique user + month)                         |
| `vyom.billing.suspend_overdue_business_invoices`        | 24 h   | Enforce unpaid partner invoices                                    |
| `vyom.billing.send_business_renewal_reminders`          | 24 h   | Renewal emails                                                     |
| `vyom.billing.cleanup_idempotency_keys`                 | 24 h   | Expire old `Idempotency-Key` rows                                  |
| `vyom.billing.reconcile_pending_business_subscriptions` | 30 min | Settle Razorpay state                                              |
| `vyom.billing.reconcile_pending_business_invoices`      | 30 min | Settle Razorpay state                                              |

---

## The imagery pipeline, end to end

1. **`discovery.py`** searches CDSE's OData catalogue for products
   intersecting a farm's (buffered) bounding box, filtered by collection
   (`SENTINEL-2`/`SENTINEL-1`), product type, cloud cover, and a
   configurable lookback window. `poll_all_farms` (Celery beat, every 6h)
   sweeps every farm with a 30-day lookback; a brand-new farm additionally
   gets a one-time 365-day/85%-cloud-cover backfill request on creation.
2. **`tile_grid.py`** groups farms that share the same processing area so
   one downloaded scene can serve several nearby farms without re-fetching.
3. **`download_manager.py`** downloads the product via CDSE's
   `/odata/v1/Products($ID)/$value` endpoint, manually re-attaching the
   Authorization bearer token across the redirect that endpoint issues
   (the `requests` library strips auth headers on cross-host redirects by
   default -- CDSE's own docs handle this with curl's
   `--location-trusted`). `cdse_rate_limiter.py` wraps every CDSE call with
   a Redis-leased concurrency limit, a fairness queue across farms, retry
   with backoff on 429/5xx, AND on network-level timeouts (an easy-to-miss
   gap: a `Timeout`/`ConnectionError` never produces an HTTP response
   object, so it needs its own except-block, not just a status-code check).
4. **`processing/pipeline_s2.py`** cloud-masks the scene and computes every
   enabled S2 index (`processing/indices.py`) as a Cloud-Optimized GeoTIFF.
   **`processing/pipeline_s1.py`** does the Sentinel-1-specific work: S1 GRD
   has no real map projection, only ground-control-point (GCP) tie-points,
   so the band must be reprojected onto a clean axis-aligned EPSG:4326 grid
   via `rasterio.warp.reproject(..., gcps=...)` -- restricted to the farm's
   bounds so it stays cheap -- before any of the downstream raster tooling
   (which assumes a real, north-up projection) can touch it.
5. **`zonal_stats.py`** extracts one scalar value per farm/index/date from
   the COG via `exactextract` (farms are passed as GeoJSON Feature dicts,
   not raw Shapely geometries -- that's the one input shape it accepts).
   NaN results (e.g. a fully cloud-masked farm window) are sanitized to
   `None` before they ever reach the database or a JSON response.
6. **`api/tiles.py`**'s `render_index_tile()` (shared by both the dashboard
   and partner-API tile routes) reads the COG, masks out anything outside
   the farm's actual polygon (a product's COG covers the shared bounding
   box of every farm on that tile, not just one), and renders either a
   discrete labelled-band PNG (via `processing/index_scale.py`, for indices
   with a defined scale) or a continuous colormap (for anything without
   one, currently just `VV_VH_RATIO`).

---

## Real vs. filled data: interpolation

Satellite revisit isn't daily -- Sentinel-2 is ~5 days _if_ cloud cover
allows a usable pass at all, Sentinel-1 depends on orbit geometry. Rather
than showing a hole in the timeline for every day without a real pass,
`interpolation.py` (scalar) and `raster_interpolation.py` (per-pixel/raster)
can fill it, on a fixed cadence, at two confidence levels:

- **`interpolated`**: a real reading exists on _both_ sides of this date --
  linear interpolation between them.
- **`provisional`**: only _one_ real reading exists so far (the most
  recent) -- a flat carry-forward, pending a second real reading to
  confirm or replace it. Weaker than `interpolated` since there's nothing
  on the far side to draw a line to yet.
- **`satellite`**: an actual reading, not filled at all.

**Every** endpoint that can return a filled value says which kind it
actually served: the dashboard/partner `available-dates` endpoints include
a `source` field per date, and every tile response (dashboard or partner)
carries an `X-Vyom-Data-Source` response header. Filled values are opt-in
(`include_interpolated=true` on the scalar endpoints) except the partner
map-tile endpoint, which defaults to including them -- a map widget with
large blank gaps between real passes is a much worse experience than the
dashboard's date-picker, where a farmer can just see there's no reading
for that day.

---

## Auth: three separate schemes

This codebase has three genuinely different auth mechanisms, used in
different places on purpose -- conflating them is the single most common
source of confusion when reading unfamiliar parts of the code:

| Scheme                                        | Used for                                             | Implemented in                                                        | Notes                                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------------------------------------- | ---------------------------------------------------- | --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Session cookie / JWT                          | Dashboard pages, most `/farms/*`, `/notifications/*` | `vyom/auth.py`, `require_auth`                                        | Google Sign-In or phone-OTP; `require_auth_query` is a `?token=`-based variant used **only** by `/tiles/*` because map tiles are loaded as plain image requests with no custom headers available.                                                                                                                                                                                                              |
| API key + secret (headers)                    | Every `/api/v1/*` partner route except tile PNGs     | `vyom/api_auth.py`, `require_business_api_auth`                       | `X-Api-Key` / `X-Api-Secret` headers; rate-limited, audit-logged, tied to a `BusinessAccount`.                                                                                                                                                                                                                                                                                                                 |
| Signed short-lived map-tile token (`?token=`) | Partner map tile PNGs only                           | `vyom/api_auth.py`, `issue_map_tile_token` / `require_map_tile_token` | Minted by `GET /api/v1/farms/{id}/map-layer` (itself key/secret-authed), scoped to exactly one `farm_id` + credential, expires in 24h. Exists for the same reason as `require_auth_query` above: a map library's tile requests can't carry custom headers, so the credential has to live in the URL, and it's deliberately narrow-scoped and short-lived so a leaked tile URL can't be used for anything else. |

If you're adding a new endpoint, ask "who's calling this, and can they set
headers?" before picking one of the three.

---

## Billing & monetization

Two separate billing surfaces exist:

1. **Farm plans** (`farm_pricing.py`, `billing.py`, `wallet.py`,
   `coupons.py`): a farmer pays per acre for a 3/6/12-month plan on a farm,
   from wallet balance first, then Razorpay for any remainder. GST is
   applied via `gst.py`. `billing_tasks.py` (Celery beat, daily) expires
   plans past their term and reconciles farms whose payment never
   completed.
2. **Business/partner API accounts** (`business_onboarding.py`,
   `business_api_credentials.py`, `invoicing.py`, `invoice_pdf.py`,
   `gst_verification.py`): a business signs up (GST verified against the
   GSTINAPI service), gets a maintenance-fee subscription
   (`BusinessSubscription`), and issues one or more API credentials
   (`ApiCredential`) scoped to their account. Usage is metered and shows up
   on their monthly `BusinessApiInvoice`, rendered as a PDF and emailed via
   `email_utils.py`.

Both surfaces reuse the same `Coupon`/GST/invoice machinery rather than two
separate implementations -- check `billing.py` before assuming a
partner-side billing question needs new code.

---

## The partner API

Base path: `/api/v1`. Full interactive docs: `GET /developers/docs` (Swagger
UI, filtered to just the `partner-api`-tagged routes via
`vyom/openapi_partner.py`) and a hands-on tester at
`web/developers/playground.html`.

Every JSON response shares one envelope:

```json
{ "data": { "...": "..." }, "meta": { "request_id": "...", "warnings": [] } }
```

or, on failure:

```json
{ "error": { "code": "FARM_NOT_FOUND", "message": "...", "retriable": false } }
```

Key routes (see `vyom/api/partner_farms.py` and `partner_tiles.py` for full
docstrings on each):

- `POST/GET/PATCH/DELETE /api/v1/farms` -- farm CRUD, scoped to farms
  created via this credential (`created_via='api'`).
- `GET /api/v1/farms/{id}/indices` -- latest (or a specific date's) index
  readings.
- `GET /api/v1/farms/{id}/timeseries` -- full history for one metric.
- `GET /api/v1/farms/{id}/available-dates?metric=...&include_interpolated=...`
  -- which dates have data, each tagged with its `source`
  (`satellite`/`interpolated`/`provisional`) -- see
  [Real vs. filled data](#real-vs-filled-data-interpolation).
- `GET /api/v1/farms/{id}/map-layer` -- **plot a farm's indices on your own
  map.** Returns an XYZ tile URL template with a signed token already
  embedded, the farm's bounds/center, which indices exist per platform, and
  a legend (colors + labelled bands) matching exactly what the dashboard
  itself renders. Feed the template straight into Leaflet / Mapbox GL /
  Google Maps' `ImageMapType`, substituting `{platform}`, `{index}`,
  `{date}`, `{z}`, `{x}`, `{y}` yourself.
- `GET /api/v1/farms/{id}/map/{platform}/{index}/{date}/{z}/{x}/{y}.png` --
  the actual tile pixels behind that template. Auth is the embedded
  `?token=`, **not** `X-Api-Key`/`X-Api-Secret` -- see
  [Auth](#auth-three-separate-schemes). Always includes
  `X-Vyom-Data-Source`.

Idempotency: mutating routes accept an `Idempotency-Key` header
(`vyom/idempotency.py`) so a retried request after a network blip doesn't
create a duplicate farm/charge.

---

## Local development setup

**Prerequisites**: Python 3.11+, PostgreSQL with PostGIS, Redis, a CDSE
account (free, at dataspace.copernicus.eu), and either local disk or a
Wasabi/S3-compatible bucket pair for storage.

```bash
git clone <this repo>
cd Vyom-Engine
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Postgres + Redis for local dev:
docker compose -f docker-compose.dev.yml up -d

cp .env.example .env
# Fill in at minimum: CDSE_USERNAME/PASSWORD, DATABASE_URL, REDIS_URL,
# AUTH_SECRET_KEY (openssl rand -hex 32), and either
# STORAGE_BACKEND=local (uses RAW_DATA_DIR/PROCESSED_DATA_DIR, no S3 needed)
# or the S3_* Wasabi credentials for STORAGE_BACKEND=s3.

# Apply migrations in order (schema.sql first, then 002 onward):
psql "$DATABASE_URL" -f migrations/schema.sql
for f in migrations/0*.sql; do psql "$DATABASE_URL" -f "$f"; done

# Local assets (logo + app-store badges) are served from web/assets/img/:
python scripts/download_assets.py

# Run everything in separate terminals (not via systemd -- that's
# deploy-only, see below):
uvicorn vyom.api.main:app --reload --port 8000
celery -A vyom.celery_app worker -Q download,discover,billing --loglevel=info
celery -A vyom.celery_app worker -Q process,stats --loglevel=info
# Dedicated worker for farmer-waiting work; without it the *_priority
# queues are never consumed and new farms will sit idle:
celery -A vyom.celery_app worker \
  -Q download_priority,discover_priority,process_priority,stats_priority \
  --loglevel=info
celery -A vyom.celery_app beat --loglevel=info

# Frontend needs no build step -- just serve web/ statically, or point
# your dev server's API base at http://localhost:8000 in web/config.js
# (copy from web/config.js.example first).
```

Split Celery across separate worker processes -- `download,discover,billing`,
`process,stats`, and a dedicated `*_priority` worker -- rather than one worker
for everything. A single shared
queue has caused OOM kills in the past when a heavy raster-processing task
and several concurrent downloads land on the same worker at once.

`test/` is not a pytest suite -- `test_wasabi.py` and `check_cdse_auth.py`
are standalone scripts for manually verifying storage/CDSE credentials are
working, run directly with `python test/test_wasabi.py`.

---

## Configuration reference

Every setting is a field on `Settings` in `vyom/config.py` (pydantic-settings,
reads from `.env`); `.env.example` documents each with a comment. The
categories, roughly:

- **CDSE**: credentials, OAuth/OData/download URLs, concurrency/rate limits.
- **Database / Redis**: `DATABASE_URL`, `REDIS_URL`.
- **Storage**: `STORAGE_BACKEND` (`local` or `s3`), local paths, or the
  full Wasabi S3 credential set.
- **Discovery defaults**: default cloud-cover threshold, S1/S2 collection
  and product-type identifiers, `S2_INDICES` (JSON list -- add a new index
  here _and_ implement its formula in `processing/indices.py` before it'll
  actually compute anything).
- **Auth**: `AUTH_SECRET_KEY` (JWT signing -- treat as a real secret),
  session TTL, `GOOGLE_CLIENT_ID`, AgriDoot's own OTP API credentials.
- **Notifications**: admin alert email/throttle, stale-data threshold,
  `DASHBOARD_BASE_URL` (also used to build partner map-tile URLs).
- **Billing**: Razorpay keys, GST percent/number, maintenance fee,
  subscription length, GSTINAPI key for business verification.
- **Legal**: platform legal name/address/invoice email, used on generated
  invoices.
- **CORS**: `CORS_ALLOWED_ORIGINS` -- a comma-separated allowlist; do not
  wildcard this in production (see [Recurring gotchas](#recurring-gotchas)).

---

## Deployment

Production currently runs manually (not via the `systemd`/`nginx` units
that exist in the repo history) on a Hostinger KVM instance with Wasabi
object storage. Whichever way you deploy:

- **nginx routing**: every new top-level API prefix (`/auth`, `/farms`,
  `/tiles`, `/errors` -> `/api/errors`, `/admin`, `/health`, `/reference`,
  `/support`, `/notifications`, and now `/api/v1`) needs to be added to
  nginx's `location ~ ^/(...)(/|$)` regex, or it 404s to the SPA's
  `index.html` fallback instead of reaching FastAPI at all. This has bitten
  every new router added so far -- if a brand-new endpoint returns HTML
  instead of JSON in production, check this first.
- **Deploying `web/index.html` changes**: always fully overwrite the file
  (e.g. `scp` from a clean local copy) rather than manually editing it on
  the server. Partial copy-paste edits on a live server have caused
  syntax errors that silently broke the _entire_ page's script (not just
  the intended change) more than once.
- **Env vars that must be set for production, not left as `.env.example`
  placeholders**: `AUTH_SECRET_KEY`, all `S3_*` credentials,
  `RAZORPAY_*`, `GST_NUMBER`, `PLATFORM_*`, `SMTP_*`, and
  `CORS_ALLOWED_ORIGINS` (a real origin list, never `*`).

---

## Recurring gotchas

Things that have already caused real bugs in this codebase -- worth
knowing before you hit them again:

- **Near-duplicate polygon vertices** (two clicks a few cm apart while
  drawing) make CDSE's geometry validator reject the whole farm with an
  opaque 400. Both the client (`polygonToGeoJSON()` in `web/index.html`)
  and the server (`geometry_utils.py`, applied on every create/update) snap
  and dedupe vertices as a backstop -- if you're adding another geometry
  entry point, route it through `geometry_utils.py` too.
- **A product's COG is NOT scoped to one farm.** It covers the shared
  buffered bounding box of every farm on that processing tile
  (`tile_grid.py`). Any new raster-serving code must mask by the actual
  farm polygon (`get_coverage_array`, as `render_index_tile` already does)
  or it'll show neighboring fields' land.
- **`exactextract` wants GeoJSON Feature dicts**, not raw Shapely geometry
  objects -- passing a bare `Polygon` fails silently in ways that are easy
  to misattribute to something else.
- **NaN, not None**, is what a fully-masked zonal-stat window produces --
  sanitize before it reaches the DB or a JSON response (`json` rejects
  NaN outright).
- **CDSE's `download` endpoint redirects across hosts**, and `requests`
  strips the `Authorization` header on cross-host redirects by default --
  `download_manager.py` re-attaches it manually.
- **Timeouts don't raise HTTP-status-based exceptions.** A retry loop that
  only checks `response.status_code` will never fire on a raw
  `requests.Timeout`/`ConnectionError` -- needs its own `except` clause.
- **Every new API router needs an nginx location-regex entry** (see
  [Deployment](#deployment)) or it 404s to the SPA fallback in production.
- **Long unbroken strings (raw CDSE URLs in error messages) overflow
  fixed-width cards** without `overflow-wrap: anywhere` -- has recurred in
  both `web/admin/errors.html` and the dashboard's notification panel.

---

## Where to look for X

| I want to...                            | Start here                                                                                                                                                           |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Add a new satellite index               | `vyom/processing/indices.py` (S2) or `sar_indices.py` (S1), then add it to `S2_INDICES`/`s1_indices` in config, then optionally give it a legend in `index_scale.py` |
| Change how a farm is billed             | `vyom/farm_pricing.py`, `vyom/api/billing.py`                                                                                                                        |
| Add a partner-API endpoint              | `vyom/api/partner_farms.py` or `partner_tiles.py`; reuse dashboard logic where it exists rather than re-deriving it                                                  |
| Change the dashboard UI                 | `web/index.html` -- search for the relevant DOM id or function name                                                                                                  |
| Debug a stuck/failed satellite fetch    | `vyom/api/errors.py` + `web/admin/errors.html`, or the `Product.status` column directly                                                                              |
| Understand what data is real vs. filled | [Real vs. filled data](#real-vs-filled-data-interpolation)                                                                                                           |
| Add a new auth-gated route              | [Auth: three separate schemes](#auth-three-separate-schemes) -- pick the right one first                                                                             |
| Change notification behavior            | `vyom/notifications.py` (rules), `vyom/api/notifications.py` (API), `vyom/email_utils.py` (templates)                                                                |
