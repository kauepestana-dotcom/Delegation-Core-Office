"""
jobs.py — In-process background job store.

Any module can submit a blocking function as a daemon thread and get a job_id
back immediately. The server polls status via task_status().

Job IDs are in-memory only — they do not survive a server restart.

Completed run times ARE persisted, per task name, so task_status() can tell a
caller how long this kind of job usually takes. A calling agent otherwise has
only elapsed_seconds to go on and has no way to distinguish "started 20s ago,
will take 8 minutes" from "nearly done" — in practice that means polling every
30s through a 7-minute build, or abandoning the tool and watching the output
directory from a shell instead.
"""

import json
import logging
import os
import statistics
import threading
import uuid

from . import keepawake
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("jobs")

_jobs: dict = {}
_lock = threading.Lock()

# When this process's job store came up. A job_id that is absent is either a
# typo or a casualty of a restart, and those want opposite responses from the
# caller — this is the one fact that separates them: submit a job, get an id
# back, and if the store is younger than the id you are holding, the daemon
# restarted underneath you and the work's outcome is unknown, not failed.
STARTED_AT = datetime.now()

# Rolling history of completed durations per task name. Small and advisory —
# a corrupt or missing file just means callers get no hint, never an error.
_DURATIONS_PATH = Path.home() / ".delegation_core" / "job_durations.json"
_DURATIONS_KEEP = 10

# The durations file is read-modify-written by whichever job thread happens to
# finish, and jobs.submit hands every task its own daemon thread. Two finishing
# together both read the old file, both append their own entry, and the second
# write erases the first — the classic lost update, on a file whose whole job is
# to accumulate a history.
#
# `_lock` above guards the in-memory `_jobs` dict and nothing else, so it is not
# this lock. A separate one keeps the two concerns apart: a slow disk write must
# not block a status poll.
#
# localqueue.py, which stores the same kind of thing, already does both halves
# of this correctly (an RLock plus tmp+fsync+os.replace). This module was the
# one JSON store in the project with neither.
_durations_lock = threading.Lock()


def _load_durations() -> dict:
    try:
        data = json.loads(_DURATIONS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        # Advisory data: a corrupt file must not raise. But it must not vanish
        # silently either — before this, a truncated write turned into "no
        # history" for every task at once, and the only symptom was task_status
        # quietly dropping check_again_in_seconds from its answers.
        logger.warning("job duration history unreadable (%s) — starting empty", e)
        return {}
    return data if isinstance(data, dict) else {}


def _record_duration(task_name: str, seconds: float) -> None:
    try:
        with _durations_lock:
            data = _load_durations()
            history = [s for s in data.get(task_name, []) if isinstance(s, (int, float))]
            history.append(round(seconds, 1))
            data[task_name] = history[-_DURATIONS_KEEP:]
            _DURATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Atomic, for the same reason localqueue is: write_text truncates
            # the real file first, so a crash or a full disk mid-write leaves a
            # half-written store that is indistinguishable from a corrupt one,
            # and _load_durations then reads it as "no history at all".
            tmp = _DURATIONS_PATH.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, _DURATIONS_PATH)
    except Exception as e:  # advisory only — never fail a job over telemetry
        logger.debug("Could not record duration for %s: %s", task_name, e)


def typical_seconds(task_name: str) -> float | None:
    """Median run time of this task's last completed runs, or None if unknown."""
    history = _load_durations().get(task_name) or []
    history = [s for s in history if isinstance(s, (int, float))]
    return round(statistics.median(history), 1) if history else None


def submit(task_name: str, fn, *args, **kwargs) -> str:
    """Run fn(*args, **kwargs) in a daemon thread. Returns a job_id immediately."""
    job_id = uuid.uuid4().hex[:8]
    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "task": task_name,
            "status": "running",
            "started": datetime.now().isoformat(),
            "finished": None,
            "result": None,
            "error": None,
        }

    # Pedido ANTES de a thread comecar: se fosse dentro de _worker haveria uma
    # janela entre submit() retornar e a thread ser escalonada em que a maquina
    # ainda poderia suspender. keepawake conta referencias, entao jobs
    # simultaneos nao soltam o bloqueio uns dos outros.
    keepawake.acquire()

    def _worker():
        started_at = datetime.now()
        try:
            result = fn(*args, **kwargs)
            update = {"status": "done", "result": result, "finished": datetime.now().isoformat()}
        except Exception as e:
            logger.error("Background job %s (%s) failed: %s", job_id, task_name, e)
            update = {"status": "error", "error": str(e), "finished": datetime.now().isoformat()}
        # Recorded before the status flip so that a caller observing "done"
        # always sees the run already reflected in typical_seconds(). Only
        # successful runs shape the estimate: a job that raised after 2s says
        # nothing about how long the work actually takes.
        if update["status"] == "done":
            _record_duration(task_name, (datetime.now() - started_at).total_seconds())
        with _lock:
            _jobs[job_id].update(update)
        # finally nao: o try acima ja captura tudo e sempre define `update`.
        # Um release aqui, apos a escrita do status, garante que quem observa
        # "done" nunca ve o bloqueio ainda pendurado.
        keepawake.release()

    threading.Thread(target=_worker, daemon=True, name=f"job-{job_id}").start()
    logger.info("Submitted background job %s: %s", job_id, task_name)
    return job_id


def get(job_id: str) -> dict | None:
    """Return a snapshot of a job dict, or None if not found."""
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def running_count() -> int:
    """Return the number of currently running background jobs."""
    with _lock:
        return sum(1 for j in _jobs.values() if j["status"] == "running")
