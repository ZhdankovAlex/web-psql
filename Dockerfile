# One image for both the gateway and the worker; the worker needs psql, hence postgresql-client.
# Debian trixie ships postgresql-client 17 — the same version as the server.
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client less \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webpsql ./webpsql

# app     — runs the gateway;
# sandbox — the worker starts psql as this user: it cannot see the worker's environment.
RUN useradd --create-home --uid 1000 app \
 && useradd --create-home --uid 2000 --shell /bin/bash sandbox
USER app

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "webpsql.gateway:app", "--host", "0.0.0.0", "--port", "8000"]
