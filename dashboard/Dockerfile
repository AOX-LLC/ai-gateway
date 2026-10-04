# syntax=docker/dockerfile:1
# The dashboard: Next.js standalone output, run by a non-root user on a read-only filesystem.
# The base image is pinned by digest, like every image in this repository.

FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS deps
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund

FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build
# One build worker (next.config.ts) and a capped heap: the machine is shared.
ENV NEXT_TELEMETRY_DISABLED=1 NODE_OPTIONS=--max-old-space-size=1536
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS run
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=4400 HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
# The fonts' licence texts travel with the font files (SIL OFL 1.1; see THIRD_PARTY_NOTICES.md).
COPY --from=build --chown=node:node /app/src/fonts/ibm-plex-sans/OFL.txt ./licenses/ibm-plex-sans-OFL.txt
COPY --from=build --chown=node:node /app/src/fonts/ibm-plex-mono/OFL.txt ./licenses/ibm-plex-mono-OFL.txt
COPY --from=build --chown=node:node /app/src/fonts/space-grotesk/OFL.txt ./licenses/space-grotesk-OFL.txt
# The icon paths in the bundle follow Tabler Icons (MIT): its notice travels with them.
COPY --from=build --chown=node:node /app/licenses/tabler-icons-MIT.txt ./licenses/tabler-icons-MIT.txt
USER node
EXPOSE 4400
CMD ["node", "server.js"]
