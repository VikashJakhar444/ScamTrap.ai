"""
ScamTrap AI - 1-Click Python Launcher & Service Manager
Cross-platform automated setup & launcher for FastAPI Backend & WhatsApp Bridge.
"""

import os
import sys
import time
import shutil
import subprocess
import webbrowser
from typing import Optional, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

IS_WINDOWS = sys.platform.startswith("win")
ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
os.chdir(ROOT_DIR)

VENV_DIR = os.path.join(ROOT_DIR, ".venv")
PYTHON_EXE = (
    os.path.join(VENV_DIR, "Scripts", "python.exe")
    if IS_WINDOWS
    else os.path.join(VENV_DIR, "bin", "python")
)
PIP_EXE = (
    os.path.join(VENV_DIR, "Scripts", "pip.exe")
    if IS_WINDOWS
    else os.path.join(VENV_DIR, "bin", "pip")
)


def print_banner():
    print("=" * 64)
    print("        🛡️  SCAMTRAP AI - ONE-CLICK LAUNCHER & MANAGER")
    print("=" * 64)
    print()


def check_and_setup_venv():
    """Checks and creates Python virtual environment and installs requirements."""
    if not os.path.exists(PYTHON_EXE):
        print("📦 [1/3] Setting up Python virtual environment (.venv)...")
        res = subprocess.run([sys.executable, "-m", "venv", ".venv"])
        if res.returncode != 0:
            print("❌ Failed to create virtual environment. Using system python.")
            return sys.executable

        print("📦 Installing Python dependencies from requirements.txt...")
        subprocess.run([PYTHON_EXE, "-m", "pip", "install", "--upgrade", "pip"], stdout=subprocess.DEVNULL)
        res_pip = subprocess.run([PYTHON_EXE, "-m", "pip", "install", "-r", "requirements.txt"])
        if res_pip.returncode != 0:
            print("⚠️ Some Python packages had warnings, continuing...")
        print("✅ Python environment ready.\n")
    else:
        print("✅ [1/3] Python environment ready.")

    return PYTHON_EXE


def check_and_setup_node():
    """Checks and installs Node.js packages for whatsapp-web.js."""
    node_modules_dir = os.path.join(ROOT_DIR, "node_modules", "whatsapp-web.js")
    if not os.path.isfile(os.path.join(node_modules_dir, "package.json")):
        print("📦 [2/3] Installing Node.js dependencies (whatsapp-web.js, qrcode-terminal)...")
        npm_cmd = "npm.cmd" if IS_WINDOWS else "npm"
        if not shutil.which(npm_cmd) and not shutil.which("npm"):
            print("⚠️ Node.js / npm not found in PATH! Please install Node.js from https://nodejs.org")
            return False
        if not shutil.which("node.exe" if IS_WINDOWS else "node") and not shutil.which("node"):
            print("⚠️ Node.js not found in PATH! Please install Node.js from https://nodejs.org")
            return False
        res = subprocess.run([npm_cmd, "install"], cwd=ROOT_DIR, shell=IS_WINDOWS)
        if res.returncode != 0:
            print("❌ Node.js dependency installation failed.")
            return False
        if not os.path.isfile(os.path.join(node_modules_dir, "package.json")):
            print("❌ Node.js dependency installation completed without whatsapp-web.js.")
            return False
        print("✅ Node.js dependencies installed.\n")
    else:
        print("✅ [2/3] Node.js dependencies ready.")

    return True


def free_port(port: int = 8000):
    """Ensures port is free by terminating stale lingering processes if necessary."""
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                print(f"⚠️ Port {port} is occupied by an earlier session. Cleaning up stale process...")
                if IS_WINDOWS:
                    out = subprocess.check_output(f'netstat -ano | findstr :{port}', shell=True, text=True, errors="ignore")
                    for line in out.strip().split("\n"):
                        if "LISTENING" in line.upper():
                            parts = line.strip().split()
                            if parts:
                                pid = parts[-1]
                                if pid.isdigit() and int(pid) != os.getpid():
                                    subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.run(f"fuser -k {port}/tcp", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1.2)
    except Exception:
        pass


def main():
    print_banner()

    # Step 1: Python venv & packages
    venv_python = check_and_setup_venv()

    # Step 2: Node.js packages
    if not check_and_setup_node():
        raise SystemExit(1)

    # Ensure port 8000 is clean
    free_port(8000)

    print("\n🚀 [3/3] Launching ScamTrap AI Services...")
    print("   • Starting FastAPI Backend Server on http://localhost:8000 ...")

    # Launch Backend Server
    backend_env = os.environ.copy()
    backend_proc = subprocess.Popen(
        [venv_python, "app.py"],
        cwd=ROOT_DIR,
        env=backend_env
    )

    # Allow backend port to bind
    time.sleep(2.5)

    # Launch WhatsApp Web Bridge
    print("   • Starting WhatsApp Web Bridge (wa_bridge.js)...")
    node_cmd = "node.exe" if IS_WINDOWS else "node"
    bridge_proc = subprocess.Popen(
        [node_cmd, "wa_bridge.js"],
        cwd=ROOT_DIR,
        env=backend_env
    )

    # Open Dashboard in Browser
    time.sleep(1.5)
    print("   • Opening SOC Dashboard in default browser...")
    webbrowser.open("http://localhost:8000")

    print("\n" + "=" * 64)
    print("  🎉 [SUCCESS] ScamTrap AI is actively running!")
    print("  👉 Scan the QR code displayed in the terminal using WhatsApp")
    print("     (WhatsApp -> Linked Devices -> Link a Device)")
    print("  👉 SOC Dashboard: http://localhost:8000")
    print("  👉 Press Ctrl+C at any time to safely stop all services.")
    print("=" * 64 + "\n")

    try:
        # Keep launcher alive and monitor processes
        while True:
            time.sleep(1)
            if backend_proc.poll() is not None:
                print("⚠️ Backend server exited. Shutting down...")
                break
            if bridge_proc.poll() is not None:
                print("⚠️ WhatsApp bridge exited. Shutting down...")
                break
    except KeyboardInterrupt:
        print("\n🛑 Stopping ScamTrap AI services...")
    finally:
        try:
            backend_proc.terminate()
        except Exception:
            pass
        try:
            bridge_proc.terminate()
        except Exception:
            pass
        print("✅ All services stopped safely. Bye!")


if __name__ == "__main__":
    main()
