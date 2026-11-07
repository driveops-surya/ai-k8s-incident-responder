import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from app.config import Settings
from app.pagerduty import verify_signature
from app.queue import JobQueue, Worker

logging.basicConfig(level=logging.INFO)
MAX_BODY_BYTES = 1024 * 1024
INCIDENT_ID = re.compile(r"^[A-Za-z0-9]+$")


@asynccontextmanager
async def lifespan(app):
    settings = Settings.from_env()
    app.state.settings = settings
    app.state.queue = JobQueue(settings.database_path, settings.max_attempts)
    worker = Worker(app.state.queue, settings)
    worker.start()
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="Kubernetes Incident Responder", lifespan=lifespan)


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/webhooks/pagerduty")
async def pagerduty_webhook(request: Request):
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not verify_signature(body, request.headers.get("X-PagerDuty-Signature", ""), request.app.state.settings.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(body)
        event = payload["event"]
        event_type = event["event_type"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")
    if event_type != "incident.triggered":
        return {"status": "ignored"}
    event_id = event.get("id")
    incident = event.get("data")
    incident_id = incident.get("id") if isinstance(incident, dict) else None
    if not isinstance(event_id, str) or not event_id or len(event_id) > 200:
        raise HTTPException(status_code=400, detail="Missing or invalid event ID")
    if not isinstance(incident_id, str) or not INCIDENT_ID.fullmatch(incident_id) or len(incident_id) > 64:
        raise HTTPException(status_code=400, detail="Missing or invalid incident ID")
    created = request.app.state.queue.enqueue(event)
    return {"status": "queued" if created else "duplicate"}
