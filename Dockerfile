# One image, four roles (api / worker-priority / worker-main / beat) -- the role
# is just the command set in docker-compose.yml.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# rasterio / rio-cogeo / shapely / pyproj / exactextract ship manylinux wheels
# with GDAL/GEOS bundled, so no system GDAL is needed.
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY vyom ./vyom
COPY migrations ./migrations
# vyom/branding.py reads the logo from web/assets/img (invoice PDFs, emails).
COPY web/assets ./web/assets

# Run as a normal user; /app/data is where raw/processed files land when
# STORAGE_BACKEND=local (mounted as a volume in compose).
RUN useradd --create-home --uid 10001 vyom \
    && mkdir -p /app/data && chown -R vyom:vyom /app
USER vyom

EXPOSE 8000
CMD ["uvicorn", "vyom.api.main:app", "--host", "0.0.0.0", "--port", "8000"]