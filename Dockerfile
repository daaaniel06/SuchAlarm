FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data INVISIBLE_PLAYWRIGHT_ENGINE_DIR=/opt/invisible-engine
WORKDIR /app
# Stealth Firefox (invisible_playwright): Xvfb for its headless mode and the libraries Firefox links against.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xauth libgtk-3-0t64 libdbus-glib-1-2 libasound2t64 libx11-xcb1 libxtst6 libpci3 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && invisible-playwright fetch \
    && useradd -u 10001 -m app && mkdir /data && chown app:app /data

# Test tools are installed before the app code is copied, so a code change keeps them cached.
FROM base AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY suchalarm ./suchalarm
# Every test file runs; .dockerignore leaves out the browser test (needs Playwright Chromium, a dev-only tool).
COPY tests ./tests
USER app
ENV RUFF_CACHE_DIR=/tmp/ruff MYPY_CACHE_DIR=/tmp/mypy
CMD ["sh", "-c", "ruff check . && ruff format --check . && mypy && python -m unittest discover -s tests -p 'test_*.py' -v"]

# The last stage is what a plain `docker build` produces.
FROM base AS production
COPY suchalarm ./suchalarm
USER app
# Uvicorn trusts X-Forwarded-* only from these addresses; set the reverse proxy's (e.g. its Docker network).
ENV FORWARDED_ALLOW_IPS=127.0.0.1
EXPOSE 8000
CMD ["uvicorn", "suchalarm.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
