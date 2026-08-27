# syntax=docker/dockerfile:1.7

ARG GO_VERSION
FROM golang:${GO_VERSION}-bookworm

ARG TARGETARCH
ARG PROTOC_VERSION=29.3
ARG PROTOC_GEN_GO_VERSION=v1.36.3
ARG PROTOC_GEN_GO_GRPC_VERSION=v1.5.1
ARG OAPI_CODEGEN_VERSION=v2.4.1
ARG DEPENDENCY_GITHUB_RAW_URL=https://github.com
ARG DEPENDENCY_APT_DEBIAN_URL=
ARG DEPENDENCY_APT_DEBIAN_SECURITY_URL=
RUN if [ -n "$DEPENDENCY_APT_DEBIAN_URL$DEPENDENCY_APT_DEBIAN_SECURITY_URL" ]; then \
      find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -exec sed -i \
        -e "s|http://deb.debian.org/debian-security|$DEPENDENCY_APT_DEBIAN_SECURITY_URL|g" \
        -e "s|http://deb.debian.org/debian|$DEPENDENCY_APT_DEBIAN_URL|g" {} +; \
    fi

RUN rm -f /etc/apt/apt.conf.d/docker-clean
RUN --mount=type=cache,id=standalone-go-apt-lists-${TARGETARCH},target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,id=standalone-go-apt-cache-${TARGETARCH},target=/var/cache/apt,sharing=locked \
    apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       ca-certificates curl make unzip \
    && case "${TARGETARCH}" in \
         amd64) protoc_arch=x86_64 ;; \
         arm64) protoc_arch=aarch_64 ;; \
         *) echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl --fail --location --silent --show-error \
       "${DEPENDENCY_GITHUB_RAW_URL}/protocolbuffers/protobuf/releases/download/v${PROTOC_VERSION}/protoc-${PROTOC_VERSION}-linux-${protoc_arch}.zip" \
       --output /tmp/protoc.zip \
    && unzip -q /tmp/protoc.zip bin/protoc -d /usr/local \
    && rm -f /tmp/protoc.zip

RUN --mount=type=cache,id=standalone-go-modules-${TARGETARCH},target=/go/pkg/mod,sharing=locked \
    GOBIN=/usr/local/bin go install google.golang.org/protobuf/cmd/protoc-gen-go@${PROTOC_GEN_GO_VERSION} \
    && GOBIN=/usr/local/bin go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@${PROTOC_GEN_GO_GRPC_VERSION} \
    && GOBIN=/usr/local/bin go install github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@${OAPI_CODEGEN_VERSION}
