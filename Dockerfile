FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KWAZAR_DATA_DIR=/var/lib/kwazar
WORKDIR /app
RUN groupadd --gid 10001 kwazar && useradd --uid 10001 --gid 10001 --no-create-home kwazar && mkdir -p /var/lib/kwazar /backups && chown 10001:10001 /var/lib/kwazar /backups
COPY --chown=10001:10001 server.py backup_store.py index.html admin.html order.html app.js account.js styles.css catalog.json selection.json ./
COPY --chown=10001:10001 assets/ ./assets/
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python3", "-c", "import urllib.request; r=urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/healthz', timeout=3); assert r.status == 200"]
CMD ["python3", "server.py", "--bind", "0.0.0.0", "--port", "8000"]
