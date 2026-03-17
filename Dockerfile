FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy source
COPY src/ src/
COPY scripts/ scripts/

# Fetch clubs data at build time
ARG LAZUZ_REFRESH_TOKEN
ARG LAZUZ_AUTH_TOKEN
RUN uv run python scripts/fetch_clubs.py

CMD ["uv", "run", "python", "scripts/telegram_bot.py"]
