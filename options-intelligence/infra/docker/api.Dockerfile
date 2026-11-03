FROM python:3.12-slim
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/doi ./doi
COPY migrations /migrations
COPY scripts/smoke_live.py /scripts/smoke_live.py
RUN chmod -R a+rX,go-w /app/doi /migrations /scripts
ENV MIGRATIONS_DIR=/migrations
RUN pip install --no-cache-dir . && useradd -r -u 10001 app
USER app
EXPOSE 8000
# Migrations also run at start-up inside build_runtime (advisory-locked, idempotent).
CMD ["uvicorn", "doi.app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
