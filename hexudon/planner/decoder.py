"""Bộ giải mã toàn phần: DaySolution -> kế hoạch hành động LUÔN hợp lệ.

Ba tính chất:
  1. Mọi nghiệm đều giải mã được thành kế hoạch hợp lệ; chặng không đủ xăng
     hoặc không đủ bước bị cắt, agent đợi hết ngày.
  2. Kết quả (vị trí, udon, xăng) lấy trực tiếp từ simulator, nên dự đoán của
     planner luôn khớp tuyệt đối với luật.
  3. Nếu vì lý do nào đó kế hoạch vẫn sai, hàm repair cắt đúng agent gây lỗi
     rồi thử lại, nên đầu ra không bao giờ là bài không hợp lệ.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .. import rules
from ..simulator import DayResult, run_day
from .objective import score_day
from .solution import DayContext, DaySolution, Meet

MAX_PATROL_WAIT_FOR_CAR = 8
MAX_ROUNDS = 4


@dataclass
class PatrolWalk:
    tokens: List[int] = field(default_factory=list)
    arrivals: Dict[int, int] = field(default_factory=dict)   # stop_idx -> bước tới nơi
    stop_cell: Dict[int, int] = field(default_factory=dict)  # stop_idx -> ô
    end_cell: int = 0
    end_fuel: int = 0
    end_time: int = 0
    truncated: bool = False
    visited_stops: int = 0


@dataclass
class Decoded:
    plan: List[List[int]]
    result: DayResult
    score: float
    truncated: int = 0
    dropped_meets: int = 0


def _emit_path(ctx: DayContext, start: int, path: Sequence[int], budget: int) -> Tuple[List[int], int, int]:
    """Đi theo dãy ô trong giới hạn bước. Trả (token, ô cuối, số bước dùng)."""
    tokens: List[int] = []
    cur = start
    used = 0
    dir_index = ctx.config.dir_index
    for nxt in path:
        cost = ctx.step_cost_of[cur]
        if cost <= 0 or used + cost > budget:
            break
        d = dir_index[cur].get(nxt)
        if d is None:
            break
        tokens.append(d)
        used += cost
        cur = nxt
    return tokens, cur, used


def _choose_leg(ctx: DayContext, cur: int, dst: int, time_left: int, fuel: int, reserve: int):
    """Chọn phương án chặng: ưu tiên ít bước, lùi dần về ít xăng, rồi bỏ dự trữ."""
    options = ctx.book.options(cur, dst)
    if not options:
        return None
    for keep_reserve in (True, False):
        need = reserve if keep_reserve else 0
        for opt in options:
            if opt.steps <= time_left and fuel - opt.fuel >= need:
                return opt
    return None


def walk_patrol(ctx: DayContext, sol: DaySolution, slot: int,
                meet_stops: Dict[int, int], waits: Dict[Tuple[int, int], int]) -> PatrolWalk:
    """Đi hết tour của một xe tuần tra, cắt khi thiếu bước hoặc thiếu xăng."""
    agent = ctx.patrol_agent(slot)
    cur = ctx.agents[agent].pos
    fuel = ctx.agents[agent].fuel
    steps = ctx.steps
    walk = PatrolWalk()
    t = 0

    if -1 in meet_stops:
        w = max(1, waits.get((slot, -1), 1))
        w = min(w, steps)
        if w > 0:
            walk.tokens.append(-w)
            t += w
        walk.arrivals[-1] = 0
        walk.stop_cell[-1] = cur
        fuel = ctx.config.fuel_limits

    tour = sol.tours[slot]
    for i, spot_idx in enumerate(tour):
        dst = ctx.spot_cells[spot_idx]
        if dst == cur:
            walk.arrivals[i] = t
            walk.stop_cell[i] = cur
            walk.visited_stops += 1
            continue
        reserve = 0
        if i not in meet_stops and i + 1 < len(tour):
            nxt = ctx.spot_cells[tour[i + 1]]
            cheapest = ctx.book.min_fuel(dst, nxt)
            reserve = int(cheapest) if cheapest != float("inf") else 0
        opt = _choose_leg(ctx, cur, dst, steps - t, fuel, reserve)
        if opt is None:
            walk.truncated = True
            break
        tokens, cur_after, used = _emit_path(ctx, cur, opt.path, steps - t)
        if cur_after != dst:
            walk.truncated = True
            walk.tokens.extend(tokens)
            t += used
            cur = cur_after
            break
        walk.tokens.extend(tokens)
        t += used
        fuel -= opt.fuel
        cur = dst
        walk.arrivals[i] = t
        walk.stop_cell[i] = cur
        walk.visited_stops += 1
        if i in meet_stops:
            fuel = ctx.config.fuel_limits
        w = waits.get((slot, i), 0)
        if w > 0:
            w = min(w, steps - t)
            if w > 0:
                walk.tokens.append(-w)
                t += w

    walk.end_cell = cur
    walk.end_fuel = fuel
    walk.end_time = t
    return walk


def _window(ctx: DayContext, walk: PatrolWalk, sol: DaySolution, slot: int, stop_idx: int,
            waits: Dict[Tuple[int, int], int]) -> Optional[Tuple[int, int, int]]:
    """Cửa sổ (ô, bước sớm nhất, bước muộn nhất) xe tiếp có thể gặp xe tuần tra."""
    if stop_idx not in walk.arrivals:
        return None
    cell = walk.stop_cell[stop_idx]
    start = walk.arrivals[stop_idx]
    if stop_idx == -1:
        return cell, 0, max(0, waits.get((slot, -1), 1) - 1) if waits.get((slot, -1), 1) > 0 else 0
    tour = sol.tours[slot]
    is_last = stop_idx >= len(tour) - 1 or (stop_idx + 1) not in walk.arrivals
    if is_last:
        return cell, start, ctx.steps
    wait = waits.get((slot, stop_idx), 0)
    depart_cost = ctx.step_cost_of[cell]
    return cell, start, start + wait + max(1, depart_cost) - 1


def route_cars(ctx: DayContext, sol: DaySolution, walks: List[PatrolWalk],
               alive: List[Meet], waits: Dict[Tuple[int, int], int], allow_wait: bool):
    """Định tuyến xe tiếp qua các điểm hẹn; bỏ điểm hẹn không kịp."""
    kept: List[Meet] = []
    dropped = 0
    changed = False
    car_plans: Dict[int, List[int]] = {}
    new_waits = dict(waits)

    by_car: Dict[int, List[Meet]] = {}
    for m in alive:
        if m.patrol >= len(walks):
            dropped += 1
            changed = True
            continue
        by_car.setdefault(m.car, []).append(m)

    for car_slot in range(ctx.n_cars):
        agent = ctx.car_agent(car_slot)
        cur = ctx.agents[agent].pos
        t = 0
        tokens: List[int] = []
        meets = by_car.get(car_slot, [])
        ordered = []
        for m in meets:
            win = _window(ctx, walks[m.patrol], sol, m.patrol, m.stop_idx, new_waits)
            if win is None:
                dropped += 1
                changed = True
                continue
            ordered.append((win[1], m, win))
        ordered.sort(key=lambda x: x[0])

        for _, m, (cell, win_start, win_end) in ordered:
            path = ctx.dist_step.path(cur, cell) if cur != cell else []
            travel = 0
            probe = cur
            for nxt in path:
                travel += ctx.step_cost_of[probe]
                probe = nxt
            arrive = t + travel
            if arrive > win_end:
                late = arrive - win_end
                patrol_end = walks[m.patrol].end_time
                if (allow_wait and late <= MAX_PATROL_WAIT_FOR_CAR
                        and patrol_end + late <= ctx.steps):
                    key = (m.patrol, m.stop_idx)
                    new_waits[key] = new_waits.get(key, 0) + late
                    changed = True
                    kept.append(m)
                    continue
                dropped += 1
                changed = True
                continue
            if arrive > ctx.steps:
                dropped += 1
                changed = True
                continue
            moved, cur_after, used = _emit_path(ctx, cur, path, ctx.steps - t)
            if cur_after != cell:
                dropped += 1
                changed = True
                tokens.extend(moved)
                t += used
                cur = cur_after
                continue
            tokens.extend(moved)
            t += used
            cur = cell
            if t < win_start:
                pause = min(win_start - t, ctx.steps - t)
                if pause > 0:
                    tokens.append(-pause)
                    t += pause
            kept.append(m)

        # sau điểm hẹn cuối: tiến về xe tuần tra thiếu xăng nhất
        if t < ctx.steps and walks:
            needy = min(range(len(walks)), key=lambda s: walks[s].end_fuel)
            target = walks[needy].end_cell
            if target != cur:
                path = ctx.dist_step.path(cur, target)
                moved, cur, used = _emit_path(ctx, cur, path, ctx.steps - t)
                tokens.extend(moved)
                t += used
        if t < ctx.steps:
            tokens.append(-(ctx.steps - t))
        car_plans[car_slot] = tokens

    return kept, new_waits, car_plans, dropped, changed


def build_plan(ctx: DayContext, sol: DaySolution):
    """Dựng kế hoạch hành động cho cả đội."""
    waits = dict(sol.waits)
    alive = list(sol.meets)
    walks: List[PatrolWalk] = []
    car_plans: Dict[int, List[int]] = {}
    dropped_total = 0

    for rnd in range(MAX_ROUNDS):
        meet_by_patrol: List[Dict[int, int]] = [{} for _ in range(ctx.n_patrols)]
        for m in alive:
            if m.patrol < ctx.n_patrols:
                meet_by_patrol[m.patrol][m.stop_idx] = m.car
        walks = [walk_patrol(ctx, sol, s, meet_by_patrol[s], waits) for s in range(ctx.n_patrols)]
        alive, waits, car_plans, dropped, changed = route_cars(
            ctx, sol, walks, alive, waits, allow_wait=(rnd < MAX_ROUNDS - 1)
        )
        dropped_total += dropped
        if not changed:
            break

    plan: List[List[int]] = [[] for _ in ctx.agents]
    truncated = 0
    for slot, walk in enumerate(walks):
        agent = ctx.patrol_agent(slot)
        tokens = list(walk.tokens)
        if walk.end_time < ctx.steps:
            tokens.append(-(ctx.steps - walk.end_time))
        plan[agent] = tokens if tokens else [-ctx.steps]
        if walk.truncated:
            truncated += 1
    for slot in range(ctx.n_cars):
        agent = ctx.car_agent(slot)
        tokens = car_plans.get(slot) or [-ctx.steps]
        plan[agent] = tokens
    return plan, truncated, dropped_total


def repair_to_valid(ctx: DayContext, plan: List[List[int]]) -> Tuple[List[List[int]], DayResult, int]:
    """Chạy simulator; nếu sai thì cắt đúng agent gây lỗi rồi thử lại."""
    cuts = 0
    for _ in range(len(ctx.agents) * 4 + 4):
        result = run_day(ctx.config, ctx.steps, ctx.agents, plan, ctx.stocks, ctx.road_status)
        if result.ok:
            return plan, result, cuts
        err = result.error
        cuts += 1
        if err is None or err.agent < 0:
            plan = [[-ctx.steps] for _ in ctx.agents]
            continue
        i = err.agent
        k = max(0, err.token_index)
        issued = err.step if err.step >= 0 else 0
        prefix = list(plan[i][:k])
        rest = ctx.steps - issued
        if rest > 0:
            prefix.append(-rest)
        plan = list(plan)
        plan[i] = prefix if prefix else [-ctx.steps]
    return (
        [[-ctx.steps] for _ in ctx.agents],
        run_day(ctx.config, ctx.steps, ctx.agents, [[-ctx.steps] for _ in ctx.agents], ctx.stocks, ctx.road_status),
        cuts,
    )


def decode(ctx: DayContext, sol: DaySolution) -> Decoded:
    """Giải mã nghiệm thành kế hoạch hợp lệ kèm điểm."""
    plan, truncated, dropped = build_plan(ctx, sol)
    plan, result, cuts = repair_to_valid(ctx, plan)
    score = score_day(ctx, result, truncated=truncated + cuts, dropped_meets=dropped)
    return Decoded(plan=plan, result=result, score=score, truncated=truncated + cuts, dropped_meets=dropped)
