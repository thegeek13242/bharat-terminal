# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

COPY frontend/package.json ./
# Install with npm ci for reproducible builds; generate lock file if absent
RUN npm install

COPY frontend/ ./
# Vite inlines VITE_* env at build time; pass the Google OAuth client id through.
ARG VITE_GOOGLE_CLIENT_ID=""
ENV VITE_GOOGLE_CLIENT_ID=$VITE_GOOGLE_CLIENT_ID
RUN npm run build

# ── Stage 2: Serve ────────────────────────────────────────────────────────────
FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY docker/frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=3s \
  CMD wget -q --spider --no-check-certificate https://127.0.0.1/ || exit 1
