"""
Scheduler: persistent background job runner using APScheduler.

Jobs are stored in data/jobs.json and reloaded on startup. Each job is a
(source_id, frequency_minutes) tuple. The scheduler executes sync_source
in the background at the specified interval.
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .adls import ADLSClient
from .config import get_source
from .state import append_history, get_data_dir
from .sync import sync_source


JOBS_FILE = get_data_dir() / "jobs.json"


def _load_jobs() -> list[dict]:
    if not JOBS_FILE.exists():
        return []
    try:
        return json.loads(JOBS_FILE.read_text())
    except Exception:
        return []


def _save_jobs(jobs: list[dict]):
    JOBS_FILE.write_text(json.dumps(jobs, indent=2, default=str))


class JobScheduler:
    """Singleton scheduler that runs sync jobs in the background."""

    def __init__(self):
        self.scheduler = BackgroundScheduler(daemon=True)
        self._lock = threading.Lock()
        self._client: Optional[ADLSClient] = None
        self.scheduler.start()
        self._reload_jobs()

    def _get_client(self) -> ADLSClient:
        if self._client is None:
            self._client = ADLSClient()
            # Force connection
            self._client.test_connection()
        return self._client

    def _reload_jobs(self):
        """Load jobs from JSON and add them to APScheduler."""
        jobs = _load_jobs()
        for job in jobs:
            if job.get("active", True):
                self._add_to_scheduler(job)

    def _add_to_scheduler(self, job: dict):
        """Register a job with APScheduler."""
        try:
            self.scheduler.add_job(
                func=self._run_sync,
                trigger=IntervalTrigger(minutes=job["frequency_minutes"]),
                args=[job["source_id"]],
                id=job["job_id"],
                replace_existing=True,
                next_run_time=datetime.now(),  # run immediately, then on interval
            )
        except Exception as e:
            print(f"Failed to schedule {job.get('job_id')}: {e}")

    def _run_sync(self, source_id: str):
        """Background sync execution."""
        source = get_source(source_id)
        if source is None:
            return
        client = self._get_client()
        result = sync_source(client, source)
        append_history({
            "timestamp": datetime.now().isoformat(),
            "source_id": source_id,
            "source_name": source["name"],
            "trigger": "scheduled",
            "total": result["total"],
            "uploaded": result["uploaded"],
            "failed": result["failed"],
            "elapsed_seconds": result["elapsed_seconds"],
        })

    # ───────────────────────── public API ───────────────────────────────

    def list_jobs(self) -> list[dict]:
        """Return jobs with next run time enriched from APScheduler."""
        jobs = _load_jobs()
        for job in jobs:
            sj = self.scheduler.get_job(job["job_id"])
            job["next_run"] = sj.next_run_time.isoformat() if sj and sj.next_run_time else None
        return jobs

    def add_job(self, source_id: str, frequency_minutes: int) -> dict:
        """Add a new scheduled job."""
        with self._lock:
            jobs = _load_jobs()
            job_id = f"{source_id}_{int(time.time())}"
            new_job = {
                "job_id": job_id,
                "source_id": source_id,
                "frequency_minutes": frequency_minutes,
                "active": True,
                "created_at": datetime.now().isoformat(),
            }
            jobs.append(new_job)
            _save_jobs(jobs)
            self._add_to_scheduler(new_job)
            return new_job

    def remove_job(self, job_id: str):
        """Remove a scheduled job."""
        with self._lock:
            jobs = _load_jobs()
            jobs = [j for j in jobs if j["job_id"] != job_id]
            _save_jobs(jobs)
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass

    def toggle_job(self, job_id: str, active: bool):
        """Pause or resume a job."""
        with self._lock:
            jobs = _load_jobs()
            for j in jobs:
                if j["job_id"] == job_id:
                    j["active"] = active
            _save_jobs(jobs)
            if active:
                for j in jobs:
                    if j["job_id"] == job_id:
                        self._add_to_scheduler(j)
            else:
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass
