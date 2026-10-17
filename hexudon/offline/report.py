"""Benchmark: chạy nhiều map và seed, tổng hợp kết quả và thời gian.

Hai lệnh dùng file này:
  - `bench`: bot của ta đấu một nhóm đối thủ cố định, báo thắng/hoà/thua;
  - `ab`: hai biến thể của bot ta (khác trọng số) chơi CÙNG trận, nên chịu cùng giao
    thông và cùng đối thủ; so từng cặp trận, kèm khoảng tin cậy.

Với `--search-iterations`, ngân sách tìm kiếm tính theo số vòng ALNS thay vì giây: chạy
lại ra đúng số cũ, và chạy song song (`--jobs`) không làm bot yếu đi vì tranh CPU.
"""

import json
import math
import statistics
import time
from dataclasses import fields, replace
from typing import Dict, Iterable, List, Optional, Sequence

from ..planner.solution import Weights
from .bots import BOT_TYPES, SearchBot
from .mapgen import load_or_generate
from .runner import run_match


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def _map_jobs(func, jobs: Sequence[dict], n_jobs: int):
    """Chạy func trên từng việc, song song nếu n_jobs > 1; trả kết quả theo đúng thứ tự."""
    if n_jobs <= 1 or len(jobs) <= 1:
        for job in jobs:
            yield func(job)
        return
    from multiprocessing import Pool

    with Pool(min(n_jobs, len(jobs))) as pool:
        for row in pool.imap(func, jobs):
            yield row


def _search_bot(config, seed: int, job: dict, weights: Optional[Weights] = None, name: str = "search"):
    bot = SearchBot(config, search_seconds=job["search_seconds"], role_seconds=job["role_seconds"],
                    seed=seed, weights=weights, search_iterations=job.get("search_iterations"),
                    role_iterations=job.get("role_iterations"))
    bot.name = name
    return bot


def _opponents(config, seed: int, job: dict) -> list:
    bots = []
    for i, name in enumerate(job["opponents"]):
        if name == "search":
            bots.append(_search_bot(config, seed * 10 + i + 1, job))
        else:
            bots.append(BOT_TYPES[name](config, seed=seed * 10 + i + 1))
    return bots


def _bench_match(job: dict) -> dict:
    """Một trận của `bench`. Hàm ở cấp module để tiến trình con gọi được."""
    map_name, seed = job["map"], job["seed"]
    config = load_or_generate(map_name, seed=seed)
    bots = [_search_bot(config, seed, job)] + _opponents(config, seed, job)
    # lưu lượng chia cho số đội THẬT trong trận (cases 3, 8.8), không phải số
    # players ghi trong map mẫu: chia cho 10 khi chỉ có 4 đội làm đường thông giả
    config.players = len(bots)
    t0 = time.monotonic()
    outcome = run_match(config, bots)
    elapsed = time.monotonic() - t0

    ours = outcome.teams[0]
    # xếp như đề: L, D, Q rồi tổng thời gian phản hồi thấp hơn (tiêu chí 4)
    best = max(outcome.teams[1:], key=lambda t: t.rank_key(), default=None)
    best_other = best.score() if best is not None else (0, 0, 0)
    if best is None or ours.rank_key() > best.rank_key():
        verdict = "thắng"
    elif ours.rank_key() == best.rank_key():
        verdict = "hoà"
    else:
        verdict = "THUA"
    return {
        "map": map_name,
        "seed": seed,
        "size": f"{config.width}x{config.height}",
        "agents": config.n_agents,
        "days": config.n_days,
        "ours": ours.score(),
        "kinds": ours.kinds,
        "best_opponent": best_other,
        "response_seconds": round(ours.response_seconds, 2),
        "best_opponent_response_seconds": round(best.response_seconds, 2) if best else None,
        "verdict": verdict,
        "invalid": ours.invalid_days,
        "plan_seconds": [round(s, 3) for s in ours.plan_seconds],
        "max_plan_seconds": round(max(ours.plan_seconds or [0]), 3),
        "match_seconds": round(elapsed, 1),
    }


def run_benchmark(
    maps: Sequence[str],
    seeds: Iterable[int],
    search_seconds: float = 1.0,
    role_seconds: float = 5.0,
    opponents: Sequence[str] = ("greedy", "sample", "sample"),
    out_path: Optional[str] = None,
    log=None,
    jobs: int = 1,
    search_iterations: Optional[int] = None,
    role_iterations: Optional[int] = None,
) -> dict:
    log = log or (lambda msg: None)
    seeds = list(seeds)
    work = [{"map": m, "seed": s, "search_seconds": search_seconds, "role_seconds": role_seconds,
             "opponents": list(opponents), "search_iterations": search_iterations,
             "role_iterations": role_iterations} for m in maps for s in seeds]
    rows: List[dict] = []
    for row in _map_jobs(_bench_match, work, jobs):
        rows.append(row)
        log(f"{row['map']} seed {row['seed']}: ta {tuple(row['ours'])} | đối thủ tốt nhất "
            f"{tuple(row['best_opponent'])} -> {row['verdict']} ({row['match_seconds']:.0f}s)")

    wins = sum(1 for r in rows if r["verdict"] == "thắng")
    ties = sum(1 for r in rows if r["verdict"] == "hoà")
    losses = sum(1 for r in rows if r["verdict"] == "THUA")
    invalid_total = sum(r["invalid"] for r in rows)
    all_times = [t for r in rows for t in r.pop("plan_seconds")]
    total = max(1, len(rows))
    summary = {
        "matches": len(rows),
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "win_rate": round(wins / total, 3),
        "invalid_plans": invalid_total,
        "totals_LDQ": [sum(r["ours"][i] for r in rows) for i in range(3)],
        "search_iterations": search_iterations,
        "plan_seconds_p50": round(_percentile(all_times, 0.50), 3),
        "plan_seconds_p95": round(_percentile(all_times, 0.95), 3),
        "plan_seconds_p99": round(_percentile(all_times, 0.99), 3),
        "plan_seconds_max": round(max(all_times or [0]), 3),
        "rows": rows,
    }
    log(
        f"\nTỔNG KẾT: {wins} thắng / {ties} hoà / {losses} thua trong {len(rows)} trận | "
        f"L/D/Q tổng {summary['totals_LDQ']} | bài không hợp lệ: {invalid_total} | "
        f"thời gian ngày p99 {summary['plan_seconds_p99']}s"
    )
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        log(f"đã ghi báo cáo: {out_path}")
    return summary


# ---------------------------------------------------------------- A/B

WEIGHT_NAMES = tuple(f.name for f in fields(Weights))


def parse_weights(text: str) -> Dict[str, float]:
    """'fuel_bonus=2, near_chain=0.4' -> {'fuel_bonus': 2.0, 'near_chain': 0.4}."""
    out: Dict[str, float] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"trọng số phải có dạng tên=giá_trị: {part!r}")
        name, value = (s.strip() for s in part.split("=", 1))
        if name not in WEIGHT_NAMES:
            raise ValueError(f"không có trọng số {name!r}; có: {', '.join(WEIGHT_NAMES)}")
        out[name] = float(value)
    return out


def _ab_match(job: dict) -> dict:
    """Một trận của `ab`: biến thể A và B cùng chơi với các đối thủ."""
    map_name, seed = job["map"], job["seed"]
    config = load_or_generate(map_name, seed=seed)
    a = _search_bot(config, seed, job, replace(Weights(), **job["a"]), name="A")
    b = _search_bot(config, seed, job, replace(Weights(), **job["b"]), name="B")
    bots = [a, b] + _opponents(config, seed, job)
    config.players = max(job.get("players") or 0, len(bots))
    outcome = run_match(config, bots)
    ta, tb = outcome.teams[0], outcome.teams[1]
    ranking = [t.name for t in outcome.ranking()]
    return {
        "map": map_name, "seed": seed, "size": f"{config.width}x{config.height}",
        "agents": config.n_agents, "days": config.n_days,
        "A": list(ta.score()), "B": list(tb.score()), "kinds_A": ta.kinds, "kinds_B": tb.kinds,
        "rank_A": ranking.index("A") + 1, "rank_B": ranking.index("B") + 1, "teams": len(bots),
        "invalid_A": ta.invalid_days, "invalid_B": tb.invalid_days,
        "plan_seconds_A": [round(s, 3) for s in ta.plan_seconds],
        "plan_seconds_B": [round(s, 3) for s in tb.plan_seconds],
    }


def _mean_ci(values: Sequence[float]) -> List[float]:
    """[trung bình, nửa khoảng tin cậy 95%] theo xấp xỉ chuẩn."""
    if not values:
        return [0.0, 0.0]
    mean = statistics.mean(values)
    if len(values) < 2:
        return [round(mean, 3), 0.0]
    half = 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    return [round(mean, 3), round(half, 3)]


def run_ab(
    maps: Sequence[str],
    seeds: Iterable[int],
    a: Dict[str, float],
    b: Dict[str, float],
    opponents: Sequence[str] = ("search", "search", "greedy", "sample"),
    search_iterations: Optional[int] = 1000,
    role_iterations: Optional[int] = 100,
    search_seconds: float = 1.0,
    role_seconds: float = 3.0,
    players: int = 0,
    jobs: int = 1,
    out_path: Optional[str] = None,
    log=None,
) -> dict:
    """So hai bộ trọng số của bot trong cùng các trận; B thắng nghĩa là (L, D, Q) của B lớn hơn.

    Thời gian phản hồi không được xét: với ngân sách theo số vòng nó chỉ đo tốc độ máy.
    """
    log = log or (lambda msg: None)
    seeds = list(seeds)
    work = [{"map": m, "seed": s, "a": dict(a), "b": dict(b), "opponents": list(opponents),
             "search_iterations": search_iterations, "role_iterations": role_iterations,
             "search_seconds": search_seconds, "role_seconds": role_seconds, "players": players}
            for m in maps for s in seeds]
    rows: List[dict] = []
    for row in _map_jobs(_ab_match, work, jobs):
        rows.append(row)
        verdict = "B hơn" if tuple(row["B"]) > tuple(row["A"]) else (
            "hoà" if row["B"] == row["A"] else "A hơn")
        log(f"{row['map']} seed {row['seed']}: A {tuple(row['A'])} | B {tuple(row['B'])} -> {verdict}")

    b_wins = sum(1 for r in rows if tuple(r["B"]) > tuple(r["A"]))
    ties = sum(1 for r in rows if r["B"] == r["A"])
    a_wins = len(rows) - b_wins - ties
    times_a = [t for r in rows for t in r.pop("plan_seconds_A")]
    times_b = [t for r in rows for t in r.pop("plan_seconds_B")]
    summary = {
        "matches": len(rows),
        "a": a, "b": b, "opponents": list(opponents), "search_iterations": search_iterations,
        "b_wins_ties_losses": [b_wins, ties, a_wins],
        "delta_L": _mean_ci([r["B"][0] - r["A"][0] for r in rows]),
        "delta_D": _mean_ci([r["B"][1] - r["A"][1] for r in rows]),
        "delta_Q": _mean_ci([r["B"][2] - r["A"][2] for r in rows]),
        "totals_A": [sum(r["A"][i] for r in rows) for i in range(3)],
        "totals_B": [sum(r["B"][i] for r in rows) for i in range(3)],
        "mean_rank_A": round(statistics.mean(r["rank_A"] for r in rows), 3) if rows else 0,
        "mean_rank_B": round(statistics.mean(r["rank_B"] for r in rows), 3) if rows else 0,
        "invalid_A": sum(r["invalid_A"] for r in rows),
        "invalid_B": sum(r["invalid_B"] for r in rows),
        "plan_seconds_p95_A": round(_percentile(times_a, 0.95), 3),
        "plan_seconds_p95_B": round(_percentile(times_b, 0.95), 3),
        "rows": rows,
    }
    log(
        f"\nTỔNG KẾT {len(rows)} trận: B hơn/hoà/kém A = {b_wins}/{ties}/{a_wins} | "
        f"ΔD mỗi trận {summary['delta_D'][0]:+.2f} ± {summary['delta_D'][1]:.2f} | "
        f"ΔQ {summary['delta_Q'][0]:+.1f} ± {summary['delta_Q'][1]:.1f} | ΔL {summary['delta_L'][0]:+.3f} | "
        f"hạng TB A {summary['mean_rank_A']} B {summary['mean_rank_B']} | "
        f"bài sai A {summary['invalid_A']} B {summary['invalid_B']}"
    )
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        log(f"đã ghi báo cáo: {out_path}")
    return summary
