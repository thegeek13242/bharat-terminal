"""WebSocket connection manager for real-time ImpactReport streaming."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections with sector-based filtering."""

    def __init__(self):
        self._connections: Dict[str, WebSocket] = {}  # client_id → websocket
        self._sector_subscriptions: Dict[str, Set[str]] = {}  # sector → {client_ids}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self._connections[client_id] = websocket
        logger.info(f"WS connected: {client_id} (total: {len(self._connections)})")

    def disconnect(self, client_id: str):
        self._connections.pop(client_id, None)
        for sector_clients in self._sector_subscriptions.values():
            sector_clients.discard(client_id)
        logger.info(f"WS disconnected: {client_id} (total: {len(self._connections)})")

    async def broadcast(self, message: dict):
        """Broadcast to all connected clients."""
        if not self._connections:
            return

        data = json.dumps(message, default=str)
        dead = []

        for client_id, ws in self._connections.items():
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(client_id)

        for client_id in dead:
            self.disconnect(client_id)

    async def broadcast_to_sector(self, sector: str, message: dict):
        """Broadcast to clients subscribed to a sector."""
        client_ids = self._sector_subscriptions.get(sector, set())
        if not client_ids:
            return await self.broadcast(message)

        data = json.dumps(message, default=str)
        dead = []

        for client_id in client_ids:
            ws = self._connections.get(client_id)
            if ws:
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(client_id)

        for client_id in dead:
            self.disconnect(client_id)

    @property
    def active_connections(self) -> int:
        return len(self._connections)


manager = ConnectionManager()
