FROM python:3.12-slim

WORKDIR /app

COPY requirements/sentiment.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bharat_terminal/ ./bharat_terminal/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "bharat_terminal.sentiment.main"]
