FROM node:22-bookworm-slim AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Force the same-origin production default; no developer .env enters the build.
ENV VITE_API_BASE_URL=""
RUN npm run build

FROM python:3.11-slim AS runtime

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Explicit runtime inputs: no frontend sources, tests, Node or node_modules.
COPY *.py alembic.ini ./
COPY routers/ ./routers/
COPY services/ ./services/
COPY alembic/ ./alembic/
COPY --from=frontend-build /build/frontend/dist/ ./frontend/dist/

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"${PORT:-8000}\""]
