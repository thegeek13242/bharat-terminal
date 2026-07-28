# Paper-trading service — mirrors docker/research/Dockerfile (needs the
# scientific stack because the materiality gate loads a scikit-learn joblib
# artifact at boot).
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/papertrade.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bharat_terminal/ ./bharat_terminal/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# The materiality artifact is NOT baked into the image: compose must mount
# ./exports:/app/exports (MATERIALITY_ARTIFACT defaults to
# exports/materiality_lab/materiality_v4_1_tax22.joblib). A missing artifact
# is survivable — the gate fails OPEN in paper mode, with provenance.
ENTRYPOINT ["python", "-m", "bharat_terminal.papertrade.main"]
