FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev && rm -rf /var/lib/apt/lists/*

COPY requirements/analysis.txt ./requirements.txt
# Install CPU-only PyTorch (~250MB vs 1.5GB GPU build) — sufficient for sentence-transformers
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
# Install remaining requirements (torch already satisfied above)
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the sentence transformer model
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')"

COPY bharat_terminal/ ./bharat_terminal/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "bharat_terminal.analysis.main"]
