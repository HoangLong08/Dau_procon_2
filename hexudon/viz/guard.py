"""Quy tắc an toàn khi web trực quan hoá chơi trên server thi.

TEST_DAU_01 trùng TOKEN của đội: mọi bài web gửi đều mang tên đội thật. Vì vậy
trên server thật web chỉ vào Practice và trận có "Mock" trong tên, chỉ dùng biến
token TEST_*, và không bao giờ cho hai lượt chạy cùng token trên cùng câu (bài
sau ghi đè bài trước). Ngày thi dùng CLI `web --live --follow`, không dùng web.
"""

import re
import threading
from typing import Dict, Sequence, Tuple

from ..client.web_games import PRACTICE, GameRef

TEST_VAR_RE = re.compile(r"^TEST_[A-Z0-9]+(_[A-Z0-9]+)*$")


class GuardError(Exception):
    """Bị chặn vì quy tắc an toàn; `status` là mã HTTP web trả về."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def allowed_on_real(ref: GameRef) -> bool:
    """Server thật: chỉ practice và trận có 'mock' trong tên (không phân biệt hoa thường)."""
    return ref.mode == PRACTICE or "mock" in (ref.match_name or "").casefold()


def check_real_game(ref: GameRef) -> None:
    if not allowed_on_real(ref):
        raise GuardError(f"web không vào {ref.label()}: trên server thật chỉ chơi Practice và "
                         "Mock Contest; ngày thi dùng `python -m hexudon web --live --follow`")


def check_token_var(name: str) -> None:
    if not TEST_VAR_RE.fullmatch(name or ""):
        raise GuardError(f"web chỉ dùng biến token TEST_*; {name!r} không được phép")


class RunRegistry:
    """Các (đích, câu, token) đang có bot chơi."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active: Dict[Tuple[str, str, str], str] = {}

    def claim(self, target: str, game_id: str, token_vars: Sequence[str], run_id: str) -> None:
        keys = [(target, game_id, var) for var in token_vars]
        with self.lock:
            for key in keys:
                owner = self.active.get(key)
                if owner is not None:
                    raise GuardError(f"{key[2]} đang chơi câu này ở lượt {owner}; dừng lượt đó trước",
                                     status=409)
            for key in keys:
                self.active[key] = run_id

    def release_run(self, run_id: str) -> None:
        with self.lock:
            for key in [k for k, owner in self.active.items() if owner == run_id]:
                del self.active[key]
