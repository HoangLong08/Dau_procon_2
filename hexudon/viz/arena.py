"""Trận offline n bot cho web trực quan hoá: simulator của repo làm trọng tài.

Các bot lần lượt lập kế hoạch mỗi ngày (mỗi bot dùng trọn ngân sách của nó),
giao thông cộng dồn mọi đội như luật thật. Chỉ bot `search` có suy luận để xem;
bot còn lại chỉ có kết quả từng ngày.
"""

import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from ..model import MatchConfig
from ..offline.bots import BOT_TYPES, SearchBot
from ..offline.runner import MatchOutcome, run_match
from .events import Emit, log_level
from .shapes import agents_payload, config_payload, status_payload, trace_from

EmitFor = Callable[[Optional[str]], Emit]
MAX_SEARCH_SECONDS = 60.0


@dataclass(frozen=True)
class BotSpec:
    kind: str                  # search / greedy / sample / wait
    seconds: float = 1.0       # giây tìm kiếm mỗi ngày; chỉ bot search dùng

    @staticmethod
    def parse(text: str) -> "BotSpec":
        """'search:3' -> search 3 s mỗi ngày; 'greedy' -> greedy."""
        name, _, secs = text.strip().partition(":")
        if name not in BOT_TYPES:
            raise ValueError(f"không có loại bot {name!r}; chọn một trong {', '.join(BOT_TYPES)}")
        try:
            seconds = float(secs) if secs else 1.0
        except ValueError:
            raise ValueError(f"số giây không đọc được: {text!r}") from None
        if not math.isfinite(seconds) or not 0 < seconds <= MAX_SEARCH_SECONDS:
            raise ValueError(f"số giây phải là số hữu hạn trong khoảng (0, {MAX_SEARCH_SECONDS:g}]: {text!r}")
        return BotSpec(name, seconds)

    def label(self, index: int) -> str:
        suffix = f" {self.seconds:g}s" if self.kind == "search" else ""
        return f"{index + 1}. {self.kind}{suffix}"


def build_bots(config: MatchConfig, specs: Sequence[BotSpec], seed: int, emit_for: EmitFor,
               role_seconds: float) -> list:
    bots = []
    for i, spec in enumerate(specs):
        name = spec.label(i)
        if spec.kind == "search":
            bot = SearchBot(config, search_seconds=spec.seconds, role_seconds=role_seconds,
                            seed=seed + i, emit=emit_for(name))
        else:
            bot = BOT_TYPES[spec.kind](config, seed=seed + i)
        bot.name = name
        bots.append(bot)
    return bots


class _Observer:
    """Nối runner với file sự kiện."""

    def __init__(self, emit_for: EmitFor):
        self.emit_for = emit_for
        self.match_emit = emit_for(None)

    def match_ready(self, records) -> None:
        for rec in records:
            self.emit_for(rec.name)("roles_chosen", {"kinds": list(rec.kinds)})

    def day_start(self, day, steps, status, records) -> None:
        self.match_emit("day_start", {
            "day": day, "steps": steps, "status": status_payload(status),
            "teams": {rec.name: agents_payload(rec.agents) for rec in records},
        })

    def team_day(self, idx, rec, state, plan, result, elapsed, error, trace) -> None:
        err = None
        if error is not None:
            err = {"code": error.code, "agent": error.agent, "step": error.step, "detail": error.detail}
        self.emit_for(rec.name)("day_result", {
            "day": state.day, "ok": plan is not None and error is None, "error": err,
            "types": len(result.brands), "servings": result.servings,
            "brands": sorted(result.brands), "seconds": round(elapsed, 3),
            "trace": trace_from(trace or [], result), "total": list(rec.score()),
        })


def run_arena(config: MatchConfig, specs: Sequence[BotSpec], emit_for: EmitFor, seed: int = 0,
              role_seconds: float = 3.0) -> MatchOutcome:
    """Chạy trọn trận offline, phát sự kiện qua emit_for(tên bot | None)."""
    # lưu lượng chia cho số đội thật trong trận (cases 3, 8.8)
    config.players = len(specs)
    bots = build_bots(config, specs, seed, emit_for, role_seconds)
    emit = emit_for(None)
    emit("match_start", {
        "mode": "offline", "config": config_payload(config),
        "bots": [{"name": bot.name, "type": spec.kind,
                  "seconds": spec.seconds if spec.kind == "search" else None}
                 for bot, spec in zip(bots, specs)],
    })

    def log(msg: str) -> None:
        emit("log", {"text": msg, "level": log_level(msg)})

    outcome = run_match(config, bots, log=log, observer=_Observer(emit_for))
    emit("match_end", {"ranking": [
        {"name": rec.name, "L": rec.score()[0], "D": rec.score()[1], "Q": rec.score()[2],
         "invalid_days": rec.invalid_days, "kinds": list(rec.kinds)}
        for rec in outcome.ranking()]})
    return outcome
