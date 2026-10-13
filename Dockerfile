FROM jlesage/makemkv:v26.09.2

RUN add-pkg python3

COPY app /opt/disc-ingest
COPY rootfs/startapp.sh /startapp.sh

RUN chmod 0755 /startapp.sh /opt/disc-ingest/server.py

ENV MAKEMKV_GUI=1 \
    AUTO_DISC_RIPPER=0 \
    APP_PORT=8080 \
    MEDIA_ROOT=/mnt/data \
    AUTO_EJECT=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/state', timeout=3)" || exit 1
