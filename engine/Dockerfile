# syntax=docker/dockerfile:1
#
# Referee engine: a custom code-execution image for agent loops.
#
# Bakes in toolchains (Microsoft Build of OpenJDK 21, Maven, Go, Python + pytest + ruff), the referee
# CLI, and the target projects. Dependency caches are deliberately NOT baked in: prepare_images.py
# boots this image as a sandbox, fills the caches once, and commits the result as a "primed" disk image.
FROM mcr.microsoft.com/mirror/docker/library/ubuntu:24.04

ARG MAVEN_VERSION=3.9.9
ARG MAVEN_SHA512=a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b
ARG GO_VERSION=1.27.2
ARG GO_SHA256=ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5

ENV DEBIAN_FRONTEND=noninteractive

# OS tools, Python 3.12, and the Microsoft Build of OpenJDK 21.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git bash python3 python3-venv \
 && curl -fsSL -o /tmp/packages-microsoft-prod.deb https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb \
 && dpkg -i /tmp/packages-microsoft-prod.deb \
 && rm /tmp/packages-microsoft-prod.deb \
 && apt-get update \
 && apt-get install -y --no-install-recommends msopenjdk-21 \
 && ln -s "$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")" /opt/java \
 && rm -rf /var/lib/apt/lists/*

# Maven and Go from their official archives, verified by checksum.
RUN curl -fsSL -o /tmp/maven.tar.gz "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
 && echo "${MAVEN_SHA512}  /tmp/maven.tar.gz" | sha512sum -c - \
 && mkdir -p /opt/maven \
 && tar -xzf /tmp/maven.tar.gz -C /opt/maven --strip-components=1 \
 && rm /tmp/maven.tar.gz \
 && curl -fsSL -o /tmp/go.tar.gz "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
 && echo "${GO_SHA256}  /tmp/go.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/go.tar.gz -C /usr/local \
 && rm /tmp/go.tar.gz

# Python test and lint tools in a private virtual environment.
RUN python3 -m venv /opt/referee/venv \
 && /opt/referee/venv/bin/pip install --no-cache-dir pytest==8.3.4 ruff==0.8.6

# The engine itself, plus the projects it knows how to referee.
COPY referee /opt/referee/lib/referee
COPY targets /opt/referee/targets
COPY bin/referee /usr/local/bin/referee
RUN sed -i 's/\r$//' /usr/local/bin/referee \
 && chmod 0755 /usr/local/bin/referee \
 && mkdir -p /work /opt/referee/primed /opt/referee/m2 /opt/referee/go/cache /opt/referee/go/path \
 && chmod 0777 /work \
 && /usr/local/bin/referee describe > /dev/null

ENV JAVA_HOME=/opt/java \
    MAVEN_HOME=/opt/maven \
    PATH=/opt/referee/venv/bin:/opt/java/bin:/opt/maven/bin:/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LABEL org.opencontainers.image.title="referee-engine" \
      org.opencontainers.image.description="Custom code-execution engine for Azure Container Apps Sandboxes"

WORKDIR /work
# A sandbox only needs a long-running process; the orchestrator drives the engine through exec.
CMD ["sleep", "infinity"]
