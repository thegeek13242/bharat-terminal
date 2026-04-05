"""WebSocket endpoint for real-time ImpactReport streaming."""
import uuid
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from bharat_terminal.api.ws_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/feed")
async def websocket_feed(websocket: WebSocket):
    """
    Stream ImpactReport events to the client.
    Client can send JSON messages to filter by sector:
    {"action": "subscribe_sector", "sector": "BANKING"}
    """
    client_id = str(uuid.uuid4())
    await manager.connect(websocket, client_id)

    # Send connection acknowledgement
    await websocket.send_json({
        "type": "connected",
        "client_id": client_id,
        "message": "Connected to Bharat Terminal feed",
    })

    try:
        while True:
            # Listen for client messages (subscription control)
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "ping":
                await websocket.send_json({"type": "pong"})
            elif action == "subscribe_sector":
                sector = data.get("sector", "").upper()
                logger.info(f"Client {client_id} subscribed to sector {sector}")

    except WebSocketDisconnect:
        manager.disconnect(client_id)
