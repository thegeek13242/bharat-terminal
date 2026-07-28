"""
Bharat Terminal API Gateway — FastAPI application.
Port 8000. Proxies KB service + streams WebSocket events.
"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from bharat_terminal.api.auth import get_current_user, require_internal_user
from bharat_terminal.api.kafka_relay import start_kafka_relay, stop_kafka_relay
from bharat_terminal.api.ltp_relay import start_ltp_relay, stop_ltp_relay
from bharat_terminal.api.sentiment_relay import (
    start_sentiment_relay,
    stop_sentiment_relay,
)
from bharat_terminal.api.db import close_engine
from bharat_terminal.api.routes import (
    auth,
    news,
    impact,
    company,
    companies,
    db_explorer,
    dossier,
    graph,
    watchlist,
    websocket,
    sentiment,
    signal_accuracy,
    signals_report,
    starred,
    internal_ltp,
    earnings_surprises,
    investor_holdings,
    transcripts,
    research_chat,
    papertrading,
)
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
    await start_sentiment_relay()
    await start_ltp_relay()
    yield
    # Shutdown
    await stop_kafka_relay()
    await stop_sentiment_relay()
    await stop_ltp_relay()
    await close_engine()
    logger.info("API shutdown complete")


app = FastAPI(
    title="Bharat Terminal API",
    version="1.0.0",
    description="Indian equity market intelligence terminal API",
    lifespan=lifespan,
)

app.add_middleware(GZipMiddleware, minimum_size=1000)

# CORS: cookie-based auth requires explicit origins + allow_credentials (no "*").
# Set CORS_ORIGINS (comma-separated) to the real dev + prod origins in production.
_cors_origins = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth router is public (login / session check / logout).
app.include_router(auth.router)

# Every data router requires an authenticated session (the WebSocket route
# authenticates from the session cookie on connect inside the handler).
_auth = [Depends(get_current_user)]
app.include_router(news.router, dependencies=_auth)
app.include_router(impact.router, dependencies=_auth)
app.include_router(company.router, dependencies=_auth)
app.include_router(companies.router, dependencies=_auth)
app.include_router(dossier.router, dependencies=_auth)
app.include_router(db_explorer.router, dependencies=_auth)
app.include_router(graph.router, dependencies=_auth)
app.include_router(watchlist.router, dependencies=_auth)
app.include_router(starred.router, dependencies=_auth)
app.include_router(sentiment.router, dependencies=_auth)
# Earnings-surprise statistics (PEAD): deterministic, factual-statistical
# payloads with disclaimer — customer-visible per founder decision 2026-07-04
# (flagged for RA counsel review; see routes/earnings_surprises.py docstring).
app.include_router(earnings_surprises.router, dependencies=_auth)
app.include_router(investor_holdings.router, dependencies=_auth)
app.include_router(investor_holdings.deals_router, dependencies=_auth)
# Concall transcripts: complete public filings, served unclipped.
app.include_router(transcripts.router, dependencies=_auth)
app.include_router(websocket.router)

# Internal research surfaces (signal reports + hit-rate metrics). These carry
# recommendation content and performance claims that must never reach customer
# accounts; gated by the INTERNAL_RESEARCH_EMAILS allowlist (empty = 403 all).
_internal = [Depends(require_internal_user)]
app.include_router(signal_accuracy.router, dependencies=_internal)
app.include_router(signals_report.router, dependencies=_internal)
# Angel One LTP: personal-use data — internal accounts only (licensing).
app.include_router(internal_ltp.router, dependencies=_internal)
# F12 research chat — dogfood phase, internal-only (see routes/research_chat.py).
app.include_router(research_chat.router, dependencies=_internal)
app.include_router(papertrading.router, dependencies=_internal)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "bharat-terminal-api",
        "timestamp": datetime.utcnow().isoformat(),
        "ws_connections": manager.active_connections,
    }
