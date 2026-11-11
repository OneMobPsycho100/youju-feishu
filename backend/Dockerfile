FROM python:3.12-slim AS sqlite-builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl ca-certificates \
    && curl --fail --location --retry 3 https://www.sqlite.org/2026/sqlite-autoconf-3510300.tar.gz -o sqlite.tar.gz \
    && tar -xzf sqlite.tar.gz --strip-components=1 \
    && python -c "import hashlib,pathlib; assert hashlib.sha3_256(pathlib.Path('sqlite3.c').read_bytes()).hexdigest() == '32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e'" \
    && ./configure --prefix=/usr/local --disable-static --disable-readline \
    && make -j2 && make install

FROM maven:3.9-eclipse-temurin-17 AS ofd-builder
WORKDIR /build
COPY print-converters/pom.xml print-converters/OfdPrint.java ./
RUN mvn -B -ntp dependency:copy-dependencies -DincludeScope=runtime -DoutputDirectory=/out/lib \
    && mkdir -p /out/classes \
    && javac -encoding UTF-8 -cp '/out/lib/*' -d /out/classes OfdPrint.java

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/home/app \
    PRINT_OFFICE_PYTHON=/usr/bin/python3 PRINT_OFD_CLASSPATH=/opt/ofd/classes:/opt/ofd/lib/*
WORKDIR /app
COPY --from=sqlite-builder /usr/local/lib/libsqlite3.so* /usr/local/lib/
RUN ldconfig && python -c "import sqlite3; assert sqlite3.sqlite_version == '3.51.3'"
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-calc libreoffice-impress python3-uno python3-seccomp \
    default-jre-headless fonts-wqy-zenhei fonts-noto-cjk fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ofd-builder /out/ /opt/ofd/
RUN useradd --create-home --uid 10001 app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN mkdir -p /data/db /data/files /data/config /home/app/.cache && chown -R app:app /app /data /home/app
USER app
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log"]
