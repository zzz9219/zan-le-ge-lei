"""Standalone web task; keep startup errors in a local log."""
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import traceback


if __name__ == "__main__":
    folder = Path(__file__).resolve().parent / "runtime"
    folder.mkdir(exist_ok=True)
    with (folder / "web-service.log").open("a", encoding="utf-8", buffering=1) as log:
        with redirect_stdout(log), redirect_stderr(log):
            try:
                from web import main
                main()
            except Exception:
                traceback.print_exc()
                raise SystemExit(1)
