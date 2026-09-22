"""Sự kiện suy luận của bot: ghi JSON Lines để web trực quan hoá đọc lại.

Mỗi dòng một sự kiện {seq, t, bot, kind, data}. `t` là giờ máy để trộn được
nhiều file (mỗi token online một tiến trình, một file) theo đúng thứ tự. Ghi
xong dòng nào flush dòng đó để web đọc được ngay lúc bot còn chạy; bên đọc bỏ
qua dòng cuối chưa ghi xong và đọc lại nó ở lần sau.
"""

import json
import os
import threading
import time
from typing import Callable, List, Optional, Tuple

Emit = Callable[[str, dict], None]

#: log có một trong các cụm này thì tô đỏ trên web
WARN_WORDS = ("lỗi", "từ chối", "quá hạn", "lệch", "thất bại", "không hợp lệ", "cảnh báo")


def NULL_EMIT(kind: str, data: dict) -> None:
    """Không ghi gì: mặc định khi không bật --events."""


def log_level(text: str) -> str:
    low = text.casefold()
    return "warn" if any(word in low for word in WARN_WORDS) else "info"


class EventWriter:
    """Một file sự kiện; an toàn khi nhiều luồng cùng ghi."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time):
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self.clock = clock
        self.seq = 0
        self.lock = threading.Lock()
        self.handle = open(path, "a", encoding="utf-8")

    def write(self, bot: Optional[str], kind: str, data: dict) -> None:
        with self.lock:
            if self.handle is None:
                return
            entry = {"seq": self.seq, "t": round(self.clock(), 3), "bot": bot, "kind": kind, "data": data}
            self.seq += 1
            try:
                self.handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":"),
                                             default=str) + "\n")
                self.handle.flush()
            except (OSError, ValueError):
                # sự kiện chỉ để xem: lỗi ghi không được làm hỏng trận
                pass

    def for_bot(self, bot: Optional[str]) -> Emit:
        return lambda kind, data: self.write(bot, kind, data)

    def close(self) -> None:
        with self.lock:
            if self.handle is not None:
                self.handle.close()
                self.handle = None


def read_events(path: str, offset: int = 0) -> Tuple[List[dict], int]:
    """Các sự kiện đủ dòng từ `offset` (byte); trả (sự kiện, offset mới)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return [], offset
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    events = []
    for line in chunk[:end].split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, offset + end + 1
