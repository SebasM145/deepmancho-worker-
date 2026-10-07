FROM python:3.11-slim

# ffmpeg + libsndfile para decodificar mp3/m4a/aiff/wav/flac
# libchromaprint-tools aporta `fpcalc` para la huella acústica (Fase 2, opcional).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

# Menos arenas de malloc = menos fragmentacion: la RAM ociosa por replica bajaba
# poco solo con malloc_trim (v7.5.1).
ENV MALLOC_ARENA_MAX=2

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .
COPY grid_detect.py .
COPY analizador_v8.py .
COPY parecido.py .

# Worker en segundo plano (poller). No expone puertos.
CMD ["python", "-u", "worker.py"]
