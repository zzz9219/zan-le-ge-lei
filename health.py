"""Subprocess preflight contains native-library crashes and persists the result."""
import argparse
import json
import subprocess
import sys
from library import ROOT, RUNTIME, stats, now
from douyin_favorites_knowledge.core_bridge import atomic_write_json


def check(model="small"):
    code = "from douyin_favorites_knowledge.local_whisper import _local_model; _local_model(" + repr(model) + "); from faster_whisper.vad import get_vad_model; get_vad_model(); print('MODEL_READY')"
    try:
        probe = subprocess.run([sys.executable,"-u","-X","faulthandler","-c",code],cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=60)
        ready = probe.returncode == 0 and "MODEL_READY" in probe.stdout
        error = "" if ready else "model_preflight_failed; exit=" + str(probe.returncode)
    except subprocess.TimeoutExpired:
        ready,error = False,"model_preflight_timeout"
    state=stats()
    report={"checked_at":now(),"model":model,"asr_ready":ready,"baseline_complete":bool(state["settings"].get("baseline_complete")),"error":error}
    atomic_write_json(RUNTIME/"health.json",report)
    return report


if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--model",choices=["small","base"],default="small")
    report=check(p.parse_args().model)
    print(json.dumps(report,ensure_ascii=True))
    raise SystemExit(0 if report["asr_ready"] else 1)
