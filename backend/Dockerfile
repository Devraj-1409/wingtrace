FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

VOLUME /data
EXPOSE 8000

# Download the reference data (routes, airports) on first start, then serve.
# --proxy-headers: behind Caddy, use the visitor's real IP for per-visitor rate limits.
CMD ["sh", "-c", "[ -f /data/reference.sqlite ] || python -m app.refdata_build; exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
