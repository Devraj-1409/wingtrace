FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Routes and airports are built into the image, so a fresh container (e.g. a free host waking up
# with an empty disk) is ready in seconds. Rebuild the image to refresh them.
RUN mkdir -p /data && python -m app.refdata_build

EXPOSE 8000

# Serve on $PORT when the host sets one (Render does), else 8000.
# --proxy-headers: behind a proxy (Caddy, Render), use the visitor's real IP for per-visitor rate limits.
CMD ["sh", "-c", "[ -f /data/reference.sqlite ] || python -m app.refdata_build; exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
