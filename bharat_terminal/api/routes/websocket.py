"""WebSocket endpoint for real-time ImpactReport streaming."""
import uuid
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from bharat_terminal.api.auth import user_from_session_cookie, INTERNAL_RESEARCH_EMAILS
from bharat_terminal.api import ltp_relay
from bharat_terminal.api.ws_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/feed")
async def websocket_feed(websocket: WebSocket):
    """
    Stream ImpactReport events to the client.

    All connected clients receive every relevant ImpactReport broadcast.
    The client sends periodic keepalive frames ({"action": "ping"} → {"type":
    "pong"}) so Cloudflare does not close the socket as idle. Sector filtering
    is NOT implemented server-side; the frontend filters locally.
    """
    # Authenticate from the session cookie sent during the handshake.
    user = user_from_session_cookie(websocket.cookies)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Internal accounts may additionally stream live LTP (Angel One data is
    # personal-use licensed — never available to customer accounts).
    is_internal = (user.get("email") or "").lower() in INTERNAL_RESEARCH_EMAILS

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
            # Listen for client messages (keepalive control).
            # Tolerate malformed frames: a non-JSON frame raises inside
            # receive_json — we log-and-continue rather than tearing down a
            # healthy socket over one bad frame.
            try:
                data = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                # Malformed / non-JSON frame — ignore and keep the socket open.
                continue

            if not isinstance(data, dict):
                continue

            action = data.get("action")
            if action == "ping":
                await websocket.send_json({"type": "pong"})

            elif action == "subscribe_ltp" and is_internal:
                sym = str(data.get("symbol") or "").upper().strip()
                if sym:
                    manager.subscribe_ltp(client_id, sym)
                    await ltp_relay.note_demand(sym)
                    # Instant snapshot while the first live tick arrives.
                    cached = await ltp_relay.cached_ltp(sym)
                    if cached:
                        await websocket.send_json({"type": "ltp", **cached})

            elif action == "unsubscribe_ltp":
                manager.unsubscribe_ltp(client_id)
            # Non-internal subscribe_ltp frames are silently ignored.

    except WebSocketDisconnect:
        pass
    finally:
        # Always clean up connection state, regardless of how the loop exits
        # (disconnect, send error on a dead socket, etc.).
        manager.disconnect(client_id)
