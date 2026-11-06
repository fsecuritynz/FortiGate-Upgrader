"""Background jobs with a log, cancellation and human approval gates."""
from __future__ import annotations

import datetime as dt
import threading
import time
import traceback
import uuid


class Cancelled(Exception):
    pass


_local = threading.local()


def current() -> "Job | None":
    """The job running on this thread, if any (used to stream API/SSH activity into its log)."""
    return getattr(_local, "job", None)


class Job:
    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.title = title
        self.status = "running"
        self.log: list[dict] = []
        self.result = None
        self.error = None
        self.created = dt.datetime.now().isoformat(timespec="seconds")
        self._cancel = threading.Event()
        self._approval = threading.Event()
        self._approved = False
        self.prompt: str | None = None
        self.state: dict = {}
        self.stage = "Starting"
        self.parent: str | None = None
        self.children: list[str] = []
        self.target_id: str | None = None
        self.target_ids: list[str] = []
        self.meta: dict = {}
        self.finished: str | None = None
        self.label: str | None = None  # short name shown in log streams (defaults to the title)
        self.probes = None  # ProbeSession while running; its summary dict once finished

    # ------------------------------------------------------------ logging
    def say(self, msg: str, level: str = "info", src: str | None = None):
        e = {"t": dt.datetime.now().strftime("%H:%M:%S.%f")[:-3], "ts": int(time.time() * 1000), "level": level,
             "msg": msg, "src": src or self.label or self.title}
        self.log.append(e)
        if self.parent and self.parent in JOBS:  # batch jobs show every device's activity in one stream
            JOBS[self.parent].log.append(e)

    def detail(self, msg: str, src: str | None = None):
        self.say(msg, "detail", src)

    def set_stage(self, stage: str):
        self.stage = stage
        self.say(f"— {stage} —", "stage")

    def check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    def cancel(self):
        self._cancel.set()
        self._approved = False
        self._approval.set()

    # ----------------------------------------------------------- approvals
    def wait_approval(self, prompt: str, waiting_msg: str | None = None, quiet: bool = False):
        self.prompt = prompt
        self.status = "waiting"
        prev_stage, self.stage = self.stage, "Awaiting approval" if not quiet else "Waiting for the other devices"
        self.say(waiting_msg or f"Waiting for approval: {prompt}", "gate" if not quiet else "info")
        self._approval.clear()
        self._approval.wait()
        self.prompt = None
        self.check_cancel()
        if not self._approved:
            raise Cancelled()
        self.status = "running"
        self.stage = prev_stage
        self.say("Continuing" if quiet else "Approved, continuing", "info" if quiet else "gate")

    def answer(self, approve: bool):
        self._approved = approve
        if not approve:
            self._cancel.set()
        self._approval.set()

    def to_dict(self, since: int = 0):
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                "stage": self.stage, "parent": self.parent, "children": self.children, "target_id": self.target_id,
                "target_ids": self.target_ids or ([self.target_id] if self.target_id else []), "meta": self.meta,
                "created": self.created, "finished": self.finished, "log": self.log[since:], "log_len": len(self.log),
                "prompt": self.prompt, "result": self.result, "error": self.error,
                "probes": self.probes_summary()}

    def probes_summary(self):
        p = self.probes
        if p is None or isinstance(p, dict):
            return p
        try:
            return p.summary()
        except Exception:  # noqa: BLE001
            return None


JOBS: dict[str, Job] = {}


def persist(job: Job):
    try:
        from . import storage
        storage.save_job(job.to_dict())
    except Exception:  # noqa: BLE001
        pass  # history is best effort


def start(kind: str, title: str, fn, *args, parent: Job | None = None, target_id: str | None = None,
          target_ids: list[str] | None = None, meta: dict | None = None, **kw) -> Job:
    job = Job(kind, title)
    job.target_id = target_id
    job.target_ids = list(target_ids or ([target_id] if target_id else []))
    job.meta = dict(meta or {})
    if parent:
        job.parent = parent.id
        parent.children.append(job.id)
    JOBS[job.id] = job
    persist(job)

    def run():
        _local.job = job
        try:
            job.result = fn(job, *args, **kw)
            job.status = "done"
            job.stage = "Complete"
            job.say("Finished", "ok")
        except Cancelled:
            job.status = "cancelled"
            job.stage = f"Cancelled during: {job.stage}" if job.stage not in ("Starting", "Complete") else "Cancelled"
            job.say("Cancelled", "warn")
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.stage = f"Failed during: {job.stage}" if job.stage not in ("Starting", "Complete") else "Failed"
            job.error = str(e)
            job.say(f"FAILED: {e}", "error")
            job.say(traceback.format_exc(), "debug")
        finally:
            job.finished = dt.datetime.now().isoformat(timespec="seconds")
            persist(job)

    threading.Thread(target=run, daemon=True, name=f"job-{job.id}").start()
    return job
