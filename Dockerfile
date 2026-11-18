# syntax=docker/dockerfile:1

# --- 1. Interface web (Vite) --------------------------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Chaîne vide = même origine : l'API sert elle-même l'interface (pas de CORS à régler).
ENV VITE_API_BASE_URL=""
RUN npm run build

# --- 2. Application -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    NM_ENV=production \
    NM_STATIC_DIR=/app/static
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY network_monitor ./network_monitor
COPY --from=frontend /build/dist /app/static

# Utilisateur sans privilèges ; /data (volume) reçoit la base, son verrou et l'archive Parquet.
RUN useradd --system --uid 10001 --no-create-home nm && mkdir /data && chown nm /data
USER nm
WORKDIR /data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# UN SEUL worker : la boucle de monitoring vit dans le processus (voir app/lock.py, qui refuse de
# démarrer autrement). Ne pas mettre derrière un orchestrateur qui lance plusieurs réplicas.
CMD ["python", "-m", "uvicorn", "network_monitor.app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
