"""Lightweight in-process scheduler (Priority 13).

No Celery/Kafka: a daemon thread polls the `schedules` table every
SCHEDULER_POLL_SECONDS and runs any enabled schedule whose
last_run_at + interval_hours <= now. Runs the same Pipeline and pushes
event-driven alerts. Enable with SCHEDULER_AUTOSTART=true (or start in code).

v4 fixes:
- UTC-safe due computation via calendar.timegm (#15) — time.mktime() would
  interpret the stored UTC timestamp in the machine's LOCAL timezone.
- per-schedule run lock (#16) so two polls/workers cannot double-launch a run;
  a production multi-worker deployment should still move to an external job
  system (documented in README).
"""
from __future__ import annotations

import calendar
import json
import logging
import threading
import time
from typing import Optional

from app.config import settings
from app.orchestrator import Pipeline
from app.output.alerts import send_event_alerts
from app.schemas import Competitor, CompetitorTarget
from app.storage.db import OfferStore

logger = logging.getLogger(__name__)


def utc_to_epoch(ts: str) -> float:
    """UTC-safe '%Y-%m-%dT%H:%M:%SZ' -> epoch seconds (review #15)."""
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


class Scheduler:
    def __init__(self, pipeline: Optional[Pipeline] = None, store: Optional[OfferStore] = None):
        self.pipeline = pipeline or Pipeline()
        self.store = store or self.pipeline.store
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._running: set = set()  # running schedule IDs (review #16)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="cintel-scheduler")
        self._thread.start()
        logger.info("scheduler started (poll %ss)", settings.scheduler_poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(settings.scheduler_poll_seconds):
            for sched in self.store.list_schedules():
                if not sched.get("enabled"):
                    continue
                sid = sched.get("id")
                with self._lock:
                    if sid in self._running:
                        continue
                    self._running.add(sid)
                try:
                    if self._due(sched):
                        self._run(sched)
                except Exception:
                    logger.exception("scheduled run failed for schedule %s", sid)
                finally:
                    with self._lock:
                        self._running.discard(sid)

    def _due(self, sched: dict) -> bool:
        last = sched.get("last_run_at")
        if not last:
            return True
        try:
            last_ts = utc_to_epoch(last)  # UTC-safe (#15)
        except ValueError:
            return True
        due_ts = last_ts + (sched.get("interval_hours", 24) or 24) * 3600
        return time.time() >= due_ts

    def _run(self, sched: dict) -> None:
        target = CompetitorTarget(
            company=sched["company"], website=sched["website"],
            regions=sched["regions"], focus_products=sched["focus_products"],
            competitors=[Competitor(**c) for c in sched.get("competitors", [])],
        )
        logger.info("scheduled run: %s", sched["company"])
        result = self.pipeline.run(target)
        events = [e for e in result.events or []]
        from app.schemas import EventAlert
        send_event_alerts(result, [EventAlert(**e) for e in events])
        self.store.set_schedule_last_run(sched["id"],
                                         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


_scheduler: Optional[Scheduler] = None


def get_scheduler(pipeline: Optional[Pipeline] = None) -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(pipeline)
    return _scheduler
