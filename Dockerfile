FROM python:3.12-slim-bookworm

# Variables d'env build-time (injectées par CI)
ARG SEH_VERSION=dev
ARG SEH_CHANNEL=stable
ENV SEH_VERSION=${SEH_VERSION} SEH_CHANNEL=${SEH_CHANNEL}

# Système : juste ce qu'il faut, pas de paquets superflus
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Dossier app
WORKDIR /app

# Dépendances Python en premier (cache layer Docker)
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Code backend
COPY backend/ /app/

# Frontend statique
COPY frontend/ /app/frontend/

# Volume pour la persistance (DB, config, secrets)
VOLUME ["/data"]

# Port HTTP (FastAPI)
EXPOSE 8000

# Healthcheck (utilisé par le watchdog du sprint 14)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Démarrage
ENV DATA_DIR=/data \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
