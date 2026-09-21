FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: audio/video decoding; tini: clean shutdown and zombie reaping
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp needs a JS runtime to solve YouTube challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY assets ./assets
RUN pip install .

RUN useradd -m -u 1000 bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "tunegram"]
