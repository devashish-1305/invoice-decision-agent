"""GET /api/runs/{id}/stream: Server-Sent Events read from the stage_events table.

Because events come from the database, a client that connects late (or reconnects) still gets everything.
  - every event carries its database id as the SSE "id:" (monotonically increasing);
  - a reconnecting client sends Last-Event-ID (EventSource does this itself) or ?after=<id> and receives only
    events with a larger id: no duplicates, no gaps;
  - the stream ends with an "end" event, and only after the final stage event has been delivered. We read the
    run status BEFORE the events: the orchestrator sets the final status after its last event is committed, so
    a final status means every event is already in the table.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from app.api.deps import AppState, get_state
from app.api.views import FINAL_STATUSES, iso
from app.db import models as m
from sqlalchemy import select

router = APIRouter()
POLL_SECONDS = 0.25


def read_new_events(state: AppState, run_id: int, after: int):
    with state.read_session_factory() as session:
        run = session.get(m.Run, run_id)
        status, decision = run.status, run.decision          # status FIRST (see module docstring)
        events = list(session.scalars(select(m.StageEvent).where(
            m.StageEvent.run_id == run_id, m.StageEvent.id > after).order_by(m.StageEvent.id)))
        payloads = [{"id": e.id, "run_id": e.run_id, "stage": e.stage, "status": e.status, "output": e.output,
                     "error": e.error, "duration_ms": e.duration_ms, "created_at": iso(e.created_at)} for e in events]
    return status, decision, payloads


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: int, state: AppState = Depends(get_state),
                     last_event_id: str | None = Header(default=None),
                     after: int | None = Query(default=None)):
    with state.read_session_factory() as session:
        if session.get(m.Run, run_id) is None:
            raise HTTPException(404, "Run not found")
    try:
        last = int(last_event_id) if last_event_id else (after or 0)
    except ValueError:
        last = after or 0

    async def events():
        nonlocal last
        while True:
            status, decision, payloads = await asyncio.to_thread(read_new_events, state, run_id, last)
            for p in payloads:
                last = p["id"]
                yield {"id": str(p["id"]), "event": "stage", "data": json.dumps(p)}
            if status in FINAL_STATUSES:
                yield {"event": "end", "data": json.dumps({"run_id": run_id, "status": status, "decision": decision})}
                return
            await asyncio.sleep(POLL_SECONDS)

    return EventSourceResponse(events())
