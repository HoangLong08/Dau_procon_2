"""Giao diện kênh truyền: session không cần biết là HTTP hay sandbox."""

from typing import List, Optional, Sequence

from ..model import Ack, DayState, MatchConfig


class MatchEnded(Exception):
    """Trận đã kết thúc."""


class Transport:
    #: HTTP cho phép gửi lại nhiều lần trong ngày (bài hợp lệ cuối cùng được dùng);
    #: sandbox chỉ nhận đúng một dòng trả lời cho mỗi khung, nên phải gửi một lần.
    supports_resubmit = True

    def get_setup(self) -> MatchConfig:
        raise NotImplementedError

    def send_assignment(self, kinds: Sequence[int], deadline: Optional[float] = None) -> Ack:
        raise NotImplementedError

    def wait_day_state(self, kinds: Optional[Sequence[int]] = None) -> DayState:
        """Chặn tới khi có ngày mới; ném MatchEnded khi hết trận."""
        raise NotImplementedError

    def send_actions(self, day: int, plan: List[List[int]],
                     deadline: Optional[float] = None) -> Ack:
        """`deadline` là mốc time.monotonic() không được vượt qua."""
        raise NotImplementedError

    def get_result(self) -> Optional[dict]:
        return None

    def close(self) -> None:
        pass
