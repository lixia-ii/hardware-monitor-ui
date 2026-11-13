"""Real Linux startup without a display; verifies signal exit and persistence."""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time


def request(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        assert response.status == 200, response.status
        return response.read()
    finally:
        conn.close()


if sys.platform != "linux":
    raise SystemExit("This integration check requires Linux")

with tempfile.TemporaryDirectory() as directory:
    env = dict(os.environ, DISPLAY="", HARDWARE_MONITOR_DATA_DIR=directory)
    # A missing tkinter package or DISPLAY must not affect imports/startup.
    subprocess.run([sys.executable, "-c", "import sys; sys.modules['tkinter']=None; from hwmonitor.bootstrap import main; main(['--check'])"], env=env, check=True)
    assert not list(Path(directory).iterdir()), "--check wrote persistent state"
    ports = []
    for _ in range(2):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            ports.append(probe.getsockname()[1])
    for restart in range(2):
        process = subprocess.Popen([sys.executable, "-m", "hwmonitor", "--headless", "--port", str(ports[0]),
                                    "--data-port", str(ports[1])], env=env)
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise AssertionError(f"Server exited with {process.returncode}")
                try:
                    state = json.loads(request(ports[0], "/api/state"))
                    if state["data"] and state["data"]["cpu_load"] is not None:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise AssertionError("No usable samples within 15 seconds")
                time.sleep(.1)
            assert state["mode"] == "headless"
            assert state["data"]["mem_total"] > 0
            tree = json.loads(request(ports[1], "/data.json"))
            assert tree["Children"]
            assert json.loads(request(ports[0], "/api/history"))["count"] > 0
            assert request(ports[0], "/api/history.csv")
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=12) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert (Path(directory) / "history.sqlite3").stat().st_size > 0
print("Linux headless startup, HTTP, persistence and SIGTERM passed")
