# Stage 1: build the dashboard
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# Stage 2: API + CLI (FFmpeg and espeak-ng give the offline voice/video fallbacks)
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg espeak-ng fonts-noto fonts-noto-core libraqm0 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
RUN pip install --no-cache-dir ".[anthropic,postgres]"
COPY --from=ui /ui/dist ./frontend/dist
ENV SAX_HOME=/data SAX_API_HOST=0.0.0.0 SAX_API_PORT=8765
VOLUME /data
EXPOSE 8765
# A non-loopback bind refuses to start without SAX_API_TOKEN, by design.
CMD ["super-agent", "serve"]
