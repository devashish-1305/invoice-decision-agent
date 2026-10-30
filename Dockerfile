# One image, one web service: FastAPI serves the API and the built React app.

# ---- Stage 1: build the frontend ------------------------------------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: the app -----------------------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    DATABASE_URL=sqlite:////app/data/invoice_agent.db

# backend/ at /app/backend and the build at /app/frontend/dist: config.py finds the build at ../frontend/dist.
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/ ./
COPY --from=frontend /build/frontend/dist /app/frontend/dist

# The SQLite file lives in /app/data. On a fresh container it is created and seeded on startup.
RUN useradd --create-home app && mkdir -p /app/data && chown -R app:app /app/data
USER app

EXPOSE 8000
# $PORT is set by the platform (Render); 8000 when run locally. Exactly one process: runs and SSE share it.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
