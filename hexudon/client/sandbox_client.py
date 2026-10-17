"""Kênh JSON Lines qua stdin/stdout cho chế độ sandbox.

Mỗi dòng stdin là một khung JSON; phân loại theo trường có mặt (khung không có
trường 'type'). Trả lời bằng một dòng JSON trên stdout. Log chỉ ra stderr.
"""

import json
import sys
import time
from typing import Callable, List, Optional, Sequence, TextIO

from ..model import Ack, DayState, MatchConfig
from .transport import MatchEnded, Transport


class SandboxTransport(Transport):
    supports_resubmit = False

    def __init__(self, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None,
                 log: Optional[Callable[[str], None]] = None,
                 replay: Optional[Callable[[dict], None]] = None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.log = log or (lambda msg: None)
        self.replay = replay or (lambda entry: None)
        self.config: Optional[MatchConfig] = None
        self.kinds: Optional[List[int]] = None
        self.pending: Optional[dict] = None
        self.deadline: Optional[float] = None

    def _read(self) -> dict:
        if self.pending is not None:
            frame, self.pending = self.pending, None
            return frame
        while True:
            line = self.stdin.readline()
            if not line:
                raise MatchEnded("stdin đã đóng")
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                self.log(f"bỏ qua dòng không phải JSON: {exc}")
                continue
            self.replay({"t": time.time(), "in": frame})
            return frame

    def _write(self, payload) -> None:
        self.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.stdout.flush()
        self.replay({"t": time.time(), "out": payload})

    @staticmethod
    def _is_setup(frame: dict) -> bool:
        return "map" in frame

    @staticmethod
    def _is_day(frame: dict) -> bool:
        return "traffics" in frame or ("agents" in frame and "day" in frame)

    @staticmethod
    def _is_end(frame: dict) -> bool:
        return "standings" in frame or "result" in frame or frame.get("finished") is True

    def get_setup(self) -> MatchConfig:
        while True:
            frame = self._read()
            if self._is_setup(frame):
                self.config = MatchConfig.from_json(frame)
                self.log(f"setup: {self.config.width}x{self.config.height}, "
                         f"{self.config.n_agents} agent, {self.config.n_days} ngày")
                return self.config
            if self._is_end(frame):
                raise MatchEnded(frame)

    def send_assignment(self, kinds: Sequence[int], deadline: Optional[float] = None) -> Ack:
        self.kinds = list(kinds)
        self._write(list(kinds))
        return Ack(ok=True)

    def wait_day_state(self, kinds: Optional[Sequence[int]] = None) -> DayState:
        kinds = kinds if kinds is not None else self.kinds
        while True:
            frame = self._read()
            if self._is_end(frame):
                raise MatchEnded(frame)
            if self._is_day(frame):
                # cần bề rộng map để đọc vị trí dạng (cột, hàng), và dung tích bình
                # làm mặc định khi khung ẩn xăng (đọc thành 0 thì mọi xe đứng yên)
                state = DayState.from_json(
                    frame, kinds,
                    width=self.config.width if self.config is not None else None,
                    fuel_limits=self.config.fuel_limits if self.config is not None else None,
                )
                if state.ends_at is None and self.config is not None:
                    state.ends_at = time.time() + self.config.seconds_of_day(state.day)
                return state
            if self._is_setup(frame):
                self.config = MatchConfig.from_json(frame)

    def send_actions(self, day: int, plan: List[List[int]],
                     deadline: Optional[float] = None) -> Ack:
        self._write(plan)
        return Ack(ok=True)
