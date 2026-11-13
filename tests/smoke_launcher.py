"""Exercise the launcher against real Linux processes, HTTP and occupied ports."""
import fcntl
import http.client
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


with tempfile.TemporaryDirectory(prefix="hwmonitor launcher ") as directory:
    app = Path(directory)
    shutil.copy(ROOT / "start.sh", app)
    shutil.copy(ROOT / "pyproject.toml", app)
    venv = app / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    shutil.copy(app / "pyproject.toml", venv / ".hwmonitor-pyproject.toml")
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
               HARDWARE_MONITOR_DATA_DIR=str(app / "state with spaces"), DISPLAY="")
    state = app / "data/launcher"
    pid_file = state / "hwmonitor.pid"

    def run(*args, expected=0):
        result = subprocess.run(["sh", str(app / "start.sh"), *args], env=env,
                                cwd="/", text=True, capture_output=True, timeout=45)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
        return result.stdout

    def request(port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET", "/api/state")
            response = conn.getresponse()
            assert response.status == 200, response.status
            response.read()
        finally:
            conn.close()

    # A stale PID must never cause an unrelated Python process to be signalled.
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(180)"], cwd=app)
    try:
        run("status", expected=1)
        pid_file.write_text(str(unrelated.pid) + "\n")
        run("stop")
        assert unrelated.poll() is None
        with (state / "control.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            run("start", expected=1)
        run("--check")
        assert not (app / "state with spaces").exists(), "--check created app state"
        port = free_port()
        run("start", "--port", str(port))
        first = pid_file.read_text()
        assert "[HWMONITOR_READY]" in (state / "startup.log").read_text()
        assert "0.0.0.0" in (state / "startup.log").read_text()
        request(port)
        run("start")
        assert pid_file.read_text() == first, "duplicate start changed PID"
        # Discover the process even when the cache is gone.
        pid_file.unlink()
        run("status")
        assert pid_file.read_text() == first
        run("restart")
        assert pid_file.read_text() != first
        request(port)
        run("stop")
        run("status", expected=1)
        run("stop")
        # A bind failure must be reported and must not affect the listener owner.
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            run("start", "--port", str(occupied.getsockname()[1]), expected=1)
        assert not pid_file.exists()
        run("status", expected=1)
        # Old foreground instances can be managed after upgrading the script.
        foreground = subprocess.Popen([str(venv / "bin/python"), "-m", "hwmonitor",
                                       "--headless", "--port", str(port)], cwd=app, env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    request(port)
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise AssertionError("Foreground instance did not start")
            run("status")
            run("stop")
            assert foreground.wait(timeout=10) == 0
        finally:
            if foreground.poll() is None:
                foreground.terminate()
                foreground.wait(timeout=15)
    finally:
        run("stop")
        unrelated.terminate()
        unrelated.wait(timeout=5)

print("Launcher background start, duplicate start, recovery, restart, stop and failures passed")
