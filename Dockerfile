FROM python:3.11-slim

# ffmpeg + libsndfile para decodificar mp3/m4a/aiff/wav/flac
# libchromaprint-tools aporta `fpcalc` para la huella acústica (Fase 2, opcional).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .

# Worker en segundo plano (poller). No expone puertos.
CMD ["python", "-u", "worker.py"]
