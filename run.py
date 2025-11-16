import multiprocessing
import subprocess
import sys
import os  # <-- THIS IS REQUIRED for os.name and os.geteuid
import time
import ctypes  # <-- THIS IS REQUIRED for ctypes.windll
import threading
import atexit

# Import the detector's main function
from detector import start_detector
# Import the dashboard's app, ip finder, AND the data generator
from dashboard import app as dash_app, get_wifi_ipv4, fallback_sensor_data, DATA_PATH
# Import the TUI's main function
from firewall_web_report import main as start_dashboard

def run_detector_process():
    """Target function for the detector process."""
    print("[Orchestrator] Starting Detector process in this terminal...")
    start_detector()

def launch_tui_window():
    """Launches the TUI dashboard in a new command window."""
    print("[Orchestrator] Launching TUI Dashboard in new window...")
    
    python_exe = sys.executable 
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tui_script = os.path.join(script_dir, "firewall_web_report.py")
    
    subprocess.Popen(
        [python_exe, tui_script],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

def start_ml_scripts():
    """
    Launches all background ML scripts in the correct order.
    It WAITS for training to finish before starting the others.
    """
    try:
        # Step 1: Run the training script and WAIT for it to finish.
        print("[Orchestrator] Running model training... This may take a moment.")

        train_process = subprocess.run(
            ["python", "E:\\FactoryDigitalTwin\\ML_Model\\train_model.py"],
            capture_output=True, text=True 
        )

        if train_process.returncode != 0:
            print("="*30 + " ML TRAINING FAILED " + "="*30)
            print(train_process.stderr)
            return
        else:
            print("[Orchestrator] Model training complete.")

        # Step 2: Launch the LIVE scripts.
        # We are NOT running accuracy.py to prevent file conflicts.
        print("[Orchestrator] Launching data generator and prediction scripts...")
        subprocess.Popen(["python", "E:\\FactoryDigitalTwin\\scripts\\generate_data.py"])
        subprocess.Popen(["python", "E:\\FactoryDigitalTwin\\ML_Model\\predict_failure.py"])

    except Exception as e:
        print(f"[Orchestrator] Error launching background scripts: {e}")

def check_admin_privileges():
    """Checks if the script is running as Administrator/root."""
    try:
        if os.name == 'nt':
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        else:
            return os.geteuid() == 0
    except Exception:
        return False

def cleanup_data():
    """Deletes the sensor data file on exit."""
    if os.path.exists(DATA_PATH):
        print(f"[Orchestrator] Cleaning up {DATA_PATH}...")
        os.remove(DATA_PATH)

if __name__ == "__main__":
    if not check_admin_privileges():
        print("=" * 50)
        print("⛔ ERROR: INSUFFICIENT PRIVILEGES ⛔")
        print("This IDS must be run as Administrator (Windows) or root (Linux)")
        print("=" * 50)
        sys.exit(1)
    else:
        print("✅ Running with Administrator / root privileges.")

    # 1. Register the cleanup function
    atexit.register(cleanup_data)

    # 2. Start the background ML scripts
    start_ml_scripts()

    # 3. Start the fallback data generator thread
    print("[Orchestrator] Starting fallback data generator thread...")
    threading.Thread(target=fallback_sensor_data, daemon=True).start()

    # 4. Start the Detector in a background process
        # --- FIX: ensure access.log exists before detector starts ---
    access_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "access.log")
    for _ in range(5):
        if os.path.exists(access_log) and os.path.getsize(access_log) > 0:
            break
        print("[Orchestrator] Waiting for access.log to initialize...")
        time.sleep(1)
    # Now start detector
    p_detector = multiprocessing.Process(target=run_detector_process)
    p_detector.start()


    # 5. Launch the TUI in its new, separate window
    launch_tui_window()

    print("[Orchestrator] Waiting 2 seconds for services to launch...")
    time.sleep(2)

    # 6. Run the Dashboard in the main thread
    print("[Orchestrator] Starting Dashboard in this terminal...")
    ip = get_wifi_ipv4() or "127.0.0.1"
    
    print(f"--- Starting Dash Server on http://{ip}:8051 ---")
    
    try:
        # Run on port 8051
        dash_app.run(host=ip, port=8051, debug=False)
        
    except Exception as e:
        if "access permissions" in str(e):
            print("\n" + "="*50)
            print("⛔ ERROR: PORT 8051 IS ALREADY IN USE. ⛔") # <-- FIXED
            print("Another application is using port 8051.") # <-- FIXED
            print("Please close the other program or restart your computer.")
            print(f"Error details: {e}")
            print("="*50)
        else:
            raise e # Re-raise other errors 
    
    # When you press Ctrl+C in this terminal, the dashboard stops
    print("\n[Orchestrator] Dashboard stopped. Cleaning up detector process...")
    if p_detector.is_alive():
        p_detector.terminate()
        p_detector.join()
    print("All processes terminated.")
