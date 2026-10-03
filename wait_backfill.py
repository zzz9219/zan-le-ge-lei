"""Wait locally for the ASR stage; return once, without invoking AI."""
from contextlib import closing
import json
import sqlite3
import time
from library import DB, backfill_workflow


def wait(timeout=3600):
    deadline = time.monotonic() + timeout
    while True:
        with closing(sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)) as db:
            workflow = backfill_workflow(db)
        if workflow["phase"] != "transcribing":
            return workflow
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "waiting_for_transcription", **workflow}
        time.sleep(min(30, remaining))


if __name__ == "__main__":
    print(json.dumps(wait(), ensure_ascii=True), flush=True)
