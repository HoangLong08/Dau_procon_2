"""Vòng đời một trận: không biết thuật toán, không biết kênh truyền."""

import json
import os
import time
from typing import Callable, List, Optional

from ..model import DayState, MatchConfig
from ..planner.decoder import Decoded
from ..planner.solution import Weights
from ..planner.strategy import MatchStrategist
from ..simulator import wait_plan
from .transport import MatchEnded, Transport

WATCHDOG_SECONDS = 1.5


class ReplayWriter:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.handle = None
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.handle = open(path, "a", encoding="utf-8")

    def __call__(self, entry: dict) -> None:
        if self.handle is None:
            return
        try:
            self.handle.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")
            self.handle.flush()
        except Exception:
            pass

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class MatchSession:
    """Chạy trọn một trận qua một kênh truyền bất kỳ."""

    def __init__(
        self,
        transport: Transport,
        search_seconds: float = 3.0,
        net_margin: float = 1.5,
        role_seconds: float = 20.0,
        seed: int = 0,
        weights: Optional[Weights] = None,
        log: Optional[Callable[[str], None]] = None,
        replay: Optional[ReplayWriter] = None,
    ):
        self.transport = transport
        self.search_seconds = search_seconds
        self.net_margin = net_margin
        self.role_seconds = role_seconds
        self.seed = seed
        self.weights = weights
        self.log = log or (lambda msg: None)
        self.replay = replay or (lambda entry: None)
        self.strategist: Optional[MatchStrategist] = None
        self.config: Optional[MatchConfig] = None
        #: server có thể đánh số ngày từ 1 thay vì 0; học từ lần bị từ chối đầu tiên
        self.day_offset = 0

    def run(self) -> Optional[dict]:
        self.config = self.transport.get_setup()
        self.strategist = MatchStrategist(
            self.config, weights=self.weights, search_seconds=self.search_seconds,
            net_margin=self.net_margin, role_seconds=self.role_seconds,
            seed=self.seed, log=self.log,
        )
        t0 = time.monotonic()
        kinds = self.strategist.select_roles()
        self.log(f"chọn vai trò xong trong {time.monotonic() - t0:.1f}s")
        ack = self.transport.send_assignment(kinds)
        self.replay({"assignment": kinds, "ok": ack.ok})
        if not ack.ok:
            self.log("server không nhận vai trò; tiếp tục với vai trò mặc định của server")

        while True:
            try:
                state = self.transport.wait_day_state(kinds)
            except MatchEnded as end:
                self.log(f"trận kết thúc: {end.args[0] if end.args else ''}")
                break
            self.play_day(state)

        result = self.transport.get_result()
        if self.strategist is not None:
            L, D, Q = self.strategist.score_tuple()
            self.log(f"điểm dự kiến của ta: L={L} D={D} Q={Q}")
        return result

    def steps_for(self, day: int) -> int:
        """Số bước của ngày, đã tính tới khả năng server đánh số ngày từ 1."""
        assert self.config is not None
        return self.config.steps_of_day(day + self.day_offset)

    def maybe_fix_day_indexing(self, ack, state: DayState) -> None:
        """Server đánh số ngày từ 1 sẽ làm tổng số bước sai và bài bị từ chối.

        Nhận ra qua mã lỗi E_STEP_OVERFLOW ngay lần gửi đầu, rồi lùi chỉ số ngày
        một đơn vị cho phần còn lại của trận.
        """
        assert self.config is not None
        if self.day_offset != 0 or not ack.reason:
            return
        if "STEP" not in str(ack.reason).upper():
            return
        alt = state.day - 1
        if 0 <= alt < self.config.n_days and self.config.day_steps[alt] != self.config.steps_of_day(state.day):
            self.day_offset = -1
            self.log(f"server có vẻ đánh số ngày từ 1: chuyển sang dùng daySteps[{alt}]")

    def play_day(self, state: DayState) -> Decoded:
        assert self.strategist is not None and self.config is not None
        started = time.monotonic()
        steps = self.steps_for(state.day)
        budget = self.strategist.budget_for(state)
        deadline = started + budget
        sent = {"count": 0, "ok": False}

        def submit(plan: List[List[int]]) -> bool:
            ack = self.transport.send_actions(state.day, plan)
            sent["count"] += 1
            sent["ok"] = sent["ok"] or ack.ok
            self.replay({"day": state.day, "plan": plan, "ok": ack.ok, "reason": ack.reason})
            if not ack.ok:
                self.log(f"ngày {state.day}: bài bị từ chối ({ack.reason})")
                self.maybe_fix_day_indexing(ack, state)
            return ack.ok

        resubmit = getattr(self.transport, "supports_resubmit", True)
        try:
            decoded = self.strategist.plan_day(
                state, deadline=deadline, submit=submit if resubmit else None,
                steps_override=steps,
            )
            if not resubmit:
                # kênh chỉ nhận một dòng trả lời cho mỗi ngày: gửi đúng bài tốt nhất
                submit(decoded.plan)
        except Exception as exc:
            self.log(f"ngày {state.day}: planner lỗi {exc!r}")
            if sent["count"] == 0:
                submit(wait_plan(self.config.n_agents, steps))
            raise

        if sent["count"] == 0 or not sent["ok"]:
            self.log(f"ngày {state.day}: chưa có bài nào được nhận, gửi bài đứng yên")
            submit(wait_plan(self.config.n_agents, steps))

        if not sent["ok"] and self.day_offset != 0:
            # đã phát hiện lệch cách đánh số ngày: lập lại kế hoạch với số bước đúng
            self.log(f"ngày {state.day}: lập lại kế hoạch với {self.steps_for(state.day)} bước")
            decoded = self.strategist.plan_day(
                state, deadline=time.monotonic() + max(0.2, deadline - time.monotonic()),
                submit=None, steps_override=self.steps_for(state.day),
            )
            submit(decoded.plan)

        elapsed = time.monotonic() - started
        self.log(
            f"ngày {state.day}: {len(decoded.result.brands)} loại, {decoded.result.servings} phần, "
            f"gửi {sent['count']} lần, {elapsed:.2f}s (ngân sách {budget:.2f}s)"
        )
        return decoded
