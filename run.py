"""Crash-restart wrapper used by the scheduled task: python run.py [--dry-run]"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "pythonw.exe"
args = [str(PY), "-m", "givvy.main", "run", *sys.argv[1:]]
backoff = 10
while True:
    started = time.time()
    rc = subprocess.call(args, cwd=str(ROOT))
    if rc == 0:
        break
    if time.time() - started > 600:
        backoff = 10
    time.sleep(backoff)
    backoff = min(backoff * 2, 300)
