"""ALNS anytime trên nghiệm 'tour + điểm hẹn'.

Mọi nghiệm đều giải mã ra kế hoạch hợp lệ, nên toán tử phá/sửa được phép tuỳ tiện:
kết quả tệ nhất cũng chỉ là điểm thấp hơn, không bao giờ là bài không hợp lệ.
"""

import random
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .construct import construct, repair
from .decoder import Decoded, decode
from .solution import DayContext, DaySolution, Meet

DESTROY_OPS = ("random", "zero_gain", "related", "chain_dup", "tail", "meet_toggle")
REPAIR_OPS = ("greedy", "regret", "coverage")

REWARD_BEST = 8.0
REWARD_BETTER = 4.0
REWARD_ACCEPT = 1.0
WEIGHT_DECAY = 0.8
WEIGHT_FLOOR = 0.1
ADAPT_EVERY = 50
RRT_RATIO = 0.02
RESTART_AFTER = 500


def _remove_stops(sol: DaySolution, victims: Sequence[Tuple[int, int]]) -> DaySolution:
    """Bỏ các điểm dừng (slot, chỉ số) và dời điểm hẹn cho khớp."""
    by_patrol: Dict[int, List[int]] = {}
    for slot, idx in victims:
        by_patrol.setdefault(slot, []).append(idx)
    for slot, idxs in by_patrol.items():
        keep = [s for i, s in enumerate(sol.tours[slot]) if i not in set(idxs)]
        sol.tours[slot] = keep
    # điểm hẹn và lệnh đợi gắn với chỉ số điểm dừng nên không còn tin cậy: bỏ theo tour bị động
    touched = set(by_patrol)
    sol.meets = [m for m in sol.meets if m.patrol not in touched or m.stop_idx == -1]
    sol.waits = {k: v for k, v in sol.waits.items() if k[0] not in touched}
    return sol


def destroy_random(ctx, sol, rng, q):
    stops = sol.stops()
    if not stops:
        return sol
    victims = rng.sample(stops, min(q, len(stops)))
    return _remove_stops(sol, victims)


def destroy_zero_gain(ctx, sol, rng, q):
    """Bỏ điểm dừng không đem lại gì: chuỗi đã được xe khác phủ và kho đã hết phần."""
    seen_brand: Dict[int, int] = {}
    taken: Dict[int, int] = {}
    victims = []
    for slot, tour in enumerate(sol.tours):
        for idx, spot in enumerate(tour):
            brand = ctx.spot_brand[spot]
            brand_count = seen_brand.get(brand, 0)
            spot_count = taken.get(spot, 0)
            worthless = brand_count > 0 and spot_count >= ctx.stocks[spot]
            if worthless:
                victims.append((slot, idx))
            seen_brand[brand] = brand_count + 1
            taken[spot] = spot_count + 1
    if not victims:
        return destroy_random(ctx, sol, rng, q)
    rng.shuffle(victims)
    return _remove_stops(sol, victims[:q])


def destroy_related(ctx, sol, rng, q):
    stops = sol.stops()
    if not stops:
        return sol
    seed = rng.choice(stops)
    seed_cell = ctx.spot_cells[sol.tours[seed[0]][seed[1]]]
    scored = []
    for slot, idx in stops:
        cell = ctx.spot_cells[sol.tours[slot][idx]]
        scored.append((ctx.dist_step.cost(seed_cell, cell), slot, idx))
    scored.sort()
    victims = [(slot, idx) for _, slot, idx in scored[:q]]
    return _remove_stops(sol, victims)


def destroy_chain_dup(ctx, sol, rng, q):
    """Bỏ điểm dừng thuộc chuỗi đã được xe khác phủ, giải phóng bước cho chuỗi mới."""
    first_of_brand: Dict[int, Tuple[int, int]] = {}
    victims = []
    for slot, tour in enumerate(sol.tours):
        for idx, spot in enumerate(tour):
            brand = ctx.spot_brand[spot]
            if brand in first_of_brand:
                victims.append((slot, idx))
            else:
                first_of_brand[brand] = (slot, idx)
    if not victims:
        return destroy_random(ctx, sol, rng, q)
    rng.shuffle(victims)
    return _remove_stops(sol, victims[:q])


def destroy_tail(ctx, sol, rng, q):
    non_empty = [s for s, t in enumerate(sol.tours) if t]
    if not non_empty:
        return sol
    slot = rng.choice(non_empty)
    k = min(q, len(sol.tours[slot]))
    victims = [(slot, len(sol.tours[slot]) - 1 - i) for i in range(k)]
    return _remove_stops(sol, victims)


def destroy_meet_toggle(ctx, sol, rng, q):
    if ctx.n_cars == 0:
        return destroy_random(ctx, sol, rng, q)
    if sol.meets and rng.random() < 0.5:
        victim = rng.randrange(len(sol.meets))
        m = sol.meets.pop(victim)
        if rng.random() < 0.5 and ctx.n_cars > 1:
            other = rng.randrange(ctx.n_cars)
            sol.meets.append(Meet(m.patrol, m.stop_idx, other))
        return sol
    candidates = []
    for slot, tour in enumerate(sol.tours):
        for idx in range(-1, len(tour)):
            if not any(m.patrol == slot and m.stop_idx == idx for m in sol.meets):
                candidates.append((slot, idx))
    if not candidates:
        return sol
    slot, idx = rng.choice(candidates)
    sol.meets.append(Meet(slot, idx, rng.randrange(ctx.n_cars)))
    return sol


DESTROY_FUNCS = {
    "random": destroy_random,
    "zero_gain": destroy_zero_gain,
    "related": destroy_related,
    "chain_dup": destroy_chain_dup,
    "tail": destroy_tail,
    "meet_toggle": destroy_meet_toggle,
}


class Roulette:
    """Trọng số thích nghi cho các toán tử."""

    def __init__(self, names: Sequence[str]):
        self.names = list(names)
        self.weight = {n: 1.0 for n in self.names}
        self.reward = {n: 0.0 for n in self.names}
        self.uses = {n: 0 for n in self.names}

    def pick(self, rng: random.Random) -> str:
        total = sum(self.weight.values())
        r = rng.random() * total
        acc = 0.0
        for name in self.names:
            acc += self.weight[name]
            if r <= acc:
                return name
        return self.names[-1]

    def award(self, name: str, value: float):
        self.reward[name] += value
        self.uses[name] += 1

    def adapt(self):
        for name in self.names:
            if self.uses[name] == 0:
                continue
            mean = self.reward[name] / self.uses[name]
            self.weight[name] = max(WEIGHT_FLOOR, WEIGHT_DECAY * self.weight[name] + (1 - WEIGHT_DECAY) * mean)
            self.reward[name] = 0.0
            self.uses[name] = 0


class AlnsResult:
    __slots__ = ("solution", "decoded", "iterations", "seconds")

    def __init__(self, solution, decoded, iterations, seconds):
        self.solution = solution
        self.decoded = decoded
        self.iterations = iterations
        self.seconds = seconds


def improve(
    ctx: DayContext,
    start: Optional[DaySolution] = None,
    deadline: Optional[float] = None,
    max_iterations: int = 1_000_000,
    seed: int = 0,
    on_better: Optional[Callable[[DaySolution, Decoded], None]] = None,
) -> AlnsResult:
    """Chạy ALNS tới hạn chót. Luôn trả về một nghiệm hợp lệ."""
    rng = random.Random((seed << 8) ^ (ctx.day + 1))
    began = time.monotonic()
    cur = start.copy() if start is not None else construct(ctx, rng)
    cur_dec = decode(ctx, cur)
    best, best_dec = cur.copy(), cur_dec
    if on_better is not None:
        on_better(best, best_dec)

    destroy = Roulette(DESTROY_OPS)
    repair_ops = Roulette(REPAIR_OPS)
    since_best = 0
    it = 0
    max_stops = max(1, ctx.config.n_agents)

    while it < max_iterations:
        if deadline is not None and time.monotonic() >= deadline:
            break
        it += 1
        d_name = destroy.pick(rng)
        r_name = repair_ops.pick(rng)
        q = rng.randint(1, max(1, min(4, max_stops)))
        cand = DESTROY_FUNCS[d_name](ctx, cur.copy(), rng, q)
        cand = repair(ctx, cand, rng, mode=r_name)
        cand_dec = decode(ctx, cand)
        s = cand_dec.score

        if s > best_dec.score:
            best, best_dec = cand.copy(), cand_dec
            cur, cur_dec = cand, cand_dec
            destroy.award(d_name, REWARD_BEST)
            repair_ops.award(r_name, REWARD_BEST)
            since_best = 0
            if on_better is not None:
                on_better(best, best_dec)
        elif s > cur_dec.score:
            cur, cur_dec = cand, cand_dec
            destroy.award(d_name, REWARD_BETTER)
            repair_ops.award(r_name, REWARD_BETTER)
            since_best += 1
        elif s >= best_dec.score - RRT_RATIO * abs(best_dec.score):
            cur, cur_dec = cand, cand_dec
            destroy.award(d_name, REWARD_ACCEPT)
            repair_ops.award(r_name, REWARD_ACCEPT)
            since_best += 1
        else:
            destroy.award(d_name, 0.0)
            repair_ops.award(r_name, 0.0)
            since_best += 1

        if it % ADAPT_EVERY == 0:
            destroy.adapt()
            repair_ops.adapt()
        if since_best >= RESTART_AFTER:
            cur = destroy_random(ctx, best.copy(), rng, max(2, max_stops // 2))
            cur = repair(ctx, cur, rng, mode="coverage")
            cur_dec = decode(ctx, cur)
            since_best = 0

    return AlnsResult(best, best_dec, it, time.monotonic() - began)
