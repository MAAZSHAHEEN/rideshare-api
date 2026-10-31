"""One structured summary line per job run, plus the exit-code contract.

Every run of a maintenance job ends with exactly one line on stdout:

    job=expire_bookings status=ok expired=3 closed=1 duration_ms=42
    job=expire_bookings status=skipped duration_ms=5
    job=expire_bookings status=error error=ConnectionDoesNotExistError duration_ms=17

Exit codes (what a scheduler alerts on):
    0  status=ok or status=skipped (another runner holds the lock; not a failure)
    1  status=error (the job raised; work already committed per ride/batch stays
       committed, the rest is retried by the next run; the traceback is on stderr)
    2  bad command-line arguments (argparse)

Only the exception *class* goes on the summary line: messages can carry SQL
parameters or row values. A heartbeat table is deliberately not used here (it
would need a migration); see docs/release/SCHEDULER_AND_OPS.md.
"""

import time
import traceback
import sys

EXIT_OK = 0
EXIT_FAILED = 1


class Timer:
    def __init__(self) -> None:
        self.started = time.monotonic()

    def ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


def summary(job: str, status: str, timer: Timer, **fields) -> str:
    parts = [f"job={job}", f"status={status}"]
    parts += [f"{name}={value}" for name, value in fields.items()]
    parts.append(f"duration_ms={timer.ms()}")
    return " ".join(parts)


def report_failure(job: str, timer: Timer, exc: BaseException) -> int:
    traceback.print_exception(exc, file=sys.stderr)
    print(summary(job, "error", timer, error=type(exc).__name__), flush=True)
    return EXIT_FAILED
