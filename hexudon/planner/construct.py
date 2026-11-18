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


def neighbours(ctx: DayContext) -> SpotNeighbours:
    """Bảng spot gần nhất của ngữ cảnh, dựng một lần rồi dùng chung cho mọi phép sửa.

    Trước 18/11/2026 mỗi InsertionState dựng bảng mới nên mất bộ nhớ đệm sau mỗi vòng
    ALNS (605 nghìn lần gọi `of` cho 1.500 vòng trên bản đồ VN). Bảng chỉ phụ thuộc
    dist_step và vị trí spot, không phụ thuộc trọng số, nên kết quả không đổi.
    """
    near = ctx.__dict__.get("_near")
    if near is None:
        near = SpotNeighbours(ctx)
        ctx.__dict__["_near"] = near
    return near


class InsertionState:
    """Trạng thái kế toán để đánh giá phép chèn trong O(1)."""

    def __init__(self, ctx: DayContext, sol: Optional[DaySolution] = None):
        self.ctx = ctx
        self.sol = sol.copy() if sol is not None else ctx.empty_solution()
        self.near = neighbours(ctx)
        #: ô của từng điểm dừng theo tour, giữ khớp với self.sol.tours (chỉ apply sửa tour)
        self.cells: List[List[int]] = []
        self.allowance: List[float] = []
        self.tour_steps: List[float] = []
        self.tour_fuel: List[float] = []
        self.targeted: Dict[int, int] = {}
        self.covered: Dict[int, int] = {}
        #: R1: spot của chuỗi chưa từng thu trong trận và nghiệm chưa phủ; luôn là ứng viên
        self.new_chain = ctx.weights.new_chain_first > 0
        self._new_spots: Tuple[int, ...] = ()
        self._recompute()

    def _cells_of(self, slot: int) -> List[int]:
        """Ô của các điểm dừng trong tour `slot` (danh sách lưu sẵn, KHÔNG được sửa)."""
        return self.cells[slot]

    def _recompute(self):
        ctx = self.ctx
        has_car = ctx.n_cars > 0
        self.allowance = []
        self.tour_steps = []
        self.tour_fuel = []
        self.targeted = {}
        self.covered = {}
        self.cells = [[ctx.spot_cells[s] for s in tour] for tour in self.sol.tours]
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
        self._refresh_new_spots()

    def _refresh_new_spots(self) -> None:
        if not self.new_chain:
            return
        ctx = self.ctx
        out = []
        for brand, spots in ctx.config.spots_by_brand.items():
            if brand in ctx.visited_brands or self.covered.get(brand, 0) > 0:
                continue
            out.extend(spots)
        self._new_spots = tuple(out)

    def uncovered_new_brands(self) -> List[int]:
        """Chuỗi chưa từng thu trong trận mà nghiệm này chưa ghé."""
        ctx = self.ctx
        return [b for b in ctx.config.brands
                if b not in ctx.visited_brands and self.covered.get(b, 0) == 0]

    def gain_of(self, spot: int) -> float:
        ctx = self.ctx
        w = ctx.weights
        brand = ctx.spot_brand[spot]
        gain = 0.0
        if self.covered.get(brand, 0) == 0:
            gain += w.B
            if brand not in ctx.visited_brands:
                gain += ctx.new_brand_value
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
        self.cells[slot].insert(pos, self.ctx.spot_cells[spot])
        # điểm hẹn và lệnh đợi đi theo điểm dừng của chúng, không đứng yên ở chỉ số cũ
        if self.sol.meets:
            self.sol.meets = [Meet(m.patrol, m.stop_idx + 1, m.car)
                              if m.patrol == slot and m.stop_idx >= pos else m
                              for m in self.sol.meets]
        if self.sol.waits:
            self.sol.waits = {((s, i + 1) if s == slot and i >= pos else (s, i)): w
                              for (s, i), w in self.sol.waits.items()}
        self.tour_steps[slot] += dsteps
        self.tour_fuel[slot] += dfuel
        self.targeted[spot] = self.targeted.get(spot, 0) + 1
        brand = self.ctx.spot_brand[spot]
        self.covered[brand] = self.covered.get(brand, 0) + 1
        if self._new_spots and self.covered[brand] == 1:
            self._refresh_new_spots()

    def candidate_spots(self, slot: int, pos: int) -> Tuple[int, ...]:
        cells = self._cells_of(slot)
        anchor = cells[pos - 1] if pos > 0 else self.ctx.start_of_patrol(slot)
        near = self.near.of(anchor)
        if not self._new_spots:
            return near
        # spot của chuỗi mới cả trận luôn được xét, dù xa hơn NEAR_K spot gần nhất (R1)
        extra = tuple(s for s in self._new_spots if s not in near)
        return near + extra if extra else near

    def best_new_chain_insertion(self):
        """R1: chèn trước chuỗi mới cả trận khó chèn nhất (regret theo số bước).

        Mỗi chuỗi chưa thu: mọi phép chèn khả thi trên mọi spot của nó. Chuỗi chỉ còn
        một phương án có regret vô hạn; còn lại regret = Δbước thứ hai − Δbước tốt
        nhất. Trả phép chèn rẻ nhất của chuỗi có regret lớn nhất, hoặc None.
        """
        ctx = self.ctx
        in_tour = [set(t) for t in self.sol.tours]
        best = None
        best_key = None
        for brand in self.uncovered_new_brands():
            options = []
            for spot in ctx.config.spots_by_brand.get(brand, ()):
                for slot in range(ctx.n_patrols):
                    if spot in in_tour[slot]:
                        continue
                    for pos in range(len(self.sol.tours[slot]) + 1):
                        d = self.delta(slot, pos, spot)
                        if d is None:
                            continue
                        dsteps, dfuel = d
                        if not self.feasible(slot, dsteps, dfuel):
                            continue
                        options.append((dsteps, slot, pos, spot, dfuel))
            if not options:
                continue
            options.sort()
            regret = INF if len(options) == 1 else options[1][0] - options[0][0]
            key = (regret, -options[0][0])
            if best_key is None or key > best_key:
                dsteps, slot, pos, spot, dfuel = options[0]
                best_key = key
                best = (slot, pos, spot, dsteps, dfuel)
        return best

    def insert_new_chains(self, max_inserts: int) -> int:
        """Chèn hết chuỗi mới chèn được; trả số phép chèn đã làm."""
        if not self.new_chain:
            return 0
        done = 0
        while done < max_inserts:
            best = self.best_new_chain_insertion()
            if best is None:
                break
            self.apply(*best)
            done += 1
        return done

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
    inserts = state.insert_new_chains(max_inserts)
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
    inserts = state.insert_new_chains(max_inserts)
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
