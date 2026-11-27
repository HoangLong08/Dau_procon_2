"""Điều phối lập kế hoạch một ngày: dựng ngữ cảnh, bài an toàn, rồi ALNS."""

import dataclasses
import time
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from .. import rules
from ..model import AgentState, MatchConfig
from ..pathing import DayGraph, DistTable, LegBook
from .alns import Elite, _remove_stops, improve
from .construct import InsertionState, construct
from .decoder import Decoded, decode
from .local import local_search
from .objective import lexicographic_gain, score_day
from .solution import DayContext, DaySolution, Weights, build_context

#: hạng mục cuối ngày "mềm", bị nhân Weights.q_end_scale trong pha Q (R2); các hạng
#: mục giữ L và D tương lai (cover_penalty, strand, deadline) giữ nguyên
SOFT_END_TERMS = ("urgency", "end_on_spot", "near_chain", "fuel_bonus", "road_park", "value_weight")


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


def q_phase_weights(w: Weights) -> Weights:
    """Trọng số của pha Q.

    Các hạng mục cuối ngày mềm nhân q_end_scale. Nếu q_B > 0 thì B thu về q_B: phép
    chèn và chấm điểm gần như chỉ còn số phần, các hạng mục mềm (tính theo B) teo
    theo; các hạng mục giữ L và D tương lai được nhân bù B / q_B để giữ nguyên độ lớn.
    """
    q = dataclasses.replace(w, **{k: getattr(w, k) * w.q_end_scale for k in SOFT_END_TERMS})
    if w.q_B > 0 and w.q_B < w.B:
        keep = w.B / w.q_B
        q = dataclasses.replace(q, B=w.q_B, cover_penalty=w.cover_penalty * keep,
                                strand=w.strand * keep, deadline=w.deadline * keep)
    return q


def rescore(ctx: DayContext, decoded: Decoded) -> Decoded:
    """Chấm lại một bài theo trọng số của ctx (bài của pha Q mang điểm theo trọng số khác)."""
    if not decoded.result.ok:
        return decoded
    score = score_day(ctx, decoded.result, truncated=decoded.truncated, dropped_meets=decoded.dropped_meets)
    return Decoded(plan=decoded.plan, result=decoded.result, score=score,
                   truncated=decoded.truncated, dropped_meets=decoded.dropped_meets)


def ensure_new_chains(ctx: DayContext, sol: DaySolution, decoded: Decoded) -> Tuple[DaySolution, Decoded]:
    """R1: vá L. Chuỗi mới cả trận mà bài bỏ qua dù một xe tuần tra tới được hôm nay
    thì thử chèn spot của nó vào từng tour (cắt bớt đuôi tour nếu không vừa).

    Chỉ nhận biến thể thật sự thu thêm chuỗi mới và có điểm cao hơn; thưởng chuỗi mới
    (new_brand_value) đã lớn hơn mọi D trong ngày nên không cần trọng số riêng.
    """
    if ctx.weights.new_chain_first <= 0 or not decoded.result.ok:
        return sol, decoded
    cfg = ctx.config
    missing = [b for b in cfg.brands if b not in ctx.visited_brands and b not in decoded.result.brands]
    best_sol, best_dec = sol, decoded
    for brand in missing:
        base_new = lexicographic_gain(ctx, best_dec.result)[0]
        found: Optional[Tuple[DaySolution, Decoded]] = None
        for spot in cfg.spots_by_brand.get(brand, ()):
            cell = ctx.spot_cells[spot]
            for slot in range(ctx.n_patrols):
                if ctx.dist_step.cost(ctx.start_of_patrol(slot), cell) > ctx.steps:
                    continue
                tour_len = len(best_sol.tours[slot])
                for keep in range(tour_len, -1, -1):
                    trial = best_sol.copy()
                    if keep < tour_len:
                        trial = _remove_stops(trial, [(slot, i) for i in range(keep, tour_len)])
                    if spot in trial.tours[slot]:
                        continue
                    state = InsertionState(ctx, trial)
                    options = []
                    for pos in range(len(trial.tours[slot]) + 1):
                        d = state.delta(slot, pos, spot)
                        if d is not None and state.feasible(slot, d[0], d[1]):
                            options.append((d[0], pos, d[1]))
                    if not options:
                        continue
                    dsteps, pos, dfuel = min(options)
                    state.apply(slot, pos, spot, dsteps, dfuel)
                    dec = decode(ctx, state.sol)
                    if not dec.result.ok or brand not in dec.result.brands:
                        continue
                    if lexicographic_gain(ctx, dec.result)[0] <= base_new:
                        continue
                    if found is None or dec.score > found[1].score:
                        found = (state.sol, dec)
                    break  # cắt đuôi ít nhất có thể cho tour này
        if found is not None and found[1].score > best_dec.score:
            best_sol, best_dec = found
    return best_sol, best_dec


def ensure_today_chains(ctx: DayContext, sol: DaySolution, decoded: Decoded,
                        deadline: Optional[float] = None) -> Tuple[DaySolution, Decoded]:
    """Vá D hôm nay: chuỗi chưa thu hôm nay mà một xe tuần tra tới được thì thử chèn.

    Như ensure_new_chains nhưng cho mọi chuỗi thiếu HÔM NAY (đã thu trong trận cũng tính),
    và để lấy chỗ được bỏ MỘT điểm trùng chuỗi (chuỗi của nó vẫn còn điểm dừng khác) ở bất
    kỳ vị trí nào của tour, hoặc cắt đuôi tour. Chỉ nhận biến thể làm (chuỗi mới, D) của hôm
    nay tăng thật; trong các biến thể đó chọn điểm cao nhất. Hết giờ thì trả nghiệm tốt nhất
    đã có.
    """
    if ctx.weights.today_chain_first <= 0 or not decoded.result.ok:
        return sol, decoded
    cfg = ctx.config
    best_sol, best_dec = sol, decoded
    missing = [b for b in cfg.brands if b not in decoded.result.brands]
    for brand in missing:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if brand in best_dec.result.brands:
            continue
        base = lexicographic_gain(ctx, best_dec.result)[:2]
        found: Optional[Tuple[DaySolution, Decoded]] = None
        counts: Dict[int, int] = {}
        for tour in best_sol.tours:
            for sp in tour:
                b = ctx.spot_brand[sp]
                counts[b] = counts.get(b, 0) + 1
        for spot in cfg.spots_by_brand.get(brand, ()):
            cell = ctx.spot_cells[spot]
            for slot in range(ctx.n_patrols):
                if ctx.dist_step.cost(ctx.start_of_patrol(slot), cell) > ctx.steps:
                    continue
                tour = best_sol.tours[slot]
                if spot in tour:
                    continue
                # cách lấy chỗ: không bỏ gì; bỏ một điểm trùng chuỗi; cắt đuôi dần
                removals = [()]
                removals += [(i,) for i, sp in enumerate(tour) if counts.get(ctx.spot_brand[sp], 0) >= 2]
                removals += [tuple(range(keep, len(tour))) for keep in range(len(tour) - 1, -1, -1)]
                seen = set()
                for gone in removals:
                    if gone in seen:
                        continue
                    seen.add(gone)
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    trial = best_sol.copy()
                    if gone:
                        trial = _remove_stops(trial, [(slot, i) for i in gone])
                    state = InsertionState(ctx, trial)
                    options = []
                    for pos in range(len(trial.tours[slot]) + 1):
                        d = state.delta(slot, pos, spot)
                        if d is not None and state.feasible(slot, d[0], d[1]):
                            options.append((d[0], pos, d[1]))
                    if not options:
                        continue
                    dsteps, pos, dfuel = min(options)
                    state.apply(slot, pos, spot, dsteps, dfuel)
                    dec = decode(ctx, state.sol)
                    if not dec.result.ok or lexicographic_gain(ctx, dec.result)[:2] <= base:
                        continue
                    if found is None or dec.score > found[1].score:
                        found = (state.sol, dec)
                    if not gone or len(gone) == 1:
                        continue
                    break  # cắt đuôi ít nhất có thể cho tour này
        if found is not None and found[1].score > best_dec.score:
            best_sol, best_dec = found
    return best_sol, best_dec


def day_upper_bound(ctx: DayContext) -> Tuple[int, int, int]:
    """Cận trên (chuỗi mới, D, Q) của hôm nay (R3).

    Một spot tới được nếu một xe tuần tra đi tới trong số bước của ngày, và (khi đội
    không có xe tiếp) đủ xăng theo đường ít xăng nhất. Q ≤ Σ min(kho, số xe tới được),
    vì mỗi xe thu tối đa một phần mỗi spot mỗi ngày.
    """
    has_car = ctx.n_cars > 0
    reach = [0] * len(ctx.spot_cells)
    for slot in range(ctx.n_patrols):
        start = ctx.start_of_patrol(slot)
        fuel = ctx.fuel_of_patrol(slot)
        for si, cell in enumerate(ctx.spot_cells):
            if ctx.dist_step.cost(start, cell) > ctx.steps:
                continue
            if not has_car and ctx.book.min_fuel(start, cell) > fuel:
                continue
            reach[si] += 1
    brands = {ctx.spot_brand[si] for si, n in enumerate(reach) if n}
    servings = sum(min(ctx.stocks[si], n) for si, n in enumerate(reach))
    return (len(brands - ctx.visited_brands), len(brands), servings)


def variant_weights(w: Weights) -> List[Weights]:
    """Trọng số của hai lượt ALNS biến thể tạo ứng viên cho rollout (R4), như E2:
    tiết kiệm xăng (fuel_bonus × 2) và coi trọng cuối ngày (các hạng mục cuối ngày × 2)."""
    end_x2 = {k: getattr(w, k) * 2 for k in SOFT_END_TERMS + ("strand",)}
    return [dataclasses.replace(w, fuel_bonus=w.fuel_bonus * 2), dataclasses.replace(w, **end_x2)]


def today_hard_len(ctx: DayContext, results: Sequence = ()) -> int:
    """Số phần đầu của (chuỗi mới, D, Q) hôm nay mà rollout/chọn seed không được hạ: 1 (chỉ L),
    hoặc 2 (L và D) ở ngày D an toàn khi bật rollout_d_hard. Với rollout_d_streak > 0 thì còn
    cần một bài trong `results` đạt trần D hôm nay (đủ mọi chuỗi của bản đồ); phần "mọi ngày
    trước đều đạt trần" do strategist kiểm (MatchStrategist.day_weights)."""
    w = ctx.weights
    if w.rollout_d_hard <= 0 or w.soft_unit <= 0:
        return 1
    if w.rollout_d_streak > 0:
        n = len(ctx.config.brands)
        if not any(r.ok and lexicographic_gain(ctx, r)[1] >= n for r in results):
            return 1
    return 2


def pick_by_lookahead(ctx: DayContext, best_sol: DaySolution, best_dec: Decoded, elite: Optional[Elite],
                      lookahead: Callable[[Decoded], Tuple[int, int, int]],
                      deadline: Optional[float] = None,
                      extra: Sequence[Tuple[DaySolution, Decoded]] = ()) -> Tuple[DaySolution, Decoded, str, dict]:
    """R4: trong nghiệm tốt nhất và các nghiệm của `elite`, chọn nghiệm có (chuỗi mới, D, Q)
    của hôm nay cộng `lookahead` (các ngày sau) lớn nhất; hoà thì theo điểm.

    Không nhận nghiệm có ít chuỗi mới hôm nay hơn nghiệm tốt nhất (L cứng, R1); ở ngày D an
    toàn với rollout_d_hard thì cả D cũng vậy. Hết giờ giữa chừng thì chọn trong số đã chấm.
    Trả thêm "rollout" nếu đổi khỏi nghiệm tốt nhất, và info = {"worst": giây của lần chơi
    tiếp chậm nhất, "total": tổng của bài được chọn}.
    """
    hard = today_hard_len(ctx, [best_dec.result])
    base = lexicographic_gain(ctx, best_dec.result)[:hard]
    cands = [(best_sol, best_dec)]
    seen = {tuple(tuple(seq) for seq in best_dec.plan)}
    pool = list(extra) + (elite.entries() if elite is not None else [])
    for sol, dec in pool:
        if not dec.result.ok or lexicographic_gain(ctx, dec.result)[:hard] < base:
            continue
        key = tuple(tuple(seq) for seq in dec.plan)
        if key in seen:
            continue
        seen.add(key)
        cands.append((sol, dec))
    info = {"worst": 0.0, "total": None}
    if len(cands) == 1:
        return best_sol, best_dec, "", info
    scored = []
    for idx, (sol, dec) in enumerate(cands):
        # chỉ chấm thêm khi còn kịp một lần chơi tiếp nữa (đo bằng lần chậm nhất đã chạy)
        if idx > 0 and deadline is not None and time.monotonic() + info["worst"] >= deadline:
            break
        t0 = time.monotonic()
        future = lookahead(dec)
        info["worst"] = max(info["worst"], time.monotonic() - t0)
        today = lexicographic_gain(ctx, dec.result)
        total = tuple(a + b for a, b in zip(today, future))
        scored.append((total, dec.score, -idx, sol, dec))
    total, _, neg_idx, sol, dec = max(scored, key=lambda x: (x[0], x[1], x[2]))
    info["total"] = total
    if neg_idx == 0:
        return best_sol, best_dec, "", info
    return sol, dec, "rollout", info


#: chẩn đoán (thi_nghiem_ai/chan_doan_q_ngay.py): đặt một dict thì plan_day và rust_start ghi
#: (L, D, Q) hôm nay sau từng bước vào đó. None (mặc định) thì không ghi gì, không đổi hành vi
TRACE: Optional[dict] = None


def _trace(key: str, value) -> None:
    if TRACE is not None:
        TRACE[key] = value


def fuel_ready(ctx: DayContext, decoded: Decoded) -> int:
    """Xăng sẵn cho ngày mai: tổng min(xăng cuối ngày, số bước ngày mai) của các xe tuần tra
    (0 ở ngày cuối)."""
    if ctx.next_steps <= 0 or not decoded.result.ok:
        return 0
    return sum(min(f, ctx.next_steps) for a, f in zip(ctx.agents, decoded.result.end_fuel)
               if a.kind == rules.PATROL)


def rust_start(ctx: DayContext, sol: DaySolution, decoded: Decoded, deadline: Optional[float],
               better: Callable[[DaySolution, Decoded], None], seed: int = 0,
               lookahead: Optional[Callable[[Decoded], Tuple[int, int, int]]] = None) -> Tuple[DaySolution, Decoded]:
    """Tour của bộ giải Rust (rust/tour), giải mã, thêm điểm hẹn và vá L/D; trả nó nếu hơn bài
    đang có theo score_day, không thì giữ nguyên. Không có file nhị phân thì không làm gì.

    Có `lookahead` (R4, không phải ngày cuối) thì còn phải không kém bài đang có theo (L, D, Q)
    hôm nay cộng các ngày sau khi chơi tiếp, và (rs_ready) không để ít xăng cho ngày mai hơn
    (fuel_ready): bộ giải Rust chỉ tối đa hôm nay, nên trên bản đồ xăng ít (vòng 1 Nhật,
    25/11) nó lấy thêm 1 chuỗi ngày 0 mà để hai xe còn 1 và 6 xăng cho ngày 1 có 36 bước."""
    from . import rust_tours
    from .orienteering import add_meets

    if not rust_tours.available():
        return sol, decoded
    w = ctx.weights
    secs = w.rs_seconds
    if deadline is not None:
        secs = min(secs, w.rs_share * max(0.0, deadline - time.monotonic()))
    if secs <= 0.05:
        return sol, decoded
    tours = rust_tours.solve(ctx, secs, seed=seed + 1, cap_fuel=w.rs_fuel > 0)
    if tours is None:
        return sol, decoded
    cand = DaySolution(tours=tours, meets=[], waits={})
    if w.rs_fuel > 0:
        dec = decode(ctx, cand)
    else:
        cand, dec = add_meets(ctx, cand, deadline)
    if not dec.result.ok:
        return sol, decoded
    cand, dec = ensure_new_chains(ctx, cand, dec)
    cand, dec = ensure_today_chains(ctx, cand, dec, deadline=deadline)
    if TRACE is not None:
        TRACE["rs"] = {"gain": list(lexicographic_gain(ctx, dec.result)) if dec.result.ok else None,
                       "fuel_ready": [fuel_ready(ctx, dec), fuel_ready(ctx, decoded)]}
    if dec.score <= decoded.score:
        lex = (w.rs_day0_lex > 0 and ctx.day == 0 and decoded.result.ok
               and lexicographic_gain(ctx, dec.result) > lexicographic_gain(ctx, decoded.result))
        if not lex:
            _trace("rs_ket_qua", "điểm không cao hơn")
            return sol, decoded
    # ngày D an toàn: rs_safe_free bỏ hai điều kiện giữ D, chỉ còn so điểm; rs_safe_ld giữ chúng
    # nhưng chỉ so L, D của chơi tiếp, và bỏ rs_ready khi một bình đủ cho mọi ngày còn lại
    d_safe = w.soft_unit > 0 and w.soft_auto > 0
    safe = d_safe and w.rs_safe_free > 0
    ld = d_safe and not safe and w.rs_safe_ld > 0
    cfg = ctx.config
    fuel_free = ld and cfg.fuel_limits >= max(cfg.day_steps[ctx.day + 1:], default=0)
    if not safe and not fuel_free and w.rs_ready > 0 and fuel_ready(ctx, dec) < fuel_ready(ctx, decoded):
        _trace("rs_ket_qua", "rs_ready")
        return sol, decoded
    if not safe and lookahead is not None and decoded.result.ok and dec.result.ok:
        mine = lexicographic_gain(ctx, dec.result)
        theirs = lexicographic_gain(ctx, decoded.result)
        hard = today_hard_len(ctx, [decoded.result, dec.result])
        if mine[:hard] < theirs[:hard]:
            _trace("rs_ket_qua", "kém L/D")
            return sol, decoded
        total_r = tuple(a + b for a, b in zip(mine, lookahead(dec)))
        total_s = tuple(a + b for a, b in zip(theirs, lookahead(decoded)))
        _trace("rs_look", [list(total_r), list(total_s)])
        if ld:
            total_r, total_s = total_r[:2], total_s[:2]     # (chuỗi mới, D): Q đã so ở score_day
        if total_r < total_s:
            _trace("rs_ket_qua", "rs_look")
            return sol, decoded
    _trace("rs_ket_qua", "nhận")
    better(cand, dec)
    return cand, dec


def orienteering_start(ctx: DayContext, sol: DaySolution, decoded: Decoded, deadline: Optional[float],
                       better: Callable[[DaySolution, Decoded], None]) -> Tuple[DaySolution, Decoded]:
    """Bước E: tour orienteering tối ưu của từng xe (MILP), giải mã và vá L/D; trả nó nếu hơn bài
    đang có theo score_day, không thì giữ nguyên."""
    from .orienteering import exact_tours

    w = ctx.weights
    secs = w.or_seconds
    if deadline is not None:
        span = max(0.0, deadline - time.monotonic())
        if span < w.or_min_seconds:   # ngày ngắn: ALNS dùng trọn ngân sách thì hơn
            return sol, decoded
        secs = min(secs, w.or_share * span)
    if secs <= 0.05:
        return sol, decoded
    seed_sol = exact_tours(ctx, secs, k_near=int(w.or_k_near), cap_fuel=w.or_fuel > 0, base=sol)
    if seed_sol is None:
        return sol, decoded
    if w.or_fuel > 0:
        dec = decode(ctx, seed_sol)
    else:
        from .orienteering import add_meets
        seed_sol, dec = add_meets(ctx, seed_sol, deadline)
    if not dec.result.ok:
        return sol, decoded
    seed_sol, dec = ensure_new_chains(ctx, seed_sol, dec)
    seed_sol, dec = ensure_today_chains(ctx, seed_sol, dec, deadline=deadline)
    if dec.score > decoded.score:
        better(seed_sol, dec)
        return seed_sol, dec
    return sol, decoded


def plan_day(
    ctx: DayContext,
    deadline: Optional[float] = None,
    seed: int = 0,
    on_plan: Optional[Callable[[Decoded], None]] = None,
    max_iterations: int = 1_000_000,
    on_better: Optional[Callable[[Decoded], None]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    on_improve: Optional[Callable[[Decoded], None]] = None,
    lookahead: Optional[Callable[[Decoded], Tuple[int, int, int]]] = None,
) -> Decoded:
    """Lập kế hoạch một ngày: gửi bài an toàn trước, rồi cải thiện tới hạn chót.

    on_plan được gọi với bài an toàn, rồi với bài cuối nếu nó tốt hơn bài an toàn.
    on_improve được gọi ngay khi ALNS tìm ra nghiệm tốt hơn hẳn mọi nghiệm trước
    đó, để bên gọi tự quyết có gửi sớm không: thời gian phản hồi tính theo bài
    hợp lệ cuối cùng (cases 11.8). Hai móc còn lại chỉ để quan sát: on_better
    nhận đúng các nghiệm như on_improve; on_progress chuyển thẳng cho ALNS.

    Hai pha (R2, 16/11/2026): pha 1 là ALNS như cũ trên (1 − q_share) ngân sách, rồi
    vá L (ensure_new_chains). Pha 2 khoá (chuỗi mới, D) hôm nay của bài tốt nhất và
    tìm thêm phần (Q) với dải chấp nhận tính theo phần, các hạng mục cuối ngày mềm
    nhân q_end_scale. Trong pha 2, on_improve chỉ được gọi khi (L, D, Q) hôm nay
    tăng thật, để không gửi lại vì một thay đổi điểm nội bộ. Bài trả về luôn mang
    điểm theo trọng số của ctx. q_share = 0 là đúng hành vi cũ.

    `lookahead` (R4), nếu có và không phải ngày cuối: giữa pha 1 và pha Q, chọn lại
    trong K nghiệm khác nhau về trạng thái cuối ngày theo kết quả chơi tiếp các ngày
    sau (pick_by_lookahead), với rollout_share ngân sách giây. Bài chọn như vậy mang
    picked_by = "rollout". Ngày cuối, đạt cận trên của hôm nay thì dừng ngay (R3).

    ALNS bắt đầu từ chính nghiệm an toàn, nên bài cuối không bao giờ kém nó.
    """
    sol, first = safe_solution(ctx, seed)
    sol, first = ensure_new_chains(ctx, sol, first)
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

    w = ctx.weights
    #: điểm xuất phát của ALNS: bài an toàn, hoặc bài an toàn đã vá D hôm nay
    start_sol, start_dec = sol, first
    if w.today_chain_first > 0:
        # vá D hôm nay SAU khi đã gửi bài an toàn (không làm chậm lần gửi đầu)
        patched_sol, patched = ensure_today_chains(ctx, sol, first, deadline=deadline)
        if patched is not first:
            start_sol, start_dec = patched_sol, patched
            better(start_sol, start_dec)
    if w.rs_seconds > 0:
        look_rs = lookahead if (w.rs_look > 0 and w.rollout_k >= 1 and ctx.days_left > 0) else None
        start_sol, start_dec = rust_start(ctx, start_sol, start_dec, deadline, better, seed=seed,
                                          lookahead=look_rs)
    _trace("truoc_or", list(lexicographic_gain(ctx, start_dec.result)) if start_dec.result.ok else None)
    if w.or_seconds > 0:
        start_sol, start_dec = orienteering_start(ctx, start_sol, start_dec, deadline, better)
    _trace("xuat_phat", list(lexicographic_gain(ctx, start_dec.result)) if start_dec.result.ok else None)
    # R3: ngày cuối end_value = 0, nên đạt cận trên của hôm nay là xong: dừng ngay để bài
    # cuối được gửi sớm (thời gian phản hồi là tiêu chí 4). Ngày khác vẫn tìm tiếp, vì
    # trạng thái cuối ngày còn cải thiện được và D, Q các ngày sau xếp trên thời gian.
    stop = None
    if ctx.days_left == 0 and w.ub_stop > 0:
        ub = day_upper_bound(ctx)
        if start_dec.result.ok and lexicographic_gain(ctx, start_dec.result) == ub:
            if on_plan is not None and start_dec is not first:
                on_plan(start_dec)
            return start_dec

        def stop(decoded: Decoded) -> bool:
            return decoded.result.ok and lexicographic_gain(ctx, decoded.result) == ub

    share = min(0.9, max(0.0, w.q_share))
    use_roll = lookahead is not None and w.rollout_k >= 1 and ctx.days_left > 0
    share_r = min(max(0.0, w.rollout_share), 0.9 - share) if use_roll else 0.0
    now = time.monotonic()
    span = None if deadline is None else max(0.0, deadline - now)
    # vòng ALNS: pha 1 | hai lượt biến thể (R4, nửa share_r) | pha Q (share)
    iters_var = int(round(max_iterations * share_r / 4)) if use_roll else 0
    if share > 0 or iters_var:
        iters1 = max(1, int(round(max_iterations * (1.0 - share))) - 2 * iters_var)
    else:
        iters1 = max_iterations
    if share > 0 or share_r > 0:
        deadline1 = None if deadline is None else now + (1.0 - share - share_r) * span
    else:
        deadline1 = deadline
    elite = Elite(ctx, int(w.rollout_k)) if use_roll else None
    result = improve(
        ctx,
        start=start_sol,
        deadline=deadline1,
        max_iterations=iters1,
        seed=seed,
        on_better=better,
        on_progress=on_progress,
        elite=elite,
        stop=stop,
    )
    if result.decoded.score > start_dec.score:
        best_sol, best_dec = result.solution, result.decoded
    else:
        best_sol, best_dec = start_sol, start_dec
    patched_sol, patched = ensure_new_chains(ctx, best_sol, best_dec)
    patched_sol, patched = ensure_today_chains(ctx, patched_sol, patched, deadline=deadline)
    if patched is not best_dec:
        best_sol, best_dec = patched_sol, patched
        better(best_sol, best_dec)
    reached = stop is not None and stop(best_dec)
    _trace("pha1", list(lexicographic_gain(ctx, best_dec.result)) if best_dec.result.ok else None)

    # R4: chọn trong các nghiệm khác nhau về trạng thái cuối ngày theo (L, D, Q) của hôm
    # nay cộng các ngày sau khi chơi tiếp bằng bộ dựng tham lam
    picked = ""
    roll = {"worst": 0.0, "total": None}
    #: (bài, tổng hôm nay + các ngày sau) của bài đã được chơi tiếp chấm (R4)
    known = None
    if use_roll and best_dec.result.ok and not reached:
        extra = []
        base_new = lexicographic_gain(ctx, best_dec.result)[0]
        for i, wv in enumerate(variant_weights(w)):
            dl = None if span is None else now + (1.0 - share - share_r + (i + 1) * share_r / 4) * span
            if (dl is not None and time.monotonic() >= dl) or (dl is None and iters_var <= 0):
                continue
            res_v = improve(dataclasses.replace(ctx, weights=wv), start=best_sol, deadline=dl,
                            max_iterations=iters_var if iters_var > 0 else max_iterations,
                            seed=seed, rng_salt=2 + i,
                            accept=lambda d: d.result.ok and lexicographic_gain(ctx, d.result)[0] >= base_new)
            if res_v.decoded.result.ok:
                extra.append((res_v.solution, rescore(ctx, res_v.decoded)))
        deadline_r = None if span is None else now + (1.0 - share) * span
        best_sol, best_dec, picked, roll = pick_by_lookahead(ctx, best_sol, best_dec, elite, lookahead,
                                                             deadline_r, extra=extra)
        _trace("r4", {"gain": list(lexicographic_gain(ctx, best_dec.result)) if best_dec.result.ok else None, "doi": bool(picked)})
        if roll["total"] is not None:
            known = (best_dec.plan, roll["total"])

    iters2 = max_iterations - iters1 - 2 * iters_var
    # Bước 2: tìm kiếm cục bộ lấy ls_share của ngân sách từ pha Q (cùng khoá chuỗi mới, D)
    ls = min(max(0.0, w.ls_share), share) if share > 0 else 0.0
    iters_ls = int(round(iters2 * ls / share)) if ls > 0 else 0
    iters_q = iters2 - iters_ls
    time_left = deadline is None or time.monotonic() < deadline
    if share > 0 and iters2 > 0 and time_left and best_dec.result.ok and not reached:
        lock = lexicographic_gain(ctx, best_dec.result)[:2]
        ctx_q = dataclasses.replace(ctx, weights=q_phase_weights(w))
        base_q = score_day(ctx_q, best_dec.result, truncated=best_dec.truncated,
                           dropped_meets=best_dec.dropped_meets)
        reported = {"gain": lexicographic_gain(ctx, best_dec.result)}

        def better_q(_sol, decoded: Decoded):
            gain = lexicographic_gain(ctx, decoded.result)
            if gain > reported["gain"]:
                reported["gain"] = gain
                rescored = rescore(ctx, decoded)
                if on_better is not None:
                    on_better(rescored)
                if on_improve is not None:
                    on_improve(rescored)

        def keeps_lock(decoded: Decoded) -> bool:
            return decoded.result.ok and lexicographic_gain(ctx, decoded.result)[:2] >= lock

        # nếu rollout đã chọn bài, chừa đủ giờ để chấm lại bài pha Q bằng một lần chơi tiếp
        deadline_q = deadline if (deadline is None or not picked) else deadline - roll["worst"]
        deadline_alns = deadline_q
        if ls > 0 and deadline_q is not None:
            deadline_alns = deadline_q - (ls / share) * max(0.0, deadline_q - time.monotonic())
        res_q = improve(
            ctx_q,
            start=best_sol,
            deadline=deadline_alns,
            max_iterations=max(1, iters_q),
            seed=seed,
            on_better=better_q,
            on_progress=on_progress,
            band=w.q_band * w.serving,
            accept=keeps_lock,
            rng_salt=1,
            stop=stop,
        )
        if ls > 0 and (deadline_q is None or time.monotonic() < deadline_q):
            # Bước 2: ủ mô phỏng trên nước đi nhỏ, từ nghiệm tốt nhất của pha Q
            if keeps_lock(res_q.decoded) and res_q.decoded.score > base_q:
                ls_start, ls_dec = res_q.solution, res_q.decoded
            else:
                ls_start = best_sol
                ls_dec = rescore(ctx_q, best_dec) if best_dec.result.ok else best_dec
            res_ls = local_search(
                ctx_q, ls_start, start_dec=ls_dec, deadline=deadline_q,
                max_moves=max(1, int(round(iters_ls * max(0.0, w.ls_moves_per_iter)))),
                seed=seed, accept=keeps_lock, on_better=better_q, stop=stop)
            if res_ls.decoded.score > res_q.decoded.score or not keeps_lock(res_q.decoded):
                res_q = res_ls
        if keeps_lock(res_q.decoded) and res_q.decoded.score > base_q:
            polished = rescore(ctx, res_q.decoded)
            keep = True
            if picked and lookahead is not None and polished.plan != best_dec.plan:
                # pha Q có thể dời trạng thái cuối ngày mà rollout vừa chọn: chấm lại bài pha Q
                # (tổng của bài rollout đã có sẵn); hết giờ thì giữ bài rollout
                if deadline is not None and time.monotonic() + roll["worst"] >= deadline:
                    keep = False
                else:
                    g1 = lexicographic_gain(ctx, polished.result)
                    total1 = tuple(a + b for a, b in zip(g1, lookahead(polished)))
                    keep = roll["total"] is None or total1 >= roll["total"]
                    if keep:
                        known = (polished.plan, total1)
            if keep:
                best_sol, best_dec = res_q.solution, polished

    final = best_dec
    final.picked_by = picked
    _trace("cuoi", list(lexicographic_gain(ctx, final.result)) if final.result.ok else None)
    if known is not None and known[0] == final.plan:
        final.total = known[1]
    if on_plan is not None and final is not first:
        on_plan(final)
    return final
