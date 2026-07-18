# ---- build stage: dependencies into a clean virtualenv ----------------------
FROM python:3.12-slim AS build
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /venv && \
    /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# ---- runtime -----------------------------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Crossfadarr" \
      org.opencontainers.image.description="Bridge your YouTube Music library into Lidarr (metadata only)" \
      org.opencontainers.image.source="https://github.com/crossfadarr/crossfadarr" \
      org.opencontainers.image.licenses="MIT"

RUN useradd --uid 1000 --create-home app && \
    mkdir -p /config && chown app:app /config

COPY --from=build /venv /venv
COPY --chown=app:app *.py /app/

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    CROSSFADARR_HOST=0.0.0.0 \
    CROSSFADARR_PORT=5000

# All state lives here: config.yaml, auth.json, data/*.json, *_cache.db.
# The app uses cwd-relative paths, so the working directory IS the volume.
WORKDIR /config
VOLUME /config

USER app
EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('CROSSFADARR_PORT','5000')+'/favicon.svg')"]

CMD ["python", "/app/app.py"]
