"""Stop only a verified execution identity, including its live descendants."""
import contextlib
import time

import psutil


def live(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_stopped(processes, timeout):
    # Do not call waitpid through psutil: multiprocessing owns the direct
    # child's exit status and must reap it in Process.join() on Linux.
    deadline = time.monotonic() + timeout
    while True:
        remaining = [process for process in processes if live(process)]
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(.02)


def terminate_identity(pid, created_at):
    if pid is None:
        return
    if created_at is None:
        raise RuntimeError('Cannot terminate a process without its creation identity')
    try:
        process = psutil.Process(pid)
        if process.create_time() != created_at:
            return  # PID was reused: the old execution is gone, this process is unrelated.
        family = [*process.children(recursive=True), process]
        for member in family:
            with contextlib.suppress(psutil.NoSuchProcess):
                member.terminate()
        alive = wait_stopped(family, 2)
        for member in alive:
            with contextlib.suppress(psutil.NoSuchProcess):
                member.kill()
        alive = wait_stopped(alive, 3)
        if alive:
            raise RuntimeError('Execution descendants did not stop')
    except psutil.NoSuchProcess:
        return
