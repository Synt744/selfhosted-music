ARG PYTHON_BASE_IMAGE=docker.io/library/python:3.12.9-alpine3.21
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY importer.py __init__.py __main__.py /app/ingest/

ENTRYPOINT ["python", "-m", "ingest"]
