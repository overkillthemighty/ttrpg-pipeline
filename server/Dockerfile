# TTRPG transcription server
#   CPU build (default):   docker compose build
#   NVIDIA GPU build:      docker compose -f docker-compose.yml -f docker-compose.gpu.yml build
FROM python:3.12-slim

ARG TORCH_VARIANT=cpu
ARG TORCH_VERSION=2.8.0
ARG WHISPERX_VERSION=3.8.6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HF_HOME=/data/models/huggingface \
    TORCH_HOME=/data/models/torch \
    NLTK_DATA=/data/models/nltk \
    XDG_CACHE_HOME=/data/models/cache

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Torch first, from the matching wheel index, so whisperx doesn't drag in the wrong build.
RUN pip install torch==${TORCH_VERSION} torchaudio==${TORCH_VERSION} \
      --index-url https://download.pytorch.org/whl/${TORCH_VARIANT} \
 && pip install whisperx==${WHISPERX_VERSION} torch==${TORCH_VERSION} torchaudio==${TORCH_VERSION} \
      --extra-index-url https://download.pytorch.org/whl/${TORCH_VARIANT} \
 && python -c "import torch, whisperx; print('torch', torch.__version__)"

# CTranslate2 (used by faster-whisper) looks for cuDNN/cuBLAS on the library path; the pip
# CUDA wheels put them in site-packages. Harmless on CPU builds.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY client ./client
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=4).status==200 else 1)"

# Starts as root only to fix /data ownership, then runs as PUID:PGID (default 1000:1000).
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
# One worker process only: the job queue lives inside it.
# Only trust X-Forwarded-For from these proxy addresses (set FORWARDED_ALLOW_IPS to your NPM host/network).
ENV FORWARDED_ALLOW_IPS="127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\" --timeout-keep-alive 75 --timeout-graceful-shutdown 20"]
