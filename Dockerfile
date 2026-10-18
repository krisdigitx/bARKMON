FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements*.txt ./
RUN pip install -r requirements-worker.txt
COPY scripts/download_model.py /app/scripts/download_model.py
RUN python scripts/download_model.py /models/yamnet.tflite
COPY licenses /app/licenses
COPY barkmon ./barkmon
RUN useradd --uid 10001 --create-home barkmon && mkdir /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "barkmon.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
