FROM python:3.12-slim

RUN useradd --create-home --uid 1000 crawler

WORKDIR /work
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Headless Chromium for JS-rendered hosts (D68a), shared by every user, with its system libraries.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps --only-shell chromium && chmod -R a+rX /ms-playwright

# Tesseract OCR, English only (user, 2026-10-28), for PDF pages with no text layer (printed as outlines or scanned).
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1

USER crawler
