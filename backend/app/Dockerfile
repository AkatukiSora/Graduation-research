# syntax=docker/dockerfile:1.7

FROM --platform=$BUILDPLATFORM golang:1.25.11 AS builder

ARG TARGETOS
ARG TARGETARCH

WORKDIR /src

COPY go.mod go.sum ./

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    go mod download

COPY cmd ./cmd
COPY db ./db
COPY internal ./internal

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -tags lambda.norpc -trimpath -ldflags="-s -w" -o /out/api ./cmd/api

RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath -ldflags="-s -w" -o /out/migrate ./cmd/migrate

FROM --platform=$BUILDPLATFORM alpine:3.22 AS atlas

ARG TARGETARCH

RUN apk add --no-cache curl
RUN mkdir -p /atlas-bin
RUN curl -sSf https://atlasgo.sh | sh -s -- -y --no-install -o /atlas-bin/atlas --platform linux-$TARGETARCH && chmod +x /atlas-bin/atlas

FROM public.ecr.aws/lambda/provided:al2023 AS runner

COPY --chown=10001:10001 --chmod=0555 --from=atlas /atlas-bin/atlas /usr/local/bin/atlas
COPY --chown=10001:10001 --chmod=0555 --from=builder /out/api /api
COPY --chown=10001:10001 --chmod=0555 --from=builder /out/migrate /migrate
COPY --chown=10001:10001 db /app/db
COPY --chown=10001:10001 mockResponse.json /app/mockResponse.json

USER 10001:10001

ENTRYPOINT ["/api"]
