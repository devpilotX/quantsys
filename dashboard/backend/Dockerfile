# qsdash API / ws hub / engine runner — one image, three commands.
FROM python:3.12-slim

WORKDIR /app
# QSDASH_MIGRATIONS_DIR: the installed package does not ship the Alembic
# scripts; `python -m qsdash.cli init-db` reads them from the copy below.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 QSDASH_MIGRATIONS_DIR=/app/backend

# engine first (its deps are the heavy layer and change rarely)
COPY pyproject.toml README.md /app/engine/
COPY constraints /app/engine/constraints
COPY src /app/engine/src
COPY config /app/engine/config
# Pinned to the set CI tests on 3.12, the Python this image runs, so the paper
# engine's numerical stack does not drift on every rebuild.
RUN pip install "/app/engine[broker]" -c /app/engine/constraints/linux-py3.12.txt

COPY dashboard/backend /app/backend
# Not constrained by the engine pins: the backend has its own dependency tree
# (fastapi, sqlalchemy, argon2), which constraints/ was not resolved against.
# pyarrow: parquet IO for the research monitors (down-shock daily tracker),
# baked in so the timer job does not pip-install on every run
RUN pip install /app/backend pyarrow

# default: API. compose overrides command for ws-worker / engine.
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://127.0.0.1:8000/api/health', timeout=4).raise_for_status()"
CMD ["uvicorn", "qsdash.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
