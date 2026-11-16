"""Chơi tiếp trận từ một trạng thái giữa trận (R4, rollout cuối ngày, 16/11/2026).

Dùng chung cho hai việc:
  - roles.rollout: chơi thử cả trận từ ngày 0 để chọn vai trò xe;
  - rollout cuối ngày: chấm một kế hoạch hôm nay bằng kết quả các ngày sau khi
    chơi tiếp bằng bộ dựng tham lam (E2: chọn theo cách này bớt 20% D bị thiếu,
    trong khi end_value tương quan 0,00 với tương lai).

Giao thông các ngày sau theo dự báo (TrafficForecast), như roles.rollout vẫn làm.
"""

import random
import time
from typing import Dict, Optional, Sequence, Set, Tuple

from ..model import AgentState, MatchConfig
from ..traffic import TrafficForecast
from .construct import construct
from .decoder import decode
from .engine import make_context, plan_day
from .solution import Weights


def play_days(
    config: MatchConfig,
    kinds: Sequence[int],
    forecast: TrafficForecast,
    start_day: int,
    agents: Sequence[AgentState],
    visited: Set[int],
    status: Dict[int, int],
    prev_status: Optional[Dict[int, int]],
    prev_stay: Optional[Dict[int, int]],
    weights: Optional[Weights] = None,
    seed: int = 0,
    deadline: Optional[float] = None,
    seconds_per_day: float = 0.0,
    iterations_per_day: int = 0,
    max_days: Optional[int] = None,
) -> Tuple[Set[int], int, int, int]:
    """Chơi các ngày start_day..hết trận. Trả (chuỗi đã thu cả trận, tổng D, tổng Q, số ngày chơi).

    `status` là trạng thái đường của ngày start_day; `prev_status`, `prev_stay` là của
    ngày trước đó (để dự báo đúng công thức hai ngày của luật).
    seconds_per_day = iterations_per_day = 0 nghĩa là chỉ dựng tham lam (tất định).
    """
    agents = list(agents)
    visited = set(visited)
    daily_types = 0
    servings = 0
    days_done = 0
    rng = random.Random(seed)
    end_day = config.n_days if not max_days else min(config.n_days, start_day + int(max_days))
    for day in range(start_day, end_day):
        if deadline is not None and time.monotonic() >= deadline:
            break
        next_status = forecast.predict(status, prev_status, own_stay_yesterday=prev_stay,
                                       floor_today=day == 1)
        ctx = make_context(
            config, day, agents, status, [s.stocks for s in config.spots], visited,
            weights=weights, forecast_status=next_status,
        )
        if iterations_per_day > 0:
            decoded = plan_day(ctx, deadline=deadline, seed=seed, max_iterations=iterations_per_day)
        elif seconds_per_day > 0:
            stop = time.monotonic() + seconds_per_day
            if deadline is not None:
                stop = min(stop, deadline)
            decoded = plan_day(ctx, deadline=stop, seed=seed)
        else:
            decoded = decode(ctx, construct(ctx, rng))
        result = decoded.result
        if not result.ok:
            break
        visited |= result.brands
        daily_types += len(result.brands)
        servings += result.servings
        agents = [AgentState(kinds[i], result.end_pos[i], result.end_fuel[i])
                  for i in range(config.n_agents)]
        # dự báo phải nhận HÔM NAY và HÔM QUA; truyền hôm nay hai lần thì
        # công thức hai ngày của luật thành vô nghĩa đúng lúc nó cần nhất
        status, prev_status = forecast.predict(
            status, prev_status, own_stay_today=result.stay, own_stay_yesterday=prev_stay,
            steps_today=config.steps_of_day(day), floor_today=day == 1), status
        prev_stay = result.stay
        days_done += 1
    return visited, daily_types, servings, days_done


def future_gain(
    config: MatchConfig,
    kinds: Sequence[int],
    forecast: TrafficForecast,
    day: int,
    result,
    visited_before: Set[int],
    status_today: Dict[int, int],
    status_yesterday: Optional[Dict[int, int]],
    stay_yesterday: Optional[Dict[int, int]],
    weights: Optional[Weights] = None,
    seed: int = 0,
    deadline: Optional[float] = None,
    max_days: Optional[int] = None,
) -> Tuple[int, int, int]:
    """(chuỗi mới, D, Q) mà các ngày SAU `day` thu thêm nếu hôm nay kết thúc như `result`.

    Chơi tiếp bằng bộ dựng tham lam, giao thông theo dự báo; `max_days` giới hạn số
    ngày chơi tiếp (None = tới hết trận). Ngày cuối trả (0, 0, 0).
    """
    if day + 1 >= config.n_days or not result.ok:
        return (0, 0, 0)
    visited = set(visited_before) | set(result.brands)
    agents = [AgentState(kinds[i], result.end_pos[i], result.end_fuel[i]) for i in range(config.n_agents)]
    status_next = forecast.predict(
        status_today, status_yesterday, own_stay_today=result.stay, own_stay_yesterday=stay_yesterday,
        steps_today=config.steps_of_day(day), floor_today=day == 1)
    final, daily, servings, _ = play_days(
        config, kinds, forecast, day + 1, agents, visited, status_next, status_today, result.stay,
        weights=weights, seed=seed, deadline=deadline, max_days=max_days)
    return (len(final) - len(visited), daily, servings)
