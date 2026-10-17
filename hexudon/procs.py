"""Tiến trình con chạy được cả trên Windows lẫn Linux/macOS.

Ba chỗ khác nhau giữa hai họ hệ điều hành:
  - tách nhóm tiến trình: POSIX có start_new_session, Windows bỏ qua tham số đó
    nên phải dùng CREATE_NEW_PROCESS_GROUP, nếu không Ctrl-C ở terminal tới cả bot con;
  - dừng mềm: Popen.send_signal(SIGINT) không có trên Windows, phải gửi
    CTRL_BREAK_EVENT tới nhóm tiến trình của con (con đổi nó thành Ctrl-C);
  - mã hoá: stdout bị chuyển hướng ra file dùng bảng mã ANSI (cp1252), in tiếng
    Việt là UnicodeEncodeError, nên con luôn chạy ở chế độ UTF-8.
"""

import os
import signal
import subprocess
from typing import Optional

WINDOWS = os.name == "nt"


def detached_kwargs() -> dict:
    """Tham số Popen để con có nhóm tiến trình riêng: Ctrl-C ở terminal chỉ tới cha."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def child_env(root: str, base: Optional[dict] = None) -> dict:
    """Biến môi trường cho `python -m hexudon` con: thấy package, in UTF-8 trên mọi máy."""
    env = dict(os.environ if base is None else base)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def soft_stop(popen: subprocess.Popen) -> bool:
    """Gửi Ctrl-C cho con (Windows: CTRL_BREAK_EVENT). False nếu phải terminate thay."""
    try:
        popen.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        return True
    except (OSError, ValueError):
        # không cùng console (Windows) hoặc con vừa thoát: dừng cứng còn hơn không dừng
        if popen.poll() is None:
            popen.terminate()
        return False


def install_break_as_interrupt() -> None:
    """Windows: Ctrl-Break (soft_stop của cha) thành KeyboardInterrupt như Ctrl-C."""
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:
        return

    def _interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(sigbreak, _interrupt)
    except ValueError:          # không ở luồng chính
        pass
