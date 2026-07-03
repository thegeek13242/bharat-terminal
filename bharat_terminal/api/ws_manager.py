"""WebSocket connection manager for real-time ImpactReport streaming."""
import json
import logging
from typing import Dict, Set
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections with sector-based filtering."""

    def __init__(self):
        self._connections: Dict[str, WebSocket] = {}  # client_id → websocket
        self._sector_subscriptions: Dict[str, Set[str]] = {}  # sector → {client_ids}
        # Internal-only live LTP stream: client_id → symbol (one per client —
        # the assessment panel shows a single symbol at a time).
        self._ltp_subs: Dict[str, str] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self._connections[client_id] = websocket
        logger.info(f"WS connected: {client_id} (total: {len(self._connections)})")

    def disconnect(self, client_id: str):
        self._connections.pop(client_id, None)
        self._ltp_subs.pop(client_id, None)
        for sector_clients in self._sector_subscriptions.values():
            sector_clients.discard(client_id)
        logger.info(f"WS disconnected: {client_id} (total: {len(self._connections)})")

    # ── Internal-only LTP stream ─────────────────────────────────────────

    def subscribe_ltp(self, client_id: str, symbol: str):
        self._ltp_subs[client_id] = symbol.upper()

    def unsubscribe_ltp(self, client_id: str):
        self._ltp_subs.pop(client_id, None)

    def ltp_symbols(self) -> Set[str]:
        """Symbols with at least one live subscriber (for demand keepalive)."""
        return set(self._ltp_subs.values())

    async def send_ltp(self, symbol: str, message: dict):
        """Send an LTP tick to the clients subscribed to that symbol only —
        never broadcast (internal-gated, personal-use licensed data)."""
        symbol = symbol.upper()
        data = json.dumps(message, default=str)
        dead = []
        for client_id, sym in list(self._ltp_subs.items()):
            if sym != symbol:
                continue
            ws = self._connections.get(client_id)
            if ws is None:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(client_id)
        for client_id in dead:
            self.disconnect(client_id)

    async def broadcast(self, message: dict):
        """Broadcast to all connected clients."""
        if not self._connections:
            return

        data = json.dumps(message, default=str)
        dead = []

        # Snapshot the connection map: awaiting a send yields control and a
        # concurrent connect/disconnect could mutate the live dict mid-loop
        # ("dict changed size during iteration").
        for client_id, ws in list(self._connections.items()):
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

        # Snapshot to a list: sends await, so the subscription set / connection
        # map may be mutated concurrently mid-loop.
        for client_id in list(client_ids):
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
