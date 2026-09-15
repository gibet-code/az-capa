FROM python:3.12-slim-bookworm AS test

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY requirements.txt requirements-dev.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements-dev.txt

COPY . .
RUN python -m pytest -q

FROM test AS package

RUN sh tools/function-package/package.sh

FROM scratch AS export

COPY --from=package /out/ /