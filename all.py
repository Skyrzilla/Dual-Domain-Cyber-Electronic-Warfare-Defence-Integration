#!/usr/bin/env python3
# all.py - launcher for the EW demo

import subprocess
import sys
import time
import os

BASE_PY = sys.executable  # uses same python interpreter that runs this script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SCRIPTS = [
    ("simulator", "gnuradio_ewe_demo_gui.py"),
    ("dashboard", "web_ew_dashboard.py"),
    ("analyzer", "spectral_analyzer.py"),
    ("pox", "pox_controller.py"),
    ("key_client", "key_client.py"),
]

procs = []

def start(script):
    path = os.path.join(BASE_DIR, script)
    print(f"[launcher] starting {script}: ['{BASE_PY}', '{path}']")
    p = subprocess.Popen([BASE_PY, path], cwd=BASE_DIR)
    return p

def main():
    try:
        # Start simulator first (so its power & control servers appear quickly)
        procs.append(start("gnuradio_ewe_demo_gui.py"))
        time.sleep(3)

        # Start dashboard (it needs to bind 7000 server)
        procs.append(start("web_ew_dashboard.py"))
        time.sleep(1.0)

      

        # Start POX controller web (simple Flask app)
        procs.append(start("pox_controller.py"))
        time.sleep(0.3)

      
        print("[launcher] ✅ EW System online — open http://127.0.0.1:8060 in your browser.")
        print("[launcher] All started. Press Ctrl+C here to stop everything.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[launcher] shutting down...")
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass

if __name__ == "__main__":
    main()