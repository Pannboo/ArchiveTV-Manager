# Admin frontend (React + TypeScript), built once and served by FastAPI at /app/.
FROM node:24-slim AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Debian trixie ships FFmpeg 7.x (-readrate_initial_burst needs >= 6.1).
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATV_DATA_DIR=/data \
    ATV_MEDIA_DIR=/media \
    ATV_CACHE_DIR=/data/cache \
    ATV_PORT=8090

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tini curl nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 1000 --home /data --shell /usr/sbin/nologin archivetv

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY archivetv ./archivetv
COPY --from=frontend /src/archivetv/web/app ./archivetv/web/app

RUN mkdir -p /data /media && chown -R archivetv:archivetv /data
USER archivetv
VOLUME ["/data"]
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8090/health > /dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "archivetv.main"]
