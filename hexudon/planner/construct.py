"""Dựng nghiệm ban đầu bằng chèn tham lam, đánh giá O(1) qua bảng chặng."""

import random
from typing import Dict, List, Optional, Sequence, Tuple

from .decoder import decode
from .solution import DayContext, DaySolution, Meet

INF = float("inf")
NEAR_K = 10


class SpotNeighbours:
    """Danh sách spot gần nhất quanh một ô, tính lười và nhớ lại."""

    def __init__(self, ctx: DayContext, k: int = NEAR_K):
        self.ctx = ctx
        self.k = k
        self._cache: Dict[int, Tuple[int, ...]] = {}

    def of(self, cell: int) -> Tuple[int, ...]:
        cached = self._cache.get(cell)
        if cached is not None:
            return cached
        table = self.ctx.dist_step
        scored = []
        for si, spot_cell in enumerate(self.ctx.spot_cells):
            d = table.cost(cell, spot_cell)
            if d != INF:
                scored.append((d, si))
        scored.sort()
        out = tuple(si for _, si in scored[: self.k])
        self._cache[cell] = out
        return out


class InsertionState:
    """Trạng thái kế toán để đánh giá phép chèn trong O(1)."""

    def __init__(self, ctx: DayContext, sol: Optional[DaySolution] = None):
        self.ctx = ctx
        self.sol = sol.copy() if sol is not None else ctx.empty_solution()
        self.near = SpotNeighbours(ctx)
        self.allowance: List[float] = []
        self.tour_steps: List[float] = []
        self.tour_fuel: List[float] = []
        self.targeted: Dict[int, int] = {}
        self.covered: Dict[int, int] = {}
        self._recompute()

    def _cells_of(self, slot: int) -> List[int]:
        return [self.ctx.spot_cells[s] for s in self.sol.tours[slot]]

    def _recompute(self):
        ctx = self.ctx
        has_car = ctx.n_cars > 0
        self.allowance = []
        self.tour_steps = []
        self.tour_fuel = []
        self.targeted = {}
        self.covered = {}
        for slot in range(ctx.n_patrols):
            fuel0 = ctx.fuel_of_patrol(slot)
            self.allowance.append(fuel0 + (ctx.config.fuel_limits if has_car else 0))
            steps = 0.0
            fuel = 0.0
            cur = ctx.start_of_patrol(slot)
            for spot in self.sol.tours[slot]:
                cell = ctx.spot_cells[spot]
                s, f = ctx.dist_step.steps_fuel(cur, cell)
                if s == INF:
                    s, f = 0.0, 0.0
                steps += s
                fuel += f
                cur = cell
                self.targeted[spot] = self.targeted.get(spot, 0) + 1
                brand = ctx.spot_brand[spot]
                self.covered[brand] = self.covered.get(brand, 0) + 1
            self.tour_steps.append(steps)
            self.tour_fuel.append(fuel)

    def gain_of(self, spot: int) -> float:
        ctx = self.ctx
        w = ctx.weights
        brand = ctx.spot_brand[spot]
        gain = 0.0
        if self.covered.get(brand, 0) == 0:
            gain += w.B
            if brand not in ctx.visited_brands:
                gain += w.new_brand * w.B
        if ctx.stocks[spot] > self.targeted.get(spot, 0):
            gain += w.serving
        return gain

    def delta(self, slot: int, pos: int, spot: int) -> Optional[Tuple[float, float]]:
        """(Δbước, Δxăng) khi chèn spot vào vị trí pos của tour slot."""
        ctx = self.ctx
        cells = self._cells_of(slot)
        cell = ctx.spot_cells[spot]
        prev = cells[pos - 1] if pos > 0 else ctx.start_of_patrol(slot)
        s1, f1 = ctx.dist_step.steps_fuel(prev, cell)
        if s1 == INF:
            return None
        if pos < len(cells):
            nxt = cells[pos]
            s2, f2 = ctx.dist_step.steps_fuel(cell, nxt)
            s0, f0 = ctx.dist_step.steps_fuel(prev, nxt)
            if s2 == INF:
                return None
            return s1 + s2 - s0, f1 + f2 - f0
        return s1, f1

    def feasible(self, slot: int, dsteps: float, dfuel: float) -> bool:
        return (self.tour_steps[slot] + dsteps <= self.ctx.steps
                and self.tour_fuel[slot] + dfuel <= self.allowance[slot])

    def apply(self, slot: int, pos: int, spot: int, dsteps: float, dfuel: float):
        self.sol.tours[slot].insert(pos, spot)
        self.tour_steps[slot] += dsteps
        self.tour_fuel[slot] += dfuel
        self.targeted[spot] = self.targeted.get(spot, 0) + 1
        brand = self.ctx.spot_brand[spot]
        self.covered[brand] = self.covered.get(brand, 0) + 1

    def candidate_spots(self, slot: int, pos: int) -> Tuple[int, ...]:
        cells = self._cells_of(slot)
        anchor = cells[pos - 1] if pos > 0 else self.ctx.start_of_patrol(slot)
        return self.near.of(anchor)

    def best_insertion(self, rng: Optional[random.Random] = None,
                       coverage_first: bool = False, regret: int = 0):
        """Tìm phép chèn tốt nhất. regret>0 dùng chiến lược hối tiếc theo spot."""
        ctx = self.ctx
        in_tour = [set(t) for t in self.sol.tours]
        if regret > 0:
            return self._best_by_regret(in_tour, regret)
        best = None
        best_key = 0.0
        for slot in range(ctx.n_patrols):
            for pos in range(len(self.sol.tours[slot]) + 1):
                for spot in self.candidate_spots(slot, pos):
                    if spot in in_tour[slot]:
                        continue
                    gain = self.gain_of(spot)
                    if gain <= 0:
                        continue
                    if coverage_first and self.covered.get(ctx.spot_brand[spot], 0) > 0:
                        continue
                    d = self.delta(slot, pos, spot)
                    if d is None:
                        continue
                    dsteps, dfuel = d
                    if not self.feasible(slot, dsteps, dfuel):
                        continue
                    key = gain / max(1.0, dsteps)
                    if rng is not None:
                        key *= 1.0 + 0.05 * rng.random()
                    if key > best_key:
                        best_key = key
                        best = (slot, pos, spot, dsteps, dfuel)
        return best

    def _best_by_regret(self, in_tour, regret: int):
        ctx = self.ctx
        per_spot: Dict[int, List[Tuple[float, tuple]]] = {}
        for slot in range(ctx.n_patrols):
            for pos in range(len(self.sol.tours[slot]) + 1):
                for spot in self.candidate_spots(slot, pos):
                    if spot in in_tour[slot]:
                        continue
                    gain = self.gain_of(spot)
                    if gain <= 0:
                        continue
                    d = self.delta(slot, pos, spot)
                    if d is None:
                        continue
                    dsteps, dfuel = d
                    if not self.feasible(slot, dsteps, dfuel):
                        continue
                    key = gain / max(1.0, dsteps)
                    per_spot.setdefault(spot, []).append((key, (slot, pos, spot, dsteps, dfuel)))
        best = None
        best_regret = -INF
        for spot, options in per_spot.items():
            options.sort(key=lambda x: -x[0])
            top = options[0]
            others = options[1:regret]
            value = top[0] * len(options[:regret]) - sum(k for k, _ in others)
            if value > best_regret:
                best_regret = value
                best = top[1]
        return best


def add_fuel_meets(ctx: DayContext, sol: DaySolution) -> DaySolution:
    """Thêm điểm hẹn tiếp nhiên liệu cho xe tuần tra bị cắt vì thiếu xăng."""
    if ctx.n_cars == 0:
        return sol
    from .decoder import build_plan, walk_patrol

    current = sol.copy()
    base = decode(ctx, current).score
    for slot in range(ctx.n_patrols):
        if not current.tours[slot]:
            continue
        meet_map: Dict[int, int] = {}
        for m in current.meets:
            if m.patrol == slot:
                meet_map[m.stop_idx] = m.car
        walk = walk_patrol(ctx, current, slot, meet_map, current.waits)
        if not walk.truncated and walk.end_fuel > ctx.config.fuel_limits // 4:
            continue
        stops = sorted(i for i in walk.arrivals if i >= 0)
        anchors = [stops[-1]] if stops else [-1]
        if len(stops) >= 2:
            anchors.insert(0, stops[len(stops) // 2])
        anchors.append(-1)
        for stop_idx in anchors:
            if stop_idx in meet_map:
                continue
            cell = walk.stop_cell.get(stop_idx, ctx.start_of_patrol(slot))
            car = min(
                range(ctx.n_cars),
                key=lambda c: ctx.dist_step.cost(ctx.start_of_car(c), cell),
            )
            trial = current.copy()
            trial.meets.append(Meet(patrol=slot, stop_idx=stop_idx, car=car))
            score = decode(ctx, trial).score
            if score > base:
                current = trial
                base = score
                break
    return current


def construct(ctx: DayContext, rng: Optional[random.Random] = None,
              max_inserts: int = 400, coverage_first: bool = True) -> DaySolution:
    """Nghiệm khởi tạo: phủ chuỗi trước, rồi tham lam theo lợi ích trên chi phí."""
    state = InsertionState(ctx)
    inserts = 0
    if coverage_first:
        while inserts < max_inserts:
            best = state.best_insertion(rng=rng, coverage_first=True)
            if best is None:
                break
            state.apply(*best)
            inserts += 1
    while inserts < max_inserts:
        best = state.best_insertion(rng=rng)
        if best is None:
            break
        state.apply(*best)
        inserts += 1
    return add_fuel_meets(ctx, state.sol)


def repair(ctx: DayContext, sol: DaySolution, rng: random.Random, mode: str = "greedy",
           max_inserts: int = 200) -> DaySolution:
    """Sửa nghiệm sau khi phá: chèn lại cho tới khi hết chỗ."""
    state = InsertionState(ctx, sol)
    inserts = 0
    coverage_first = mode == "coverage"
    regret = 3 if mode == "regret" else 0
    if coverage_first:
        while inserts < max_inserts:
            best = state.best_insertion(rng=rng, coverage_first=True)
            if best is None:
                break
            state.apply(*best)
            inserts += 1
    while inserts < max_inserts:
        best = state.best_insertion(rng=rng, regret=regret)
        if best is None:
            break
        state.apply(*best)
        inserts += 1
    return state.sol
