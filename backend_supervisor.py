from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


SERVER_SCRIPT = Path(__file__).with_name("web_frontend.py")
BASE_DELAY_SECONDS = 2
MAX_DELAY_SECONDS = 20
STABLE_RUN_SECONDS = 45


def compute_restart_delay(restart_count: int) -> int:
    return min(BASE_DELAY_SECONDS * max(restart_count, 1), MAX_DELAY_SECONDS)


def build_server_command() -> list[str]:
    return [sys.executable, str(SERVER_SCRIPT)]


def run_supervisor() -> None:
    restart_count = 0
    print("Laser ai backend supervisor is active.")
    print("The web server will restart automatically if it stops.")

    while True:
        start_time = time.monotonic()
        process = subprocess.Popen(build_server_command(), env=os.environ.copy())
        exit_code = process.wait()
        run_duration = time.monotonic() - start_time

        if exit_code == 0:
            print("Laser ai server stopped normally. Supervisor exiting.")
            return

        restart_count = 1 if run_duration >= STABLE_RUN_SECONDS else restart_count + 1
        delay = compute_restart_delay(restart_count)
        print(
            f"Laser ai server exited with code {exit_code}. "
            f"Restarting in {delay} seconds..."
        )
        time.sleep(delay)


if __name__ == "__main__":
    try:
        run_supervisor()
    except KeyboardInterrupt:
        print("\nSupervisor stopped.")
