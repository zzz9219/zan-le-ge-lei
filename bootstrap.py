"""Install the Windows CPU environment; collection and timers are separate actions."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def run(args, cwd=ROOT):
    subprocess.run([str(part) for part in args], cwd=cwd, check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skip-model", action="store_true")
    p.add_argument("--comments", action="store_true")
    args = p.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        raise SystemExit("Please run with Python 3.12 on Windows: py -3.12 bootstrap.py")
    interpreter = ROOT / ".venv" / "Scripts" / "python.exe"
    if not interpreter.exists():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    run([interpreter, "-m", "pip", "install", "-r", "requirements-local.txt"])
    run([interpreter, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", "vendor/favorites"])
    choices = [Path(os.environ.get(key, "C:/Program Files")) / suffix for key, suffix in [
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe")]]
    if not any(path.exists() for path in choices):
        run([interpreter, "-m", "playwright", "install", "chromium"])
    if not args.skip_model:
        run([interpreter, "library.py", "prepare-model", "--model", "small"])
        run([interpreter, "health.py", "--model", "small"])
    if args.comments:
        run(["npm.cmd", "ci", "--omit=dev"], ROOT / "comments" / "douyin-upstream")
        run(["node", "scripts/setup-bridge.js"], ROOT / "comments")
    print("Installed. Run start.cmd to open the library; ask your local agent to continue SKILL.md.")


if __name__ == "__main__":
    main()
