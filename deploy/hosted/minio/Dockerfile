# syntax=docker/dockerfile:1
# Auxiliary storage image, not a second MesoForge application image. The official
# binary image repositories are unavailable. Build the upstream security release
# from its exact Git object; module dependencies retain upstream go.sum validation.
FROM golang:1.24.8-bookworm@sha256:4ed690d6649d63c312b99a6120025ec79ce3b542968a37da53d6236c7c61a848 AS build
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /build
RUN git init \
    && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth=1 origin 9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a \
    && MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go 2025-10-15T17:29:55Z > /tmp/ldflags \
    && go build -mod=readonly -trimpath -ldflags "$(cat /tmp/ldflags)" -o /minio .

FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 minio \
    && useradd --system --uid 10001 --gid 10001 --home-dir /var/lib/minio --no-create-home minio \
    && mkdir /data /var/lib/minio \
    && chown 10001:10001 /data /var/lib/minio
COPY --from=build /minio /usr/local/bin/minio
COPY --from=build /build/LICENSE /usr/share/doc/minio/LICENSE
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
      org.opencontainers.image.revision="9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a" \
      org.opencontainers.image.version="RELEASE.2025-10-15T17-29-55Z"
USER 10001:10001
ENV HOME=/var/lib/minio
VOLUME ["/data"]
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data"]
