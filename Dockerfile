# syntax=docker/dockerfile:1
#
# Minecraft dashboard image. Also used as the image for the dashboard's
# seed/snapshot Jobs (`python -m mcdash.jobs ...`).
#
# Multi-arch (linux/amd64, linux/arm64) via `docker buildx --platform`.
# Pinned versions are tracked by Renovate (see renovate.json).

ARG PYTHON_IMAGE=python:3.13-slim-trixie
ARG UV_VERSION=0.13.0

# ---------------------------------------------------------------------------
# uv binary (stage alias so the version can come from a build-arg)
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---------------------------------------------------------------------------
# Fetch helm (target arch) and the vendored itzg chart. Runs on the build
# platform: nothing here is compiled, so no QEMU emulation is needed.
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS fetch

# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=v3.22.0
# renovate: datasource=helm depName=minecraft registryUrl=https://itzg.github.io/minecraft-server-charts
ARG ITZG_CHART_VERSION=5.2.0
ARG TARGETARCH
ARG BUILDARCH

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
WORKDIR /fetch

# Download helm for both the target arch (shipped in the image) and the build
# arch (used here to `helm pull` the chart), verifying the published sha256.
RUN set -eux; \
    fetch_helm() { \
      local arch="$1" dest="$2" tgz="helm-${HELM_VERSION}-linux-$1.tar.gz"; \
      python3 -c 'import sys, urllib.request as u; u.urlretrieve(sys.argv[1], sys.argv[2])' \
        "https://get.helm.sh/${tgz}" "${tgz}"; \
      python3 -c 'import sys, urllib.request as u; u.urlretrieve(sys.argv[1], sys.argv[2])' \
        "https://get.helm.sh/${tgz}.sha256sum" "${tgz}.sha256sum"; \
      sha256sum -c "${tgz}.sha256sum"; \
      mkdir -p "${arch}"; \
      tar -xzf "${tgz}" -C "${arch}" --strip-components=1 "linux-${arch}/helm"; \
      install -D -m 0755 "${arch}/helm" "${dest}"; \
    }; \
    fetch_helm "${TARGETARCH}" /out/helm; \
    fetch_helm "${BUILDARCH}" /usr/local/bin/helm; \
    mkdir -p /out/charts; \
    HOME=/tmp helm pull minecraft \
      --repo https://itzg.github.io/minecraft-server-charts \
      --version "${ITZG_CHART_VERSION}" \
      --untar --untardir /out/charts; \
    test -f /out/charts/minecraft/Chart.yaml

# ---------------------------------------------------------------------------
# Build the virtualenv with uv. Runs on the target platform so any native
# wheels (uvloop, httptools, ...) match the runtime architecture.
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------
# Runtime
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.source="https://github.com/gilesknap/k3s-minecraft" \
      org.opencontainers.image.description="Dashboard for launching and managing itzg Minecraft servers on Kubernetes" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN groupadd --gid 1000 mcdash \
    && useradd --uid 1000 --gid 1000 --home-dir /home/mcdash --create-home \
       --shell /usr/sbin/nologin mcdash

COPY --from=fetch /out/helm /usr/local/bin/helm
COPY --from=fetch /out/charts/minecraft /opt/charts/minecraft
COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/mcdash \
    MCD_ITZG_CHART=/opt/charts/minecraft

USER 1000:1000
WORKDIR /home/mcdash
EXPOSE 8080

CMD ["mcdash", "serve"]
