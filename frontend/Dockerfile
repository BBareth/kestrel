# syntax=docker/dockerfile:1.7
# --- 1. build the PWA -------------------------------------------------------------
FROM node:24-alpine AS build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- 2. compile Caddy with a patched Go toolchain + dependency updates ---------------
# The official caddy image (v2.11.4) is built with Go 1.26.3 and older x/net, x/crypto,
# x/text and grpc modules that have known HIGH CVEs. Rebuild the same Caddy release
# with current Go and bumped modules instead of waiting for an upstream image.
FROM golang:1.26-alpine AS caddy
ARG CADDY_VERSION=v2.11.4
RUN apk add --no-cache git
WORKDIR /src
RUN git clone --depth 1 --branch "${CADDY_VERSION}" https://github.com/caddyserver/caddy.git . \
 && go get golang.org/x/crypto@latest golang.org/x/net@latest golang.org/x/text@latest google.golang.org/grpc@latest \
 && go mod tidy \
 && cd cmd/caddy && CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /out/caddy \
 && /out/caddy version

# --- 3. runtime ----------------------------------------------------------------------
FROM caddy:2-alpine
COPY --from=caddy /out/caddy /usr/bin/caddy
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /web/dist /srv
