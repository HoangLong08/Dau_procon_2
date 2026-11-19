"""Bước E (19/11/2026): orienteering CHÍNH XÁC cho từng xe tuần tra, làm điểm xuất phát của ALNS.

Mỗi xe chỉ thu MỘT phần ở mỗi spot mỗi ngày (đã kiểm trên 352 lần quay lại trong replay thật),
và trên bản đồ kiểu server VN kho mỗi spot = số xe ≥ số xe tuần tra, nên các xe không tranh
kho của nhau: số phần trong ngày = Σ theo xe (số spot khác nhau xe đó ghé). Bài toán trong ngày
tách thành một bài orienteering cho mỗi xe (tối đa số spot ghé được trong `steps` bước), chỉ
ràng nhau qua phủ đủ chuỗi (D, L) và xe tiếp xăng.

Từ đúng trạng thái đầu ngày của DAU 2 trên 4 trận contest 17/11, lời giải tối ưu của các bài
orienteering này bằng ĐÚNG số phần DAU 2 đạt ở 5/6 ngày thử (80, 120, 118, 91, 92, 59), còn
ALNS của ta kém 4–8 phần mỗi ngày dù có 20 giây. Ở đây giải từng xe bằng MILP (scipy, HiGHS):

  nút 0 = ô xuất phát, 1..N = spot còn kho (bỏ spot trùng ô xuất phát), N+1 = đích ảo;
  x_ij nhị phân trên cung (i → j), y_j = ghé spot j; ra khỏi 0 đúng 1, vào đích đúng 1,
  vào j = ra j = y_j; Σ bước ≤ steps; Σ xăng ≤ xăng hiện có (tuỳ chọn);
  thời điểm tới u_j ≥ u_i + bước_ij khi x_ij = 1 (MTZ theo thời gian, khử chu trình);
  max Σ y_j.

Chặng i → j dùng đường ít bước nhất của DistTable (bước và xăng của đúng đường đó). Spot đi
ngang qua trên đường không được tính trong MILP (bộ giải mã vẫn thu chúng), nên giá trị MILP là
cận dưới của số phần thật của tour. Bài ra được giải mã, vá L/D như mọi nghiệm khác; ALNS chỉ
xuất phát từ nó khi nó tốt hơn theo score_day, nên không bao giờ làm bài kém đi.
"""

import time
from typing import Iterable, List, Optional, Sequence

from .solution import DayContext, DaySolution, Meet

INF = float("inf")


def available() -> bool:
    try:
        from scipy.optimize import milp  # noqa: F401
    except Exception:  # noqa: BLE001 — scipy không có hoặc quá cũ
        return False
    return True


def solve_patrol(ctx: DayContext, slot: int, time_limit: float, k_near: int = 0,
                 fuel_cap: Optional[float] = None, required_brands: Iterable[int] = (),
                 skip: Iterable[int] = (), min_count: int = 0) -> Optional[List[int]]:
    """Tour tối ưu (dãy chỉ số spot) của xe tuần tra thứ `slot`; None nếu không giải được.

    k_near > 0: mỗi nút chỉ nối tới k_near spot gần nhất (nhanh hơn, có thể mất tối ưu).
    required_brands: mỗi chuỗi trong đó phải có ít nhất một spot trong tour.
    min_count > 0: chỉ nhận tour có ít nhất ngần này spot (None nếu hết giờ chưa tìm được).
    """
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix

    agent = ctx.agents[ctx.patrols[slot]]
    start = agent.pos
    T = ctx.steps
    table = ctx.dist_step
    skip = set(skip)
    cand = [k for k, c in enumerate(ctx.spot_cells)
            if c != start and k not in skip and ctx.stocks[k] > 0 and table.cost(start, c) <= T]
    if not cand:
        return []
    N = len(cand)
    cells = [start] + [ctx.spot_cells[k] for k in cand]
    sink = N + 1
    #: bước ngắn nhất từ ô xuất phát (cung i -> j chỉ dùng được nếu d0[i] + bước_ij <= T)
    d0 = [0.0] + [float(table.cost(start, c)) for c in cells[1:]]
    arcs = []  # (i, j, bước, xăng)
    for i in range(N + 1):
        out = []
        for j in range(1, N + 1):
            if i == j:
                continue
            steps, fuel = table.steps_fuel(cells[i], cells[j])
            if d0[i] + steps <= T:
                out.append((steps, j, fuel))
        if k_near > 0 and i > 0:
            out.sort()
            out = out[:k_near]
        for steps, j, fuel in out:
            arcs.append((i, j, float(steps), float(fuel)))
        if i > 0:
            arcs.append((i, sink, 0.0, 0.0))
    arcs.append((0, sink, 0.0, 0.0))
    A = len(arcs)
    nv = A + 2 * N
    Y0, U0 = A, A + N
    # chỉ tối đa số spot: thêm ε × số bước để ưu tiên tour ngắn làm HiGHS chậm 3–10 lần (đo 19/11)
    c = np.zeros(nv)
    c[Y0:Y0 + N] = -1.0

    rows, cols, vals, lo, hi = [], [], [], [], []
    r = 0

    def add(entries, lb, ub):
        nonlocal r
        for col, v in entries:
            rows.append(r)
            cols.append(col)
            vals.append(v)
        lo.append(lb)
        hi.append(ub)
        r += 1

    out_of = [[] for _ in range(N + 2)]
    into = [[] for _ in range(N + 2)]
    for a, (i, j, _, _) in enumerate(arcs):
        out_of[i].append(a)
        into[j].append(a)
    add([(a, 1.0) for a in out_of[0]], 1.0, 1.0)
    add([(a, 1.0) for a in into[sink]], 1.0, 1.0)
    for k in range(1, N + 1):
        add([(a, 1.0) for a in into[k]] + [(Y0 + k - 1, -1.0)], 0.0, 0.0)
        add([(a, 1.0) for a in out_of[k]] + [(Y0 + k - 1, -1.0)], 0.0, 0.0)
    add([(a, arcs[a][2]) for a in range(A)], -INF, float(T))
    if fuel_cap is not None:
        add([(a, arcs[a][3]) for a in range(A) if arcs[a][3] > 0], -INF, float(fuel_cap))
    big = float(T) + max((a[2] for a in arcs), default=0.0) + 1.0
    for a, (i, j, steps, _) in enumerate(arcs):
        if j == sink:
            continue
        entries = [(U0 + j - 1, 1.0), (a, -big)]
        if i > 0:
            entries.append((U0 + i - 1, -1.0))
        add(entries, steps - big, INF)
    if min_count > 0:
        if min_count > N:
            return None
        add([(Y0 + n, 1.0) for n in range(N)], float(min_count), INF)
    brand_of = ctx.spot_brand
    for b in set(required_brands):
        cols_b = [Y0 + n for n, k in enumerate(cand) if brand_of[k] == b]
        if not cols_b:
            return None
        add([(col, 1.0) for col in cols_b], 1.0, INF)

    M = coo_matrix((vals, (rows, cols)), shape=(r, nv)).tocsr()
    lb = np.zeros(nv)
    ub = np.ones(nv)
    ub[U0:U0 + N] = float(T)
    integrality = np.zeros(nv)
    integrality[:A + N] = 1
    res = milp(c, constraints=LinearConstraint(M, lo, hi), integrality=integrality,
               bounds=Bounds(lb, ub), options={"time_limit": max(0.05, time_limit), "disp": False})
    if res.x is None:
        return None
    x = res.x
    nxt = {}
    for a, (i, j, _, _) in enumerate(arcs):
        if x[a] > 0.5:
            nxt[i] = j
    tour, cur = [], 0
    while True:
        j = nxt.get(cur)
        if j is None or j == sink or len(tour) > N:
            break
        tour.append(cand[j - 1])
        cur = j
    return tour


def _covered(ctx: DayContext, tours: Sequence[Sequence[int]]) -> set:
    got = set()
    spot_at = ctx.config.spot_index_at
    for slot, tour in enumerate(tours):
        start = ctx.agents[ctx.patrols[slot]].pos
        if start in spot_at:
            got.add(ctx.spot_brand[spot_at[start]])
        got.update(ctx.spot_brand[k] for k in tour)
    return got


def exact_tours(ctx: DayContext, seconds: float, k_near: int = 0,
                cap_fuel: bool = True, base: Optional[DaySolution] = None) -> Optional[DaySolution]:
    """Nghiệm gồm tour orienteering tối ưu của từng xe tuần tra, trong `seconds`.

    Có `base` thì mỗi xe chỉ nhận tour MILP có nhiều spot hơn hẳn tour của nó trong base (ràng
    buộc Σ y ≥ cũ + 1); không tìm được trong giờ thì giữ tour cũ cùng các điểm hẹn của nó. Nghiệm
    tạm của HiGHS lúc hết giờ có khi rất tệ (đo 19/11: 9 spot trong khi tối ưu là 22).
    Nếu các tour chưa phủ đủ chuỗi, giải lại một xe (xe nhiều spot nhất trước) với ràng buộc
    phủ các chuỗi thiếu. Trả None nếu không có scipy hoặc không xe nào giải được.
    """
    if not ctx.patrols or not available():
        return None
    from concurrent.futures import ThreadPoolExecutor

    end = time.monotonic() + seconds
    P = len(ctx.patrols)
    # mỗi xe một luồng (HiGHS nhả GIL khi giải: 4 xe song song thật, đo 19/11), chừa 20% thời
    # gian cho bước phủ chuỗi
    tl = 0.8 * seconds

    def floor(slot: int) -> int:
        return len(base.tours[slot]) + 1 if base is not None else 0

    def one(slot: int) -> Optional[List[int]]:
        fuel = ctx.agents[ctx.patrols[slot]].fuel if cap_fuel else None
        return solve_patrol(ctx, slot, tl, k_near=k_near, fuel_cap=fuel, min_count=floor(slot))

    with ThreadPoolExecutor(max_workers=P) as pool:
        found = list(pool.map(one, range(P)))
    kept = [t is None and base is not None for t in found]
    tours: List[List[int]] = [list(base.tours[s]) if kept[s] else (found[s] or []) for s in range(P)]
    meets = [m for m in base.meets if kept[m.patrol]] if base is not None else []
    waits = {k: v for k, v in base.waits.items() if kept[k[0]]} if base is not None else {}
    if base is not None and all(kept):
        return None
    if not any(tours):
        return None
    missing = set(ctx.config.brands) - _covered(ctx, tours)
    if missing:
        order = sorted(range(P), key=lambda s: -len(tours[s]))
        for slot in order:
            left = end - time.monotonic()
            if left <= 0.05:
                break
            if kept[slot]:
                continue
            fuel = ctx.agents[ctx.patrols[slot]].fuel if cap_fuel else None
            need = set(ctx.config.brands) - _covered(ctx, [[] if s == slot else tours[s] for s in range(P)])
            t = solve_patrol(ctx, slot, left / 2, k_near=k_near, fuel_cap=fuel, required_brands=need)
            if t is not None:
                tours[slot] = t
                if not set(ctx.config.brands) - _covered(ctx, tours):
                    break
    return DaySolution(tours=tours, meets=meets, waits=waits)


def add_meets(ctx: DayContext, sol: DaySolution, deadline: Optional[float] = None, rounds: int = 4):
    """Điểm hẹn xe tiếp xăng cho các tour bị cắt vì thiếu xăng: mỗi lượt thử mọi (xe tuần tra bị
    cắt, điểm dừng, xe tiếp) và giữ điểm hẹn làm score_day tăng nhiều nhất. Trả (nghiệm, bài)."""
    from .decoder import decode, walk_patrol

    cur = sol.copy()
    base = decode(ctx, cur)
    if ctx.n_cars == 0:
        return cur, base
    for _ in range(rounds):
        best = None
        for slot in range(ctx.n_patrols):
            meet_map = {m.stop_idx: m.car for m in cur.meets if m.patrol == slot}
            if not walk_patrol(ctx, cur, slot, meet_map, cur.waits).truncated:
                continue
            for idx in [-1] + list(range(len(cur.tours[slot]))):
                if idx in meet_map:
                    continue
                for car in range(ctx.n_cars):
                    trial = cur.copy()
                    trial.meets.append(Meet(patrol=slot, stop_idx=idx, car=car))
                    d = decode(ctx, trial)
                    if d.result.ok and d.score > (best[0] if best else base.score):
                        best = (d.score, trial, d)
                if deadline is not None and time.monotonic() > deadline:
                    break
        if best is None:
            break
        cur, base = best[1], best[2]
    return cur, base
