FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev && rm -rf /var/lib/apt/lists/*

COPY requirements/analysis.txt ./requirements.txt
# Install CPU-only PyTorch (~250MB vs 1.5GB GPU build) — sufficient for sentence-transformers
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
# Install remaining requirements (torch already satisfied above)
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the sentence transformer model used for entity resolution
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

# Pre-download the zero-shot classifier used by Stage 1 (relevance filter).
# DeBERTa-v3-base zero-shot is ~750MB and runs ~300-500ms per item on 2vCPU.
# Free vs. the Anthropic Haiku fallback path. Cache lives in /root/.cache/
# huggingface; downstream RUNs use it offline.
RUN python -c "from transformers import pipeline; pipeline('zero-shot-classification', model='MoritzLaurer/deberta-v3-base-zeroshot-v2.0')"

COPY bharat_terminal/ ./bharat_terminal/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "bharat_terminal.analysis.main"]
