"""Frontend activity beacon — page views + visibility heartbeats.

Fed by frontend/src/lib/beacon.ts: a page_view on every SPA route change and
a heartbeat every 60s while the tab is visible. Together with the API request
log these power time-on-platform (sessionized with a 5-min gap rule) and
per-page dwell in /admin/analytics.

Mounted with the plain _auth tier and deliberately EXEMPT from per-user
feature gates — analytics must keep working for a user whose features are
restricted. The path is in request_log.SKIP_PREFIXES so beacon POSTs are not
double-counted as API activity.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from bharat_terminal.api.auth import get_current_user
from bharat_terminal.api.request_log import record_event

router = APIRouter(prefix="/events", tags=["events"])

_TYPES = ("page_view", "heartbeat")


class BeaconBody(BaseModel):
    type: str
    path: str = ""


@router.post("/beacon")
async def beacon(body: BeaconBody, user: dict = Depends(get_current_user)):
    if body.type not in _TYPES:
        raise HTTPException(status_code=422, detail="bad_event_type")
    record_event(user, body.type, body.path[:200])
    return {"ok": True}
