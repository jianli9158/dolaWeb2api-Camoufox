FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOLA_HOST=0.0.0.0 \
    DOLA_PORT=9090 \
    DOLA_HEADLESS=true \
    DOLA_BROWSER_DATA=/app/browser_data \
    ALLOW_UNPROTECTED_BIND=false

# Install native Camoufox (Firefox) headless shared libraries (Debian Bookworm / Trixie compatible)
# Reference: flow2api-Camoufox Dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        fontconfig \
        libgtk-3-0 \
        libdbus-glib-1-2 \
        libx11-xcb1 \
        libxtst6 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libxfixes3 \
    && (apt-get install -y --no-install-recommends libasound2t64 \
        || apt-get install -y --no-install-recommends libasound2) \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY . .

RUN mkdir -p /app/browser_data /app/runtime

EXPOSE 9090

CMD ["python", "-m", "doubao2api"]
