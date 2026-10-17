"""Điều phối lập kế hoạch một ngày: dựng ngữ cảnh, bài an toàn, rồi ALNS."""

import time
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..model import AgentState, MatchConfig
from ..pathing import DayGraph, DistTable, LegBook
from .alns import improve
from .construct import construct
from .decoder import Decoded, decode
from .solution import DayContext, DaySolution, Weights, build_context


def make_context(
    config: MatchConfig,
    day: int,
    agents: Sequence[AgentState],
    road_status: Dict[int, int],
    stocks: Sequence[int],
    visited_brands: Set[int],
    weights: Optional[Weights] = None,
    forecast_status: Optional[Dict[int, int]] = None,
    steps_override: Optional[int] = None,
) -> DayContext:
    """Dựng ngữ cảnh ngày, kèm các bảng khoảng cách."""
    graph = DayGraph(config, road_status)
    sources = sorted({s.pos for s in config.spots} | {a.pos for a in agents})
    dist_step = DistTable(graph, sources, "step")
    dist_fuel = DistTable(graph, sources, "fuel")
    book = LegBook(config, dist_step, dist_fuel)
    dist_next = None
    if forecast_status is not None and day + 1 < config.n_days:
        dist_next = DistTable(DayGraph(config, forecast_status), sources, "step")
    return build_context(
        config=config,
        day=day,
        agents=agents,
        road_status=road_status,
        stocks=stocks,
        visited_brands=visited_brands,
        book=book,
        dist_step=dist_step,
        dist_next=dist_next,
        weights=weights,
        steps_override=steps_override,
    )


def safe_solution(ctx: DayContext, seed: int = 0) -> Tuple[DaySolution, Decoded]:
    """Nghiệm an toàn và bài giải mã của nó: dựng tham lam một lần, luôn hợp lệ."""
    import random

    sol = construct(ctx, random.Random(seed))
    return sol, decode(ctx, sol)


def safe_plan(ctx: DayContext, seed: int = 0) -> Decoded:
    """Bài an toàn: dựng tham lam một lần, luôn hợp lệ, gửi ngay."""
    return safe_solution(ctx, seed)[1]


def plan_day(
    ctx: DayContext,
    deadline: Optional[float] = None,
    seed: int = 0,
    on_plan: Optional[Callable[[Decoded], None]] = None,
    max_iterations: int = 1_000_000,
    on_better: Optional[Callable[[Decoded], None]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    on_improve: Optional[Callable[[Decoded], None]] = None,
) -> Decoded:
    """Lập kế hoạch một ngày: gửi bài an toàn trước, rồi cải thiện tới hạn chót.

    on_plan được gọi với bài an toàn, rồi với bài cuối nếu nó tốt hơn bài an toàn.
    on_improve được gọi ngay khi ALNS tìm ra nghiệm tốt hơn hẳn mọi nghiệm trước
    đó, để bên gọi tự quyết có gửi sớm không: thời gian phản hồi tính theo bài
    hợp lệ cuối cùng (cases 11.8). Hai móc còn lại chỉ để quan sát: on_better
    nhận đúng các nghiệm như on_improve; on_progress chuyển thẳng cho ALNS.

    ALNS bắt đầu từ chính nghiệm an toàn, nên bài cuối không bao giờ kém nó.
    """
    sol, first = safe_solution(ctx, seed)
    if on_plan is not None:
        on_plan(first)
    if deadline is not None and time.monotonic() >= deadline:
        return first
    best_seen = {"score": first.score}

    def better(_sol, decoded: Decoded):
        if decoded.score > best_seen["score"]:
            best_seen["score"] = decoded.score
            if on_better is not None:
                on_better(decoded)
            if on_improve is not None:
                on_improve(decoded)

    result = improve(
        ctx,
        start=sol,
        deadline=deadline,
        max_iterations=max_iterations,
        seed=seed,
        on_better=better,
        on_progress=on_progress,
    )
    final = result.decoded if result.decoded.score > first.score else first
    if on_plan is not None and final is not first:
        on_plan(final)
    return final
