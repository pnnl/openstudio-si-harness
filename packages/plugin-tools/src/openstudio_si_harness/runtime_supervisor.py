"""Keep the probe group leader alive until its ordinary children are terminated."""

import json
import os
import signal
import subprocess
import sys


def main():
    receipt_fd = int(sys.argv[1])
    try:
        # The private receipt descriptor is not passed to the probed program.
        child = subprocess.Popen(sys.argv[2:], stdin=sys.stdin, close_fds=True)
        code = child.wait()
        os.write(receipt_fd, json.dumps({"returncode": code}).encode())
        # We are the still-live group leader, so our PID cannot have been reused.
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except (OSError, ValueError):
        os._exit(125)


if __name__ == "__main__":
    main()
