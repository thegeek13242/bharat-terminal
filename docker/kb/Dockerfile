FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements/kb.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bharat_terminal/ ./bharat_terminal/
COPY migrations/ ./migrations/
COPY alembic.ini ./

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "bharat_terminal.kb.api:app", "--host", "0.0.0.0", "--port", "8001"]
