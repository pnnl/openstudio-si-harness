"""Bounded POSIX subprocess capture shared by evaluation and readiness."""

import os
import selectors
import signal
import subprocess
import tempfile
import time


def process(
    command, cwd, env, timeout, max_bytes=8_000_000, prompt=None, *, pass_fds=()
):
    """Capture bounded stdout/stderr and terminate the host process group (POSIX)."""
    if os.name != "posix":
        raise ValueError("Bounded subprocess isolation currently supports macOS/Linux.")
    start = time.monotonic()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    gaps = []
    with tempfile.TemporaryFile() as incoming:
        incoming.write((prompt or "").encode())
        incoming.seek(0)
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=incoming,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=pass_fds,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, "stdout")
                selector.register(child.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    if time.monotonic() - start >= timeout:
                        gaps.append("host_timeout")
                        break
                    for key, _ in selector.select(
                        min(0.1, max(0.001, timeout - (time.monotonic() - start)))
                    ):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        available = max_bytes - sum(map(len, output.values()))
                        output[key.data].extend(data[: max(0, available)])
                        if len(data) > available:
                            gaps.append("trace_limit_exceeded")
                            break
                    if gaps:
                        break
            if not gaps:
                child.wait(timeout=max(0.01, timeout - (time.monotonic() - start)))
        except subprocess.TimeoutExpired:
            gaps.append("host_timeout")
        except KeyboardInterrupt:
            gaps.append("host_interrupted")
        finally:
            # Never signal a reaped PID: it may already belong to another group.
            if child.returncode is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    gaps.append("process_group_cleanup_denied")
                    child.kill()
            child.wait()
            child.stdout.close()
            child.stderr.close()
    return {
        **{
            key: value.decode("utf-8", errors="replace")
            for key, value in output.items()
        },
        "returncode": child.returncode,
        "gaps": gaps,
        "duration_seconds": round(time.monotonic() - start, 3),
    }
