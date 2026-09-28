# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd -g 10001 kestrel && useradd -r -u 10001 -g kestrel -d /app -s /usr/sbin/nologin kestrel
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app

FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests ./tests
COPY pyproject.toml .
USER 10001
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime
# Build tooling is not needed at runtime: removing pip/setuptools also removes their
# vendored libraries (e.g. msgpack) from the attack surface and from CVE scans.
RUN python -m pip uninstall -y setuptools wheel pip 2>/dev/null || true
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header", "--timeout-graceful-shutdown", "10"]
