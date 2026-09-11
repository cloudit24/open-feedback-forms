# Open Feedback Forms — runtime image.
#
# Pure-stdlib app plus one real dependency (the MariaDB driver) and tzdata
# (Debian's slim image already ships a full IANA zoneinfo database, so
# tzdata is only strictly required on Windows — installed anyway here for
# parity with the Windows .msi build and to pin a known-good version).
FROM python:3.13-slim

LABEL org.opencontainers.image.title="Open Feedback Forms" \
      org.opencontainers.image.source="https://github.com/cloudit24/open-feedback-forms" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

RUN pip install --no-cache-dir "mysql-connector-python>=9.0" "tzdata>=2024.1"

COPY server.py db.py config_store.py translate_client.py ui_strings.py notifier.py VERSION ./
COPY public/ ./public/
COPY admin/ ./admin/
COPY docker/entrypoint.sh docker/bootstrap.py ./docker/

RUN useradd --create-home --uid 1000 offuser \
    && mkdir -p /data \
    && chown -R offuser:offuser /app /data \
    && chmod +x docker/entrypoint.sh

USER offuser

# Config and uploaded logos persist here — mount a volume at /data (see
# docker-compose.yml). Binding 0.0.0.0 is required inside a container;
# 127.0.0.1 (the default outside Docker) would be unreachable from outside it.
ENV OFF_CONFIG_DIR=/data \
    HOST=0.0.0.0 \
    ADMIN_PORT=8080 \
    FORM_PORT=8081

EXPOSE 8080 8081 8090-8189

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["python", "server.py"]
