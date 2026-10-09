"""Docker HEALTHCHECK: healthy while the bot's heartbeat file is fresh."""

import os
import sys
import time

MAX_AGE = 120


def main() -> int:
    path = os.environ.get("HEALTH_FILE", "/tmp/printerbot.health")
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return 1
    return 0 if age < MAX_AGE else 1


if __name__ == "__main__":
    sys.exit(main())
