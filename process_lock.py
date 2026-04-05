import os
import sys


def is_process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def acquire_single_process_lock(path: str):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        return fd
    except FileExistsError:
        try:
            with open(path, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip())
            if not is_process_alive(pid):
                raise ProcessLookupError("stale lock")
        except (ProcessLookupError, ValueError):
            try:
                os.remove(path)
            except Exception:
                return None
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("utf-8"))
                return fd
            except Exception:
                return None
        return None
    except Exception:
        return None
