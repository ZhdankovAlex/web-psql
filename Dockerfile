# Один образ для gateway и worker; worker'у нужен psql, поэтому ставим postgresql-client.
# Debian trixie: в нём postgresql-client 17 — та же версия, что и у сервера.
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client less \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webpsql ./webpsql

# app     — от его имени работает gateway;
# sandbox — от его имени worker запускает psql: у него нет доступа к окружению worker'а.
RUN useradd --create-home --uid 1000 app \
 && useradd --create-home --uid 2000 --shell /bin/bash sandbox
USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "webpsql.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
