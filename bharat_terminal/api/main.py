"""
Bharat Terminal API Gateway — FastAPI application.
Port 8000. Proxies KB service + streams WebSocket events.
"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from bharat_terminal.api.kafka_relay import start_kafka_relay, stop_kafka_relay
from bharat_terminal.api.db import close_engine
from bharat_terminal.api.routes import news, impact, company, graph, watchlist, websocket
from bharat_terminal.api.ws_manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting Bharat Terminal API...")
    await start_kafka_relay()
    yield
    # Shutdown
    await stop_kafka_relay()
    await close_engine()
    logger.info("API shutdown complete")


app = FastAPI(
    title="Bharat Terminal API",
    version="1.0.0",
    description="Indian equity market intelligence terminal API",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production: restrict to frontend origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(news.router)
app.include_router(impact.router)
app.include_router(company.router)
app.include_router(graph.router)
app.include_router(watchlist.router)
app.include_router(websocket.router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "bharat-terminal-api",
        "timestamp": datetime.utcnow().isoformat(),
        "ws_connections": manager.active_connections,
    }
