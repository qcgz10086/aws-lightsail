"""Clean up only the process tree created by a baseline run."""
import psutil


def stop_process_tree(proc):
    try:
        root = psutil.Process(proc.pid)
        owned = root.children(recursive=True) + [root]
    except psutil.NoSuchProcess:
        proc.wait()
        return
    for process in reversed(owned):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(owned, timeout=5)
    for process in alive:
        process.kill()
    psutil.wait_procs(alive, timeout=5)
    proc.wait(timeout=5)
