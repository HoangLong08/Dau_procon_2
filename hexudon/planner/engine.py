"""Điều phối lập kế hoạch một ngày: dựng ngữ cảnh, bài an toàn, rồi ALNS."""

import dataclasses
import time
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from ..model import AgentState, MatchConfig
from ..pathing import DayGraph, DistTable, LegBook
from .alns import _remove_stops, improve
from .construct import InsertionState, construct
from .decoder import Decoded, decode
from .objective import lexicographic_gain, score_day
from .solution import DayContext, DaySolution, Weights, build_context

#: hạng mục cuối ngày "mềm", bị nhân Weights.q_end_scale trong pha Q (R2); các hạng
#: mục giữ L và D tương lai (cover_penalty, strand, deadline) giữ nguyên
SOFT_END_TERMS = ("urgency", "end_on_spot", "near_chain", "fuel_bonus", "road_park")


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

    Hai pha (R2, 16/11/2026): pha 1 là ALNS như cũ trên (1 − q_share) ngân sách, rồi
    vá L (ensure_new_chains). Pha 2 khoá (chuỗi mới, D) hôm nay của bài tốt nhất và
    tìm thêm phần (Q) với dải chấp nhận tính theo phần, các hạng mục cuối ngày mềm
    nhân q_end_scale. Trong pha 2, on_improve chỉ được gọi khi (L, D, Q) hôm nay
    tăng thật, để không gửi lại vì một thay đổi điểm nội bộ. Bài trả về luôn mang
    điểm theo trọng số của ctx. q_share = 0 là đúng hành vi cũ.

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
    share = min(0.9, max(0.0, w.q_share))
    if share > 0:
        iters1 = max(1, int(round(max_iterations * (1.0 - share))))
        now = time.monotonic()
        deadline1 = None if deadline is None else now + (1.0 - share) * max(0.0, deadline - now)
    else:
        iters1, deadline1 = max_iterations, deadline
    result = improve(
        ctx,
        start=sol,
        deadline=deadline1,
        max_iterations=iters1,
        seed=seed,
        on_better=better,
        on_progress=on_progress,
    )
    if result.decoded.score > first.score:
        best_sol, best_dec = result.solution, result.decoded
    else:
        best_sol, best_dec = sol, first
    patched_sol, patched = ensure_new_chains(ctx, best_sol, best_dec)
    if patched is not best_dec:
        best_sol, best_dec = patched_sol, patched
        better(best_sol, best_dec)

    iters2 = max_iterations - iters1
    time_left = deadline is None or time.monotonic() < deadline
    if share > 0 and iters2 > 0 and time_left and best_dec.result.ok:
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

        res_q = improve(
            ctx_q,
            start=best_sol,
            deadline=deadline,
            max_iterations=iters2,
            seed=seed,
            on_better=better_q,
            on_progress=on_progress,
            band=w.q_band * w.serving,
            accept=keeps_lock,
            rng_salt=1,
        )
        if keeps_lock(res_q.decoded) and res_q.decoded.score > base_q:
            best_sol, best_dec = res_q.solution, rescore(ctx, res_q.decoded)

    final = best_dec
    if on_plan is not None and final is not first:
        on_plan(final)
    return final
