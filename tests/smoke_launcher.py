"""Exercise the launcher against real Linux processes, HTTP and occupied ports."""
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "smoke-session-token-0123456789abcdef"


def seed_device(directory):
    """Pre-pair one browser: the service only answers signed-in devices."""
    directory.mkdir(parents=True, exist_ok=True)
    now = time.time()
    device = {"id": "smoke", "hash": hashlib.sha256(TOKEN.encode()).hexdigest(), "name": "smoke",
              "ip": "127.0.0.1", "created": now, "last_seen": now}
    (directory / "management_devices.json").write_text(json.dumps({"devices": [device]}), encoding="utf-8")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


with tempfile.TemporaryDirectory(prefix="hwmonitor launcher ") as directory:
    app = Path(directory)
    shutil.copy(ROOT / "start-linux.sh", app)
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
        result = subprocess.run(["sh", str(app / "start-linux.sh"), *args], env=env,
                                cwd="/", text=True, capture_output=True, timeout=45)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
        return result.stdout

    def request(port, path="/api/state"):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            conn.request("GET", path, headers={"Cookie": f"hm_session={TOKEN}"})
            response = conn.getresponse()
            assert response.status == 200, response.status
            return json.loads(response.read())
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
        seed_device(app / "state with spaces")
        port = free_port()
        run("start", "--port", str(port))
        first = pid_file.read_text()
        assert "[HWMONITOR_READY]" in (state / "startup.log").read_text()
        assert "0.0.0.0" in (state / "startup.log").read_text()
        request(port)
        # The data endpoint is a setting and stays off until enabled.
        assert not request(port, "/api/settings")["settings"]["web"]["enabled"]
        run("start")
        assert pid_file.read_text() == first, "duplicate start changed PID"
        # Discover the process even when the cache is gone.
        pid_file.unlink()
        run("status")
        assert pid_file.read_text() == first
        # A legacy --data-port in saved arguments is moved into the settings once, then not replayed.
        data_port = free_port()
        while data_port == port:
            data_port = free_port()
        (state / "arguments").write_bytes(f"--host\x000.0.0.0\x00--port\x00{port}\x00--data-port\x00{data_port}\x00".encode())
        run("restart")
        assert pid_file.read_text() != first
        request(port)
        assert "Children" in request(data_port, "/data.json")
        assert b"--data-port" not in (state / "arguments").read_bytes()
        run("restart")
        assert "Children" in request(data_port, "/data.json")
        assert request(port, "/api/settings")["settings"]["web"] == {"enabled": True, "lan": True, "port": data_port}
        # A management port equal to the data port is reported, not fatal.
        run("restart", "--port", str(data_port))
        assert request(data_port, "/api/settings")["web_error"]
        run("restart", "--port", str(port))
        assert "Children" in request(data_port, "/data.json")
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
