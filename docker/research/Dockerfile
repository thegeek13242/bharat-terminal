FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/research.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bharat_terminal/ ./bharat_terminal/
COPY migrations/ ./migrations/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Default: just print help. Override via `docker compose run research-runner seed`
# or `... run --symbols=… --triggers=…`.
ENTRYPOINT ["python", "-m", "bharat_terminal.research.main"]
