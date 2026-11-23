FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend
COPY scripts ./scripts

# SQLite DB, media and generated JWT secrets live here; mount it as a volume.
ENV APP_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000
# One worker: the monitor timer runs in-process. Proxy headers: Traefik terminates HTTPS.
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
