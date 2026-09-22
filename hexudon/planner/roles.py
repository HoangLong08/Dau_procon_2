"""Chọn vai trò xe tuần tra / xe tiếp nhiên liệu trước ngày 0.

Đây là quyết định không sửa được sau khi trận bắt đầu và có ảnh hưởng lớn nhất
tới điểm cuối: thiếu xe tiếp thì xe tuần tra cạn xăng từ ngày 2 và số chuỗi mỗi
ngày sụp đổ; thừa xe tiếp thì thiếu xe thu udon.

Cách làm: chơi thử CẢ TRẬN cho từng phương án bằng bộ dựng nghiệm tham lam
(nhanh và tất định), rồi nếu còn thời gian thì đánh giá lại hai phương án dẫn
đầu bằng tìm kiếm ALNS thật.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .. import rules
from ..model import AgentState, MatchConfig
from ..pathing import DayGraph, DistTable
from ..traffic import TrafficForecast
from .construct import construct
from .decoder import decode
from .engine import make_context, plan_day
from .solution import Weights


@dataclass
class RoleCandidate:
    kinds: List[int]
    label: str
    score: Tuple[int, int, int] = (0, 0, 0)
    refined: Tuple[int, int, int] = (0, 0, 0)
    evaluated: bool = False

    @property
    def n_cars(self) -> int:
        return sum(1 for k in self.kinds if k == rules.REFUEL)

    def best(self) -> Tuple[int, int, int]:
        return max(self.score, self.refined)


def centrality(config: MatchConfig) -> List[float]:
    """Tổng khoảng cách từ vị trí xuất phát tới mọi spot; nhỏ hơn là trung tâm hơn."""
    graph = DayGraph(config, road_override=rules.SMOOTH)
    table = DistTable(graph, config.agent_starts, "step")
    out = []
    for start in config.agent_starts:
        total = 0.0
        for s in config.spots:
            d = table.cost(start, s.pos)
            total += d if d != float("inf") else 10_000
        out.append(total)
    return out


def candidates(config: MatchConfig) -> List[RoleCandidate]:
    """Liệt kê phương án vai trò: 0..3 xe tiếp, chọn theo độ trung tâm."""
    n = config.n_agents
    cen = centrality(config)
    order = sorted(range(n), key=lambda i: cen[i])
    max_cars = max(0, min(3, n - 2))
    out = [RoleCandidate([rules.PATROL] * n, "0 xe tiếp")]
    for count in range(1, max_cars + 1):
        picks = [tuple(order[:count])]
        if count < len(order):
            alt = tuple(order[:count - 1] + [order[count]])
            if alt != picks[0]:
                picks.append(alt)
        for pick in picks:
            kinds = [rules.PATROL] * n
            for i in pick:
                kinds[i] = rules.REFUEL
            label = f"{count} xe tiếp (agent {', '.join(str(i) for i in pick)})"
            if not any(c.kinds == kinds for c in out):
                out.append(RoleCandidate(kinds, label))
    return out


def rollout(
    config: MatchConfig,
    kinds: Sequence[int],
    forecast: TrafficForecast,
    weights: Optional[Weights] = None,
    seed: int = 0,
    deadline: Optional[float] = None,
    seconds_per_day: float = 0.0,
    with_days: bool = False,
):
    """Chơi thử cả trận một mình với trạng thái đường dự báo. Trả (L, D, Q).

    seconds_per_day = 0 nghĩa là chỉ dùng bộ dựng nghiệm tham lam (tất định, ~5 ms/ngày).
    `with_days=True` trả thêm số ngày thực sự chơi được: một mẫu bị cắt giữa
    chừng KHÔNG so sánh được với mẫu chơi đủ, nên người gọi phải loại nó ra.
    """
    agents = [AgentState(kinds[i], config.agent_starts[i], config.fuel_limits)
              for i in range(config.n_agents)]
    visited: set = set()
    daily_types = 0
    servings = 0
    status = {c: rules.SMOOTH for c in config.road_cells}
    prev_status = None
    rng = random.Random(seed)

    days_done = 0
    for day in range(config.n_days):
        if deadline is not None and time.monotonic() >= deadline:
            break
        next_status = forecast.predict(status, prev_status)
        ctx = make_context(
            config, day, agents, status, [s.stocks for s in config.spots], visited,
            weights=weights, forecast_status=next_status,
        )
        if seconds_per_day > 0:
            stop = time.monotonic() + seconds_per_day
            if deadline is not None:
                stop = min(stop, deadline)
            decoded = plan_day(ctx, deadline=stop, seed=seed)
        else:
            decoded = decode(ctx, construct(ctx, rng))
        result = decoded.result
        visited |= result.brands
        daily_types += len(result.brands)
        servings += result.servings
        agents = [AgentState(kinds[i], result.end_pos[i], result.end_fuel[i])
                  for i in range(config.n_agents)]
        # dự báo phải nhận HÔM NAY và HÔM QUA; truyền hôm nay hai lần thì
        # công thức hai ngày của luật thành vô nghĩa đúng lúc nó cần nhất
        status, prev_status = forecast.predict(
            status, prev_status, own_stay_today=result.stay,
            steps_today=config.steps_of_day(day)), status
        days_done += 1
    score = (len(visited), daily_types, servings)
    return (score, days_done) if with_days else score


def select_roles(
    config: MatchConfig,
    budget_seconds: float = 3.0,
    weights: Optional[Weights] = None,
    seed: int = 0,
    log: Optional[Callable[[str], None]] = None,
    on_candidate: Optional[Callable[[dict], None]] = None,
) -> Tuple[List[int], List[RoleCandidate]]:
    """Chọn mảng loại xe. Hết giờ vẫn luôn trả về một phương án hợp lệ.

    `on_candidate`, nếu có, nhận từng phương án ngay khi được chấm (để web vẽ bảng).
    """
    log = log or (lambda msg: None)

    def tell(cand: RoleCandidate, phase: str, score, **extra) -> None:
        if on_candidate is not None:
            on_candidate(dict(label=cand.label, kinds=list(cand.kinds), score=list(score),
                              phase=phase, **extra))

    started = time.monotonic()
    hard_deadline = started + max(0.2, budget_seconds)
    options = candidates(config)
    forecast = TrafficForecast(config, n_patrols=max(1, config.n_agents - 1))

    # Vòng 1: rollout tham lam toàn bộ phương án, tất định và rẻ.
    for cand in options:
        if time.monotonic() >= hard_deadline:
            break
        cand.score, days_done = rollout(config, cand.kinds, forecast, weights=weights,
                                        seed=seed, deadline=hard_deadline, with_days=True)
        if days_done < config.n_days:
            # mẫu dở dang xếp hạng cùng mẫu đủ sẽ chọn sai; thà bỏ đi
            log(f"vai trò {cand.label}: bỏ, chỉ chơi được {days_done}/{config.n_days} ngày")
            tell(cand, "skipped", cand.score, days=days_done)
            continue
        cand.evaluated = True
        log(f"vai trò {cand.label}: L={cand.score[0]} D={cand.score[1]} Q={cand.score[2]}")
        tell(cand, "rollout", cand.score)

    ranked = sorted([c for c in options if c.evaluated], key=lambda c: c.score, reverse=True)
    if not ranked:
        fallback = options[1] if len(options) > 1 else options[0]
        return list(fallback.kinds), options

    # Vòng 2: nếu còn thời gian, đánh giá lại hai phương án dẫn đầu bằng ALNS thật.
    remaining = hard_deadline - time.monotonic()
    finalists = ranked[:2]
    if remaining > 1.0 and len(finalists) > 1:
        per_candidate = remaining / len(finalists)
        per_day = max(0.05, per_candidate / max(1, config.n_days))
        for cand in finalists:
            if time.monotonic() >= hard_deadline:
                break
            cand.refined = rollout(
                config, cand.kinds, forecast, weights=weights, seed=seed,
                deadline=min(hard_deadline, time.monotonic() + per_candidate),
                seconds_per_day=per_day,
            )
            log(f"tinh chỉnh {cand.label}: L={cand.refined[0]} D={cand.refined[1]} Q={cand.refined[2]}")
            tell(cand, "refine", cand.refined)
        best = max(finalists, key=lambda c: c.best())
    else:
        best = ranked[0]

    log(f"chọn: {best.label}")
    return list(best.kinds), options
