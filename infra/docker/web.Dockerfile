# The dashboard.
#
# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS builder

# pnpm via corepack — the version comes from packageManager in package.json, so the build uses the
# same pnpm the developers do rather than whatever is newest.
RUN corepack enable

WORKDIR /repo

# Manifests first for the dependency layer, same reasoning as the API image.
# No root package.json in this repository — the pnpm workspace is defined by pnpm-workspace.yaml
# alone, and the only member is apps/web.
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN --mount=type=cache,target=/pnpm-store \
    pnpm config set store-dir /pnpm-store && pnpm install --frozen-lockfile

COPY apps/web/ apps/web/

# The build typechecks: `ignoreBuildErrors` is false in next.config.ts, so a type error fails here
# rather than shipping a page that breaks in a browser.
WORKDIR /repo/apps/web
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

# ---------------------------------------------------------------- runtime
FROM node:22-bookworm-slim AS runtime

RUN groupadd --system --gid 1001 proofstep \
    && useradd --system --uid 1001 --gid proofstep --home /app proofstep

WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Three copies, because that is what `output: standalone` produces: the server and its traced
# dependencies, the static assets Next serves itself, and anything in public/.
COPY --from=builder --chown=root:root /repo/apps/web/.next/standalone ./
COPY --from=builder --chown=root:root /repo/apps/web/.next/static ./apps/web/.next/static

USER proofstep
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD node -e "require('http').get('http://127.0.0.1:3000/login',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

# The standalone output places the server where the workspace package lived.
CMD ["node", "apps/web/server.js"]
