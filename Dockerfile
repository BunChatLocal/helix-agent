FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/root \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/usr/local/bin:/root/.local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

# Tailscale (official apt repo). Only started when TS_AUTHKEY is set — used by
# the optional owner-only /aihub proxy (web/aihub.py). Harmless otherwise.
RUN curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg \
    && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
        -o /etc/apt/sources.list.d/tailscale.list \
    && apt-get update && apt-get install -y --no-install-recommends tailscale \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# First-boot seed only. Live config lives on the Railway volume at /root/.helix.
# docker-seed/ is gitignored, so a CLI upload would fail COPY if we required it.
RUN mkdir -p /seed/helix-home

RUN sed -i 's/\r$//' /app/deploy/entrypoint.sh \
    && chmod +x /app/deploy/entrypoint.sh

EXPOSE 8080
CMD ["/app/deploy/entrypoint.sh"]
