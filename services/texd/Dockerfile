# PaperForge texd：Tectonic 编译沙箱。
# 运行时无外网（compose internal 网络）；Tectonic 首次会拉包，故构建期预热包缓存。
FROM public.ecr.aws/docker/library/python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# Tectonic runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Pin the release and verify GitHub's published SHA-256 digest.
ARG TARGETARCH
ARG TECTONIC_VERSION=0.16.9
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) target="x86_64-unknown-linux-gnu"; sha="f3c825128095dc3399ea11c08c18035b33050a216930c295c79e8eb11bd21de4" ;; \
      arm64) target="aarch64-unknown-linux-musl"; sha="f9aa39017dbd51f111fdb93dda222178cbe51c8193508fc567b523cc74fff9c1" ;; \
      *) echo "unsupported Docker architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="tectonic-${TECTONIC_VERSION}-${target}.tar.gz"; \
    url="https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/${archive}"; \
    curl -fsSL "${url}" -o /tmp/tectonic.tar.gz; \
    echo "${sha}  /tmp/tectonic.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/tectonic.tar.gz -C /usr/local/bin tectonic; \
    rm /tmp/tectonic.tar.gz; \
    tectonic --version

WORKDIR /app
COPY services/texd/paperforge_texd /app/paperforge_texd
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30"

# Build-time warmup. Toy documents are not enough: Tectonic caches per *file*,
# so class options (size11.clo), packages, fonts and .bst files only land in the
# cache if the warmup compiles the same preambles PaperForge actually ships.
# The runtime is networkless + read-only, so anything missed here fails there.
COPY services/texd/warmup /warmup
RUN set -eu; \
    for class in article IEEEtran acmart llncs; do \
      printf '\\documentclass{%s}\\begin{document}warmup\\end{document}' "${class}" > "/tmp/${class}.tex"; \
      (cd /tmp && tectonic "${class}.tex"); \
    done; \
    cp /warmup/refs.bib /tmp/refs.bib; \
    for doc in /warmup/warmup-*.tex; do \
      cp "${doc}" /tmp/; \
      (cd /tmp && tectonic --keep-intermediates "$(basename "${doc}")"); \
    done; \
    rm -f /tmp/*.tex /tmp/*.pdf /tmp/*.aux /tmp/*.bbl /tmp/*.blg /tmp/*.log /tmp/refs.bib

EXPOSE 8081
CMD ["uvicorn", "paperforge_texd.main:app", "--host", "0.0.0.0", "--port", "8081"]
