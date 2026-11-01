# Argus application image.
# Pinned to Python 3.12 because it has the broadest dependency wheel coverage;
# the project itself supports 3.11+.
FROM python:3.15-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY config ./config

# Runtime extras used by the long-running services. Dev/test tooling stays out of the image.
RUN pip install --upgrade pip && \
    pip install ".[ingestion,brain,interface,platform,dashboard]"

# Directories for runtime data
RUN mkdir -p /app/data /app/logs

# Non-root user (least privilege)
RUN useradd --create-home --uid 10001 argus && chown -R argus:argus /app
USER argus

# Default container behaviour is the long-running dashboard API, which never submits orders.
CMD ["python", "-m", "argus", "--dashboard"]
