"""SSE streaming endpoint for real-time dashboard updates."""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from src.events.bus import event_bus

router = APIRouter(tags=["events"])


@router.get("/events")
async def event_stream():
    """Server-Sent Events endpoint. Clients receive live updates
    from both Telegram and Dashboard actions."""

    async def generate():
        yield "event: connected\ndata: {}\n\n"
        async for event in event_bus.subscribe():
            yield event.to_sse()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
