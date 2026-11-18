"""Tìm kiếm cục bộ bằng nước đi nhỏ và ủ mô phỏng (Bước 2, 18/11/2026).

Vì sao: trên server VN, L và D chạm trần mọi ngày nên Q (số phần) quyết định thắng thua.
Pha 1 của ALNS chấp nhận nghiệm kém hơn tới RRT_BAND × B = 250 điểm, tức Q đi ngẫu nhiên;
và ALNS chỉ có phá rồi chèn lại, không có nước đi sắp lại tour. Đội Nhật taiki288 (hub,
meet) dùng ủ mô phỏng trên nước đi nhỏ với nhiệt độ theo đơn vị phần, và đo được 62% cận
Q so với 50% của chèn tham lam.

Ở đây: từ một nghiệm, lặp
  1. chọn ngẫu nhiên một nước đi: đảo đoạn (2-opt), dời đoạn 1–3 điểm trong tour
     (or-opt), chuyển một điểm sang xe khác, đổi hai điểm giữa hai xe, thay spot (một
     nửa số lần bằng spot khác cùng chuỗi), bỏ một điểm, chèn một spot;
  2. lọc nhanh bằng số bước và xăng ước lượng theo bảng chặng (không mô phỏng);
  3. sau nước đi làm tour ngắn đi, chèn tham lam cho đầy lại chỗ trống;
  4. giải mã (mô phỏng thật) và chấp nhận theo ủ mô phỏng trên điểm, nhiệt độ tính
     bằng số phần (T0 → T1 giảm theo hàm mũ), và KHÔNG BAO GIỜ nhận nghiệm làm giảm
     (chuỗi mới, D) của hôm nay so với nghiệm xuất phát.

Điểm hẹn và lệnh đợi đi theo điểm dừng của chúng như alns._remove_stops; điểm hẹn của
điểm dừng bị chuyển sang xe khác thì bỏ. Mọi nghiệm vẫn giải mã ra bài hợp lệ.
"""

import math
import random
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .construct import InsertionState
from .decoder import Decoded, decode
from .objective import lexicographic_gain
from .solution import DayContext, DaySolution, Meet

INF = float("inf")
MOVES = ("two_opt", "or_opt", "relocate", "swap", "replace", "drop", "insert")
#: trọng số chọn nước đi (yss đo tỉ lệ thành công: bỏ điểm và sắp lại trong tour cao nhất)
MOVE_WEIGHTS = (3.0, 3.0, 2.0, 1.0, 2.0, 1.5, 1.5)
#: nhiệt độ đầu và cuối, tính theo số phần (× Weights.serving)
T_START = 2.0
T_END = 0.05
#: số nước đi không cải thiện nghiệm tốt nhất trước khi quay về nghiệm tốt nhất
RESTART_AFTER = 300


@dataclass
class LocalResult:
    solution: DaySolution
    decoded: Decoded
    moves: int
    accepted: int
    improved: int
    seconds: float


def _remap(sol: DaySolution, slot: int, mapping: Dict[int, Optional[int]]) -> None:
    """Đổi chỉ số điểm hẹn và lệnh đợi của tour `slot`: cũ -> mới (None = bỏ).

    Chỉ số không có trong mapping giữ nguyên; -1 (gặp ở ô xuất phát) luôn giữ.
    """
    if sol.meets:
        meets = []
        for m in sol.meets:
            if m.patrol != slot or m.stop_idx < 0 or m.stop_idx not in mapping:
                meets.append(m)
                continue
            new = mapping[m.stop_idx]
            if new is not None:
                meets.append(m if new == m.stop_idx else Meet(m.patrol, new, m.car))
        sol.meets = meets
    if sol.waits:
        waits = {}
        for (s, i), w in sol.waits.items():
            if s != slot or i < 0 or i not in mapping:
                waits[(s, i)] = w
                continue
            new = mapping[i]
            if new is not None:
                waits[(s, new)] = w
        sol.waits = waits


def _reorder(sol: DaySolution, slot: int, order: List[int]) -> None:
    """Đặt tour `slot` theo thứ tự chỉ số cũ `order` (hoán vị của một phần hay toàn bộ)."""
    tour = sol.tours[slot]
    kept = set(order)
    mapping: Dict[int, Optional[int]] = {old: new for new, old in enumerate(order)}
    for i in range(len(tour)):
        if i not in kept:
            mapping[i] = None
    sol.tours[slot] = [tour[i] for i in order]
    _remap(sol, slot, mapping)


def _insert_at(sol: DaySolution, slot: int, pos: int, spot: int) -> None:
    n = len(sol.tours[slot])
    mapping = {i: (i + 1 if i >= pos else i) for i in range(n)}
    sol.tours[slot].insert(pos, spot)
    _remap(sol, slot, mapping)


class _Estimator:
    """Ước lượng nhanh (bước, xăng) của một tour theo bảng chặng, để loại nước đi chắc chắn hỏng."""

    def __init__(self, ctx: DayContext):
        self.ctx = ctx
        has_car = ctx.n_cars > 0
        self.allowance = [ctx.fuel_of_patrol(s) + (ctx.config.fuel_limits if has_car else 0)
                          for s in range(ctx.n_patrols)]

    def tour(self, slot: int, tour: List[int]) -> Tuple[float, float]:
        ctx = self.ctx
        cur = ctx.start_of_patrol(slot)
        steps = fuel = 0.0
        for spot in tour:
            cell = ctx.spot_cells[spot]
            s, f = ctx.dist_step.steps_fuel(cur, cell)
            if s == INF:
                return INF, INF
            steps += s
            fuel += f
            cur = cell
        return steps, fuel

    def ok(self, slot: int, tour: List[int]) -> bool:
        s, f = self.tour(slot, tour)
        return s <= self.ctx.steps and f <= self.allowance[slot]


def _fill(ctx: DayContext, sol: DaySolution, rng: random.Random, max_inserts: int = 6) -> DaySolution:
    """Chèn tham lam vào chỗ vừa trống (ít lần chèn, để nước đi vẫn nhỏ)."""
    state = InsertionState(ctx, sol)
    done = state.insert_new_chains(max_inserts)
    while done < max_inserts:
        best = state.best_insertion(rng=rng)
        if best is None:
            break
        state.apply(*best)
        done += 1
    return state.sol


def _propose(ctx: DayContext, sol: DaySolution, move: str, rng: random.Random,
             est: _Estimator) -> Optional[Tuple[DaySolution, bool]]:
    """Một nước đi ngẫu nhiên. Trả (nghiệm mới, có nên chèn lấp) hoặc None nếu không làm được."""
    tours = sol.tours
    slots = [s for s, t in enumerate(tours) if t]
    if move == "insert":
        state = InsertionState(ctx, sol)
        best = state.best_insertion(rng=rng)
        if best is None:
            return None
        state.apply(*best)
        return state.sol, False
    if not slots:
        return None
    new = sol.copy()
    if move == "two_opt":
        slot = rng.choice(slots)
        n = len(tours[slot])
        if n < 2:
            return None
        a = rng.randrange(n - 1)
        b = rng.randrange(a + 1, n)
        order = list(range(n))
        order[a:b + 1] = order[a:b + 1][::-1]
        _reorder(new, slot, order)
        return (new, True) if est.ok(slot, new.tours[slot]) else None
    if move == "or_opt":
        slot = rng.choice(slots)
        n = len(tours[slot])
        if n < 2:
            return None
        k = rng.randint(1, min(3, n - 1))
        i = rng.randrange(n - k + 1)
        seg = list(range(i, i + k))
        rest = [x for x in range(n) if x < i or x >= i + k]
        p = rng.randrange(len(rest) + 1)
        if p == i:
            return None
        order = rest[:p] + seg + rest[p:]
        _reorder(new, slot, order)
        return (new, True) if est.ok(slot, new.tours[slot]) else None
    if move == "drop":
        slot = rng.choice(slots)
        n = len(tours[slot])
        i = rng.randrange(n)
        _reorder(new, slot, [x for x in range(n) if x != i])
        return new, True
    if move == "replace":
        slot = rng.choice(slots)
        n = len(tours[slot])
        i = rng.randrange(n)
        old = tours[slot][i]
        in_tour = set(tours[slot])
        if rng.random() < 0.5:
            pool = [s for s in ctx.config.spots_by_brand.get(ctx.spot_brand[old], ()) if s not in in_tour]
        else:
            prev = ctx.spot_cells[tours[slot][i - 1]] if i > 0 else ctx.start_of_patrol(slot)
            near = InsertionState(ctx, sol).near.of(prev)
            pool = [s for s in near if s not in in_tour]
        if not pool:
            return None
        new.tours[slot][i] = rng.choice(pool)
        return (new, True) if est.ok(slot, new.tours[slot]) else None
    if move in ("relocate", "swap"):
        if ctx.n_patrols < 2:
            return None
        s1 = rng.choice(slots)
        s2 = rng.choice([s for s in range(ctx.n_patrols) if s != s1])
        n1 = len(tours[s1])
        i = rng.randrange(n1)
        spot = tours[s1][i]
        if move == "relocate":
            if spot in tours[s2]:
                return None
            _reorder(new, s1, [x for x in range(n1) if x != i])
            p = rng.randrange(len(new.tours[s2]) + 1)
            _insert_at(new, s2, p, spot)
            if not est.ok(s2, new.tours[s2]):
                return None
            return new, True
        if not tours[s2]:
            return None
        j = rng.randrange(len(tours[s2]))
        other = tours[s2][j]
        if other in tours[s1] or spot in tours[s2]:
            return None
        new.tours[s1][i] = other
        new.tours[s2][j] = spot
        if not (est.ok(s1, new.tours[s1]) and est.ok(s2, new.tours[s2])):
            return None
        return new, True
    raise ValueError(move)


def local_search(
    ctx: DayContext,
    start: DaySolution,
    start_dec: Optional[Decoded] = None,
    deadline: Optional[float] = None,
    max_moves: int = 1000,
    seed: int = 0,
    accept: Optional[Callable[[Decoded], bool]] = None,
    on_better: Optional[Callable[[DaySolution, Decoded], None]] = None,
    stop: Optional[Callable[[Decoded], bool]] = None,
) -> LocalResult:
    """Ủ mô phỏng trên nước đi nhỏ, từ `start`. Không bao giờ trả nghiệm kém hơn `start`.

    `accept`, nếu có, loại hẳn nghiệm không đạt (vd khoá (chuỗi mới, D) của pha Q); mặc định
    khoá (chuỗi mới, D) của chính nghiệm xuất phát. Ngân sách là `deadline` (giây đơn điệu)
    hoặc `max_moves`, cái nào tới trước.
    """
    rng = random.Random((seed << 8) ^ (ctx.day + 1) ^ (77 << 24))
    began = time.monotonic()
    cur = start.copy()
    cur_dec = start_dec if start_dec is not None else decode(ctx, cur)
    best, best_dec = cur.copy(), cur_dec
    if accept is None:
        lock = lexicographic_gain(ctx, cur_dec.result)[:2] if cur_dec.result.ok else (0, 0)

        def accept(d: Decoded) -> bool:
            return d.result.ok and lexicographic_gain(ctx, d.result)[:2] >= lock
    est = _Estimator(ctx)
    unit = max(1e-9, ctx.weights.serving)
    moves = accepted = improved = 0
    since_best = 0
    total = max(1, max_moves)
    while moves < max_moves:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            break
        moves += 1
        if deadline is not None:
            frac = min(1.0, (now - began) / max(1e-6, deadline - began))
        else:
            frac = moves / total
        temp = unit * T_START * (T_END / T_START) ** frac
        move = rng.choices(MOVES, weights=MOVE_WEIGHTS)[0]
        prop = _propose(ctx, cur, move, rng, est)
        if prop is None:
            continue
        cand, refill = prop
        if refill:
            cand = _fill(ctx, cand, rng)
        cand_dec = decode(ctx, cand)
        if not accept(cand_dec):
            continue
        delta = cand_dec.score - cur_dec.score
        if delta >= 0 or rng.random() < math.exp(delta / temp):
            cur, cur_dec = cand, cand_dec
            accepted += 1
            if cand_dec.score > best_dec.score:
                best, best_dec = cand.copy(), cand_dec
                improved += 1
                since_best = 0
                if on_better is not None:
                    on_better(best, best_dec)
                if stop is not None and stop(best_dec):
                    break
                continue
        since_best += 1
        if since_best >= RESTART_AFTER:
            cur, cur_dec = best.copy(), best_dec
            since_best = 0
    return LocalResult(best, best_dec, moves, accepted, improved, time.monotonic() - began)
