"""Chuyển dữ liệu trận sang dạng gọn cho sự kiện của web trực quan hoá."""

from typing import Dict, List, Sequence, Tuple

from ..model import AgentState, MatchConfig
from ..simulator import DayResult, run_day


def config_payload(config: MatchConfig) -> dict:
    """Những gì trang cần để vẽ bản đồ và dòng thời gian."""
    return {
        "width": config.width,
        "height": config.height,
        "cells": list(config.cells),
        "spots": [{"brand": s.brand, "pos": s.pos, "stocks": s.stocks} for s in config.spots],
        "starts": list(config.agent_starts),
        "fuel": config.fuel_limits,
        "day_steps": list(config.day_steps),
        "n_days": config.n_days,
        "players": config.players,
    }


def status_payload(status: Dict[int, int]) -> Dict[str, int]:
    """Chỉ giữ ô đông/tắc; khoá là chuỗi vì JSON."""
    return {str(cell): int(value) for cell, value in sorted(status.items()) if value}


def agents_payload(agents: Sequence[AgentState]) -> List[dict]:
    return [{"kind": a.kind, "pos": a.pos, "fuel": a.fuel} for a in agents]


def trace_from(trace: list, result: DayResult) -> dict:
    """Vết (bước, vị trí, xăng) của simulator thành các mảng gọn, kèm lỗi đầu tiên nếu có."""
    error = None
    if not result.ok and result.error is not None:
        e = result.error
        error = {"code": e.code, "agent": e.agent, "step": e.step, "detail": e.detail}
    return {
        "pos": [list(pos) for _, pos, _ in trace],
        "fuel": [list(fuel) for _, _, fuel in trace],
        "collect": [list(c) for c in result.collections],
        "error": error,
    }


def simulate(config: MatchConfig, agents: Sequence[AgentState], plan, steps: int,
             stocks: Sequence[int], status: Dict[int, int]) -> Tuple[DayResult, dict]:
    """Chạy lại một ngày có vết để web vẽ từng bước; không đổi kết quả."""
    trace: list = []
    result = run_day(config, steps, agents, plan, stocks, status, trace=trace)
    return result, trace_from(trace, result)
