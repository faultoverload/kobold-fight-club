# Stage 1: build
FROM node:20-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
# Fetch and inject 5etools monsters before building
RUN apt-get update && apt-get install -y python3 python3-pip --no-install-recommends \
    && pip3 install httpx --break-system-packages \
    && python3 scripts/sync_monsters.py \
    && apt-get remove -y python3 python3-pip && rm -rf /var/lib/apt/lists/*
RUN npm run build

# Stage 2: serve
FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
