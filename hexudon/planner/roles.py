"""Chọn vai trò xe tuần tra / xe tiếp nhiên liệu trước ngày 0.

Đây là quyết định không sửa được sau khi trận bắt đầu và có ảnh hưởng lớn nhất
tới điểm cuối: thiếu xe tiếp thì xe tuần tra cạn xăng từ ngày 2 và số chuỗi mỗi
ngày sụp đổ; thừa xe tiếp thì thiếu xe thu udon.

Cách làm: chơi thử CẢ TRẬN cho từng phương án bằng bộ dựng nghiệm tham lam
(nhanh và tất định), rồi nếu còn thời gian thì đánh giá lại hai phương án dẫn
đầu bằng tìm kiếm ALNS thật.
"""

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .. import rules
from ..model import AgentState, MatchConfig
from ..pathing import DayGraph, DistTable
from ..traffic import PRIOR, TrafficForecast
from .lookahead import play_days
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
    iterations_per_day: int = 0,
):
    """Chơi thử cả trận một mình với trạng thái đường dự báo. Trả (L, D, Q).

    seconds_per_day = 0 nghĩa là chỉ dùng bộ dựng nghiệm tham lam (tất định, ~5 ms/ngày).
    `iterations_per_day` > 0 chạy ALNS đúng ngần ấy vòng mỗi ngày thay vì theo giây:
    tất định, dùng cho benchmark (vẫn dừng ở `deadline` nếu có).
    `with_days=True` trả thêm số ngày thực sự chơi được: một mẫu bị cắt giữa
    chừng KHÔNG so sánh được với mẫu chơi đủ, nên người gọi phải loại nó ra.
    """
    agents = [AgentState(kinds[i], config.agent_starts[i], config.fuel_limits)
              for i in range(config.n_agents)]
    status = {c: rules.SMOOTH for c in config.road_cells}
    visited, daily_types, servings, days_done = play_days(
        config, kinds, forecast, 0, agents, set(), status, None, None, weights=weights, seed=seed,
        deadline=deadline, seconds_per_day=seconds_per_day, iterations_per_day=iterations_per_day)
    score = (len(visited), daily_types, servings)
    return (score, days_done) if with_days else score


#: vòng tinh chỉnh chỉ mở nhiều tiến trình khi còn ít nhất ngần này giây (R5)
PARALLEL_MIN_SECONDS = 6.0
#: giờ dành cho việc mở tiến trình con (spawn trên Windows nạp lại cả package)
POOL_STARTUP_SECONDS = 1.5
#: tiến trình con phải trả kết quả trước hạn của cha ngần này giây
POOL_RETURN_MARGIN = 0.3


def role_scenarios(config: MatchConfig, weights: Optional[Weights] = None) -> List[Tuple[str, TrafficForecast]]:
    """Các kịch bản giao thông dùng để chấm một phương án vai trò (R5).

    "cũ": dự báo trước R5 (gần như chơi một mình). "nhiều đội": mọi đội đều chơi,
    lưu lượng theo traffic.PRIOR. Ở sảnh 10 đội ngưỡng thấp, từ ngày 3 hơn nửa số ô
    đường đông hoặc tắc; xe đi đồng bằng thì đỡ xăng, nên cần ít xe tiếp hơn hẳn
    con số mà kịch bản "cũ" đòi (báo cáo 4.2.5, bản đồ Nhật trận 3).
    """
    w = weights or Weights()
    light = TrafficForecast(config, n_patrols=max(1, config.n_agents - 1))
    if w.role_traffic <= 0:
        return [("cũ", light)]
    heavy = light.with_model(PRIOR)
    if w.role_traffic >= 2:
        return [("nhiều đội", heavy)]
    return [("cũ", light), ("nhiều đội", heavy)]


def zero_cars_allowed(config: MatchConfig) -> bool:
    """0 xe tiếp chỉ được xét khi xăng dư hẳn và trận ngắn (báo cáo 5.1, bảng 4.3: −14,7 D)."""
    return config.fuel_limits >= 3 * config.steps_of_day(0) and config.n_days <= 5


def _total(parts: Sequence[Tuple[int, int, int]]) -> Tuple[int, int, int]:
    return tuple(sum(p[i] for p in parts) for i in range(3))


def _refine_job(job: dict):
    """Việc của một tiến trình con: chơi thử cả trận bằng ALNS theo giây. Trả (điểm, số ngày)."""
    left = job["wall_deadline"] - time.time()
    deadline = time.monotonic() + max(0.0, left)
    return rollout(job["config"], job["kinds"], job["forecast"], weights=job["weights"],
                   seed=job["seed"], deadline=deadline, seconds_per_day=job["per_day"], with_days=True)


def _run_parallel(work: List[dict], workers: int, hard_deadline: float):
    """Chạy các việc trên Pool; trả kết quả theo thứ tự (None nếu lỡ hạn), hoặc None nếu Pool hỏng."""
    import multiprocessing

    try:
        pool = multiprocessing.get_context("spawn").Pool(workers)
    except Exception:
        return None
    try:
        pending = [pool.apply_async(_refine_job, (job,)) for job in work]
        out = []
        for res in pending:
            left = hard_deadline - time.monotonic()
            try:
                out.append(res.get(timeout=max(0.01, left)))
            except Exception:       # hết giờ hoặc tiến trình con lỗi: bỏ việc đó
                out.append(None)
        return out
    finally:
        pool.terminate()


def select_roles(
    config: MatchConfig,
    budget_seconds: float = 3.0,
    weights: Optional[Weights] = None,
    seed: int = 0,
    log: Optional[Callable[[str], None]] = None,
    on_candidate: Optional[Callable[[dict], None]] = None,
    iterations_per_day: Optional[int] = None,
    jobs: Optional[int] = None,
    parallel_min_seconds: float = PARALLEL_MIN_SECONDS,
) -> Tuple[List[int], List[RoleCandidate]]:
    """Chọn mảng loại xe. Hết giờ vẫn luôn trả về một phương án hợp lệ.

    `on_candidate`, nếu có, nhận từng phương án ngay khi được chấm (để web vẽ bảng).
    `iterations_per_day`, nếu có, bỏ giới hạn giờ: vòng 1 chấm mọi phương án, vòng 2 chạy
    ALNS đúng ngần ấy vòng mỗi ngày, nên kết quả tất định (benchmark offline, không dùng
    khi thi vì không chặn theo cửa sổ chọn xe).

    R5 (weights.role_traffic > 0): mỗi phương án được chấm trên mọi kịch bản của
    role_scenarios() và cộng điểm từng thành phần; 0 xe tiếp bị loại khi
    zero_cars_allowed() sai. Vòng 2 theo giây chạy song song trên `jobs` tiến trình
    (mặc định số lõi − 1) khi còn ít nhất `parallel_min_seconds`.
    """
    log = log or (lambda msg: None)
    w = weights or Weights()

    def tell(cand: RoleCandidate, phase: str, score, **extra) -> None:
        if on_candidate is not None:
            on_candidate(dict(label=cand.label, kinds=list(cand.kinds), score=list(score),
                              phase=phase, **extra))

    started = time.monotonic()
    deterministic = iterations_per_day is not None
    hard_deadline = None if deterministic else started + max(0.2, budget_seconds)
    options = candidates(config)
    scenarios = role_scenarios(config, w)
    names = [name for name, _ in scenarios]
    no_zero = w.role_traffic > 0 and len(options) > 1 and not zero_cars_allowed(config)

    def describe(parts) -> str:
        if len(parts) <= 1:
            return ""
        return " (" + "; ".join(f"{n}: {p[0]}/{p[1]}/{p[2]}" for n, p in zip(names, parts)) + ")"

    # Vòng 1: rollout tham lam toàn bộ phương án, tất định và rẻ.
    for cand in options:
        if hard_deadline is not None and time.monotonic() >= hard_deadline:
            break
        if no_zero and cand.n_cars == 0:
            log(f"vai trò {cand.label}: bỏ, xăng không đủ dư để chạy không xe tiếp")
            tell(cand, "skipped", cand.score, days=0)
            continue
        parts = []
        days_done = config.n_days
        for _, forecast in scenarios:
            score, days_done = rollout(config, cand.kinds, forecast, weights=weights,
                                       seed=seed, deadline=hard_deadline, with_days=True)
            if days_done < config.n_days:
                break
            parts.append(score)
        if days_done < config.n_days:
            # mẫu dở dang xếp hạng cùng mẫu đủ sẽ chọn sai; thà bỏ đi
            log(f"vai trò {cand.label}: bỏ, chỉ chơi được {days_done}/{config.n_days} ngày")
            tell(cand, "skipped", cand.score, days=days_done)
            continue
        cand.score = _total(parts)
        cand.evaluated = True
        log(f"vai trò {cand.label}: L={cand.score[0]} D={cand.score[1]} Q={cand.score[2]}{describe(parts)}")
        tell(cand, "rollout", cand.score, scenarios=[list(p) for p in parts])

    ranked = sorted([c for c in options if c.evaluated], key=lambda c: c.score, reverse=True)
    if not ranked:
        fallback = options[1] if len(options) > 1 else options[0]
        return list(fallback.kinds), options

    def refined(cand: RoleCandidate, parts) -> None:
        cand.refined = _total(parts)
        log(f"tinh chỉnh {cand.label}: L={cand.refined[0]} D={cand.refined[1]} "
            f"Q={cand.refined[2]}{describe(parts)}")
        tell(cand, "refine", cand.refined, scenarios=[list(p) for p in parts])

    # Vòng 2: nếu còn thời gian, đánh giá lại các phương án dẫn đầu bằng ALNS thật.
    finalists = ranked[:2]
    if deterministic:
        if len(finalists) > 1:
            for cand in finalists:
                refined(cand, [rollout(config, cand.kinds, forecast, weights=weights, seed=seed,
                                       iterations_per_day=iterations_per_day)
                               for _, forecast in scenarios])
            best = max(finalists, key=lambda c: c.best())
        else:
            best = ranked[0]
        log(f"chọn: {best.label}")
        return list(best.kinds), options

    remaining = hard_deadline - time.monotonic()
    cpus = os.cpu_count() or 1
    workers = max(1, cpus - 1) if jobs is None else max(1, int(jobs))
    parallel = remaining >= parallel_min_seconds and workers >= 2 and cpus >= 3 and len(ranked) > 1
    done = False
    if parallel:
        finalists = ranked[:3]
        work = [(cand, forecast) for cand in finalists for _, forecast in scenarios]
        workers = min(workers, len(work))
        rounds = -(-len(work) // workers)
        usable = remaining - POOL_STARTUP_SECONDS - POOL_RETURN_MARGIN
        per_day = max(0.05, usable / rounds / max(1, config.n_days))
        wall_deadline = time.time() + remaining - POOL_RETURN_MARGIN
        log(f"tinh chỉnh song song {len(work)} việc trên {workers} tiến trình, {per_day:.2f}s/ngày")
        results = _run_parallel(
            [dict(config=config, kinds=list(cand.kinds), forecast=forecast, weights=weights, seed=seed,
                  per_day=per_day, wall_deadline=wall_deadline) for cand, forecast in work],
            workers, hard_deadline)
        if results is not None:
            done = True
            k = len(scenarios)
            for idx, cand in enumerate(finalists):
                chunk = results[idx * k:(idx + 1) * k]
                if all(r is not None and r[1] >= config.n_days for r in chunk):
                    refined(cand, [r[0] for r in chunk])
                else:
                    log(f"tinh chỉnh {cand.label}: không kịp chơi đủ trận, giữ điểm vòng 1")
        else:
            log("không mở được tiến trình con: tinh chỉnh tuần tự")
            finalists = ranked[:2]
    if not done and remaining > 1.0 and len(finalists) > 1:
        remaining = hard_deadline - time.monotonic()
        per_job = remaining / (len(finalists) * len(scenarios))
        per_day = max(0.05, per_job / max(1, config.n_days))
        for cand in finalists:
            if time.monotonic() >= hard_deadline:
                break
            parts = []
            for _, forecast in scenarios:
                parts.append(rollout(
                    config, cand.kinds, forecast, weights=weights, seed=seed,
                    deadline=min(hard_deadline, time.monotonic() + per_job),
                    seconds_per_day=per_day,
                ))
            refined(cand, parts)
        done = True
    best = max(finalists, key=lambda c: c.best()) if done and len(finalists) > 1 else ranked[0]

    log(f"chọn: {best.label}")
    return list(best.kinds), options
