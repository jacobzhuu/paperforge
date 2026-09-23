# Match the running PostgreSQL distribution; adding extension files needs no server restart.
ARG POSTGRES_BASE=paperforge-postgres:local
FROM ${POSTGRES_BASE} AS build
USER root
RUN apk add --no-cache build-base curl
ARG PGVECTOR_VERSION=0.8.2
RUN curl -fsSL "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
    | tar xz -C /tmp \
    && cd /tmp/pgvector-${PGVECTOR_VERSION} \
    && make OPTFLAGS="" with_llvm=no \
    && make install with_llvm=no
FROM ${POSTGRES_BASE}
COPY --from=build /usr/local/lib/postgresql/vector.so /usr/local/lib/postgresql/vector.so
COPY --from=build /usr/local/share/postgresql/extension/vector* /usr/local/share/postgresql/extension/
