# syntax=docker/dockerfile:1
FROM node:24.13.0-bookworm-slim AS development
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
        NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca npm ci --strict-ssl=true; \
    else npm ci --strict-ssl=true; fi
COPY . .
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

FROM development AS build
ARG VITE_TELEGRAM_BOT_URL=""
RUN VITE_TELEGRAM_BOT_URL="$VITE_TELEGRAM_BOT_URL" npm run build

# Export this target with BuildKit. Cloudflare serves the assets and Worker.
FROM scratch AS production
COPY --from=build /app/dist /dist
COPY --from=build /app/worker /worker
COPY --from=build /app/wrangler.jsonc /wrangler.jsonc
