# One image for both the nightly ingest CronJob and the MCP server.
# Behind Nexus: build with --build-arg PIP_INDEX_URL=https://nexus.example/repository/pypi-proxy/simple
# and point the base image at your Docker proxy (e.g. nexus.example:5000/python:3.12-slim).
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 KG_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 kg && mkdir -p /data && chown kg /data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

USER kg
EXPOSE 8000
ENTRYPOINT ["kg"]
CMD ["serve"]
