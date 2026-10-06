# syntax=docker/dockerfile:1
# One MesoForge image for every hosted role: guidance-worker, forecast-worker and
# operator commands (python -m mesoforge.application.<module>). No secrets, caches or
# runtime data are baked in. Build it from a clean commit with
# deploy/hosted/build-image.sh, which streams exactly the committed bytes (git archive)
# and passes that commit as MESOFORGE_CODE_REVISION.
#
# For byte-reproducible bases, pin both images to digests, for example
#   --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:<digest>
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.6

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
# The virtual environment links to the base image's interpreter, which the runtime
# stage shares; uv never downloads its own Python.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /app
# Locked third-party dependencies first (no development tools), then the project.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY configs ./configs
COPY src ./src
RUN uv sync --locked --no-dev \
    && python -m compileall -q /app/src

FROM ${PYTHON_IMAGE} AS runtime
ARG MESOFORGE_CODE_REVISION
RUN case "${MESOFORGE_CODE_REVISION}" in \
      *[!0-9a-f]*|"") echo "MESOFORGE_CODE_REVISION must be a 40-hex commit" >&2; exit 1 ;; \
    esac \
    && [ "${#MESOFORGE_CODE_REVISION}" -eq 40 ] \
    || { echo "MESOFORGE_CODE_REVISION must be a 40-hex commit" >&2; exit 1; }
# Operating-system time zones (the tzdata wheel is Windows-only) and CA roots.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Non-root runtime user. The runtime mount point is created and owned here so an
# empty named volume inherits this ownership on first mount.
RUN groupadd --system --gid 10001 mesoforge \
    && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/mesoforge \
       --no-create-home --shell /usr/sbin/nologin mesoforge \
    && mkdir -p /var/lib/mesoforge/runtime \
    && chown -R 10001:10001 /var/lib/mesoforge
COPY --from=build /app /app
# Keep the locked ecCodes/eckit binaries, but resolve their dependencies through the
# native loader instead of findlibs' RTLD_GLOBAL preload of every eckit library.
# The latter makes ecCodes-before-Psycopg terminate with SIGSEGV on Linux. The
# package search must be disabled because it otherwise precedes ECCODES_HOME.
ENV FINDLIBS_DISABLE_PACKAGE=yes \
    ECCODES_HOME=/app/.venv/lib/python3.12/site-packages/eccodeslib \
    LD_LIBRARY_PATH=/app/.venv/lib/python3.12/site-packages/eckitlib/lib64:/app/.venv/lib/python3.12/site-packages/eccodeslib/lib64
ENV PATH=/app/.venv/bin:${PATH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/mesoforge \
    MESOFORGE_CODE_REVISION=${MESOFORGE_CODE_REVISION} \
    MESOFORGE_PROSPECTIVE_ROOT=/var/lib/mesoforge/runtime \
    MESOFORGE_OBSERVATIONS_DIR=/var/lib/mesoforge/runtime/observations \
    MESOFORGE_MRMS_DIR=/var/lib/mesoforge/runtime/observations/mrms
# Source and configuration stay root-owned and read-only for the runtime user; the
# working directory matters because some configuration paths are relative to it.
WORKDIR /app
USER 10001:10001
# Both fresh interpreters must finish normally: successful imports/prints alone
# miss the native shutdown crash. Exercise GRIB and libpq without network or data.
RUN python -c "import eccodes; import psycopg; \
assert psycopg.pq.__impl__ == 'binary'; \
assert psycopg.pq.version() > 0; \
handle = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2'); \
assert eccodes.codes_get(handle, 'edition') == 2; \
assert len(eccodes.codes_get_values(handle)) > 0; \
eccodes.codes_release(handle)" \
    && python -c "import psycopg; import eccodes; \
assert psycopg.pq.__impl__ == 'binary'; \
assert psycopg.pq.version() > 0; \
handle = eccodes.codes_grib_new_from_samples('regular_ll_sfc_grib2'); \
assert eccodes.codes_get(handle, 'edition') == 2; \
assert len(eccodes.codes_get_values(handle)) > 0; \
eccodes.codes_release(handle)"
VOLUME ["/var/lib/mesoforge/runtime"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m"]
CMD ["mesoforge.application.guidance_worker", "run"]
