FROM public.ecr.aws/docker/library/node@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS dependencies

ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
RUN npm install --global "pnpm@9.15.9"
COPY apps/web/package.json apps/web/pnpm-lock.yaml /app/
RUN pnpm install --frozen-lockfile

FROM dependencies AS builder
ARG PAPERFORGE_API_INTERNAL_BASE=http://api:8080
ENV PAPERFORGE_API_INTERNAL_BASE=${PAPERFORGE_API_INTERNAL_BASE} \
    NEXT_PUBLIC_DEMO_MODE=false
COPY apps/web /app
RUN pnpm build

FROM public.ecr.aws/docker/library/node@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS runner

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app

COPY --from=builder --chown=node:node /app/.next/standalone /app
COPY --from=builder --chown=node:node /app/.next/static /app/.next/static

USER node
EXPOSE 3000
CMD ["node", "server.js"]
