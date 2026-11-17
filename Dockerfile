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

# Analizador v2 del «1» (7.6.25): torch de CPU + Beat This! y sus pesos `small0` (8 MB),
# comprobados por sha256. Si los pesos no bajan o no son los esperados, la construcción FALLA
# (Railway se queda con la imagen anterior). El worker solo lo calcula cuando la plataforma lo
# pide (`analisis_config.sombra_v2`); si no, ni importa torch. Va en su propia capa, antes del
# código, para que un cambio de worker.py no la vuelva a bajar.
ENV TORCH_HOME=/opt/torch
COPY requirements-v2.txt .
RUN pip install --no-cache-dir -r requirements-v2.txt
COPY rejilla_v2.py .
RUN python -c "import rejilla_v2; rejilla_v2.bajar_pesos()"

COPY worker.py .
COPY grid_detect.py .
COPY analizador_v8.py .
COPY parecido.py .

# Worker en segundo plano (poller). No expone puertos.
CMD ["python", "-u", "worker.py"]
