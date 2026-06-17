FROM python:3.11-slim AS base

# System dependencies for alt-format generators:
#   - tesseract-ocr, ghostscript, qpdf, unpaper → ocrmypdf
#   - poppler-utils                             → pdf2image
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        ghostscript \
        poppler-utils \
        qpdf \
        unpaper \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir .

COPY connector/ ./connector/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "connector.main:app", "--host", "0.0.0.0", "--port", "8000"]
