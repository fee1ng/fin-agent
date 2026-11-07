import asyncio
import json
import time
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from fin_agent.web.models import User
from fin_agent.web.runs import Runs, TERMINAL


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=8000)


def install_chat_routes(app):
    runs = Runs(app.state.database, app.state.settings)
    actor = Annotated[User, Depends(app.state.current_user)]

    @app.post('/api/v1/sessions/{session_id}/runs', status_code=202)
    def submit(session_id: str, body: Question, user: actor,
               idempotency_key: Annotated[str, Header(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')]):
        return runs.submit(session_id, user.id, body.text, idempotency_key)

    @app.post('/api/v1/runs/{run_id}/retry', status_code=202)
    def retry(run_id: str, user: actor,
              idempotency_key: Annotated[str, Header(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')]):
        return runs.retry(run_id, user.id, idempotency_key)

    @app.get('/api/v1/sessions/{session_id}/messages')
    def messages(session_id: str, user: actor):
        return runs.snapshot(session_id, user.id)

    @app.get('/api/v1/runs/{run_id}')
    def get_run(run_id: str, user: actor):
        return runs.get(run_id, user.id)

    @app.get('/api/v1/runs/{run_id}/events')
    async def events(run_id: str, request: Request, user: actor, after: int = 0):
        try:
            cursor = int(request.headers.get('last-event-id', str(after)))
        except ValueError:
            raise HTTPException(422, '事件游标无效')
        if cursor < 0:
            raise HTTPException(422, '事件游标无效')
        await asyncio.to_thread(runs.get, run_id, user.id)

        async def stream():
            nonlocal cursor
            last_ping = time.monotonic()
            last_executor_check = 0
            while not await request.is_disconnected():
                try:
                    # Re-check expiration, revocation and disabled accounts during streams.
                    await asyncio.to_thread(app.state.current_user, request)
                    page, snapshot = await asyncio.to_thread(runs.events, run_id, user.id, cursor)
                except HTTPException as exc:
                    event = 'auth.expired' if exc.status_code in (401, 403) else 'stream.reset'
                    yield f'event: {event}\ndata: {{}}\n\n'
                    return
                if time.monotonic() - last_executor_check > 5:
                    executor = await asyncio.to_thread(runs.executor_status)
                    yield f'event: executor.status\ndata: {json.dumps(executor)}\n\n'
                    last_executor_check = time.monotonic()
                if cursor > snapshot['last_seq'] or (page and page[0]['seq'] != cursor + 1) or (not page and cursor < snapshot['last_seq']):
                    yield 'event: stream.reset\ndata: {}\n\n'
                    return
                for event in page:
                    cursor = event['seq']
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if snapshot['status'] in TERMINAL and cursor >= snapshot['last_seq']:
                    return
                if time.monotonic() - last_ping >= 15:
                    yield ': keepalive\n\n'
                    last_ping = time.monotonic()
                await asyncio.sleep(.15)

        return StreamingResponse(stream(), media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})
