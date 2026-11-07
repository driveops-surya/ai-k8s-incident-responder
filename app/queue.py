import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from app.pipeline import process

logger = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, path, max_attempts):
        self.path = path
        self.max_attempts = max_attempts
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT
                )
            """)
            db.execute("UPDATE jobs SET status = 'pending' WHERE status = 'running'")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def enqueue(self, event):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO jobs (event_id, payload, status) VALUES (?, ?, 'pending')",
                (event["id"], json.dumps(event)),
            )
            return cursor.rowcount == 1

    def run_one(self, settings):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT event_id, payload, attempts FROM jobs "
                "WHERE status = 'pending' AND next_attempt <= ? ORDER BY rowid LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not row:
                return False
            event_id, payload, attempts = row
            db.execute("UPDATE jobs SET status = 'running' WHERE event_id = ?", (event_id,))
        try:
            process(settings, json.loads(payload))
        except Exception as exc:
            logger.exception("Incident job %s failed", event_id)
            attempts += 1
            status = "failed" if attempts >= self.max_attempts else "pending"
            delay = min(300, 2 ** min(attempts, 8))
            if status == "failed":
                logger.error("Incident job %s exhausted %s attempts", event_id, attempts)
            with self.connect() as db:
                db.execute(
                    "UPDATE jobs SET status = ?, attempts = ?, next_attempt = ?, last_error = ? WHERE event_id = ?",
                    (status, attempts, time.time() + delay, type(exc).__name__, event_id),
                )
        else:
            with self.connect() as db:
                db.execute("UPDATE jobs SET status = 'completed', last_error = NULL WHERE event_id = ?", (event_id,))
            logger.info("Incident job %s completed", event_id)
        return True


class Worker:
    def __init__(self, queue, settings):
        self.queue = queue
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="incident-worker", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def run(self):
        while not self.stop_event.is_set():
            try:
                processed = self.queue.run_one(self.settings)
            except Exception:
                logger.exception("Incident worker queue failure")
                processed = False
            if not processed:
                self.stop_event.wait(2)
