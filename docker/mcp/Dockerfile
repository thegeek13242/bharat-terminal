FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/mcp.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY bharat_terminal/ ./bharat_terminal/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV MCP_SERVER_PORT=8002

EXPOSE 8002

# Streamable-HTTP MCP server exposing get_financials / get_price_returns.
ENTRYPOINT ["python", "-m", "bharat_terminal.mcp_server.server"]
