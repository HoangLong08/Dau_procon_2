"""Benchmark: chạy nhiều map và seed, tổng hợp kết quả và thời gian."""

import json
import statistics
import time
from typing import Dict, Iterable, List, Optional, Sequence

from .bots import BOT_TYPES, GreedyBot, SampleBot, SearchBot
from .mapgen import load_or_generate
from .runner import run_match


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def run_benchmark(
    maps: Sequence[str],
    seeds: Iterable[int],
    search_seconds: float = 1.0,
    role_seconds: float = 5.0,
    opponents: Sequence[str] = ("greedy", "sample", "sample"),
    out_path: Optional[str] = None,
    log=None,
) -> dict:
    log = log or (lambda msg: None)
    rows: List[dict] = []
    wins = 0
    ties = 0
    losses = 0
    invalid_total = 0
    all_times: List[float] = []

    seeds = list(seeds)
    for map_name in maps:
        for seed in seeds:
            config = load_or_generate(map_name, seed=seed)
            config.players = max(config.players, 1 + len(opponents))
            bots = [SearchBot(config, search_seconds=search_seconds,
                              role_seconds=role_seconds, seed=seed)]
            for i, name in enumerate(opponents):
                cls = BOT_TYPES[name]
                bots.append(cls(config, seed=seed * 10 + i + 1))
            t0 = time.monotonic()
            outcome = run_match(config, bots)
            elapsed = time.monotonic() - t0

            ours = outcome.teams[0]
            best_other = max((t.score() for t in outcome.teams[1:]), default=(0, 0, 0))
            if ours.score() > best_other:
                wins += 1
                verdict = "thắng"
            elif ours.score() == best_other:
                ties += 1
                verdict = "hoà"
            else:
                losses += 1
                verdict = "THUA"
            invalid_total += ours.invalid_days
            all_times.extend(ours.plan_seconds)
            rows.append({
                "map": map_name,
                "seed": seed,
                "size": f"{config.width}x{config.height}",
                "agents": config.n_agents,
                "days": config.n_days,
                "ours": ours.score(),
                "best_opponent": best_other,
                "verdict": verdict,
                "invalid": ours.invalid_days,
                "max_plan_seconds": round(max(ours.plan_seconds or [0]), 3),
                "match_seconds": round(elapsed, 1),
            })
            log(f"{map_name} seed {seed}: ta {ours.score()} | đối thủ tốt nhất {best_other} -> {verdict}"
                f" ({elapsed:.0f}s)")

    total = max(1, len(rows))
    summary = {
        "matches": len(rows),
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "win_rate": round(wins / total, 3),
        "invalid_plans": invalid_total,
        "plan_seconds_p50": round(_percentile(all_times, 0.50), 3),
        "plan_seconds_p95": round(_percentile(all_times, 0.95), 3),
        "plan_seconds_p99": round(_percentile(all_times, 0.99), 3),
        "plan_seconds_max": round(max(all_times or [0]), 3),
        "rows": rows,
    }
    log(
        f"\nTỔNG KẾT: {wins} thắng / {ties} hoà / {losses} thua trong {len(rows)} trận | "
        f"bài không hợp lệ: {invalid_total} | thời gian ngày p99 {summary['plan_seconds_p99']}s"
    )
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        log(f"đã ghi báo cáo: {out_path}")
    return summary
