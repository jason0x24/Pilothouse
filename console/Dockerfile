# Multi-stage Dockerfile for the Pilothouse Next.js console.
#
# Build stage produces a `next build` output; runtime stage runs
# `next start` from a minimal Node base. ~250 MB final image (dominated
# by the Next runtime + your component tree).

FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
# We don't have a lockfile in this repo; npm install resolves at build
# time. Real deployments would commit package-lock.json and use `npm ci`.
RUN npm install --silent

FROM node:20-alpine AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-alpine AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000
WORKDIR /app

# Only copy what's needed at runtime — avoids shipping the build cache.
COPY --from=build /app/package.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/next.config.mjs ./

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3000 >/dev/null || exit 1

CMD ["npm", "run", "start"]
