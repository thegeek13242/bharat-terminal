FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/ingestion.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bharat_terminal/ ./bharat_terminal/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "bharat_terminal.ingestion.main"]
