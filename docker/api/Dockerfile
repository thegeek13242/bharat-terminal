FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements/api.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bharat_terminal/ ./bharat_terminal/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "bharat_terminal.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
