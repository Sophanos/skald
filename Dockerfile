FROM python:3.12-alpine
# TODO pin digest
RUN apk add --no-cache tzdata && mkdir /data && chown 65534 /data && chmod 700 /data
WORKDIR /app
COPY server.py /app/server.py
COPY skald/ /app/skald/
ENV DATA_DIR=/data PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN printf '#!/bin/sh\nexec python -m skald "$@"\n' > /usr/local/bin/skald && chmod 755 /usr/local/bin/skald
USER 65534
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).read()"
CMD ["python", "-m", "skald.boot"]
