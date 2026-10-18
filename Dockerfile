FROM python:3.12-slim

RUN useradd --create-home --uid 1000 crawler

WORKDIR /work
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1

USER crawler
