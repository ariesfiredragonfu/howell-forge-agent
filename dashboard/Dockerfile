# ══════════════════════════════════════════════════════════════════════════════
# dashboard/Dockerfile — Mission Control (Vite build → nginx serve)
#
# Stage 1 (builder): installs npm deps and runs `vite build`
# Stage 2 (runtime): nginx:alpine serves the built /dist on port 3000
#
# VITE_FORGE_API_HOST is injected at build time via docker-compose build-args.
# The browser JS bundle uses this to know where to connect WebSockets.
# Default: localhost:8765 (works when running locally with exposed ports)
# Override in aria.env: FORGE_API_HOST=your-server-ip:8765
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

# Receive the WebSocket host at build time
ARG VITE_FORGE_API_HOST=localhost:8765
ENV VITE_FORGE_API_HOST=${VITE_FORGE_API_HOST}

WORKDIR /build

# Layer: dependencies (only invalidated when package.json changes)
COPY package.json package-lock.json* ./
RUN npm ci --prefer-offline

# Layer: source (invalidated on any source change)
COPY . .

# Vite bakes VITE_FORGE_API_HOST into the JS bundle here
RUN npm run build


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

# Copy built assets from Stage 1
COPY --from=builder /build/dist /usr/share/nginx/html

# Copy custom nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Remove the default nginx site config
RUN rm -f /etc/nginx/conf.d/default.conf.bak

EXPOSE 3000

# nginx runs in the foreground
CMD ["nginx", "-g", "daemon off;"]
