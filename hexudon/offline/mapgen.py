"""Sinh map ngẫu nhiên trong đúng giới hạn chính thức của ban tổ chức."""

import random
from collections import deque
from typing import Dict, List, Optional, Tuple

from .. import rules
from ..geometry import build_neighbor_table
from ..model import MatchConfig, Spot

# tỉ lệ địa hình lấy quanh phân bố của ba map mẫu chính thức
TERRAIN_MIX = {
    rules.PLAIN: (0.22, 0.36),
    rules.ROAD: (0.22, 0.32),
    rules.MOUNTAIN: (0.16, 0.32),
    rules.POND: (0.16, 0.26),
}


def _random_terrain(rng: random.Random, n: int) -> List[int]:
    weights = {}
    for terrain, (lo, hi) in TERRAIN_MIX.items():
        weights[terrain] = rng.uniform(lo, hi)
    total = sum(weights.values())
    cells = []
    kinds = list(weights)
    probs = [weights[k] / total for k in kinds]
    for _ in range(n):
        r = rng.random()
        acc = 0.0
        for kind, p in zip(kinds, probs):
            acc += p
            if r <= acc:
                cells.append(kind)
                break
        else:
            cells.append(rules.PLAIN)
    return cells


def _largest_component(cells: List[int], neighbors: List[List[int]]) -> List[int]:
    seen = [False] * len(cells)
    best: List[int] = []
    for start in range(len(cells)):
        if seen[start] or cells[start] == rules.POND:
            continue
        comp = []
        q = deque([start])
        seen[start] = True
        while q:
            c = q.popleft()
            comp.append(c)
            for nb in neighbors[c]:
                if nb >= 0 and not seen[nb] and cells[nb] != rules.POND:
                    seen[nb] = True
                    q.append(nb)
        if len(comp) > len(best):
            best = comp
    return best


def generate(
    seed: int = 0,
    size: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    agents: Optional[int] = None,
    spots: Optional[int] = None,
    chains: Optional[int] = None,
    days: Optional[int] = None,
) -> MatchConfig:
    """Sinh một cấu hình trận hợp lệ, mọi tham số nằm trong giới hạn BTC."""
    rng = random.Random(seed)
    if size is not None:
        width = height = size
    width = width or rng.randint(8, 32)
    height = height or width
    n = width * height
    neighbors = build_neighbor_table(width, height)
    n_agents = agents if agents is not None else rng.randint(3, 8)

    for _ in range(60):
        cells = _random_terrain(rng, n)
        comp = _largest_component(cells, neighbors)
        if len(comp) < max(n_agents * 4, n // 3):
            continue
        in_comp = set(comp)
        for c in range(n):
            if cells[c] != rules.POND and c not in in_comp:
                cells[c] = rules.POND
        # bảo đảm mỗi loại địa hình xuất hiện ít nhất một lần
        present = {cells[c] for c in comp}
        pool = [c for c in comp if cells[c] == rules.PLAIN]
        ok = True
        for terrain in (rules.ROAD, rules.MOUNTAIN):
            if terrain not in present:
                if len(pool) < 4:
                    ok = False
                    break
                cells[pool.pop()] = terrain
        if not ok or rules.POND not in set(cells):
            continue
        plains = [c for c in comp if cells[c] == rules.PLAIN]
        n_spots = spots if spots is not None else min(max(width, height), max(n_agents, width))
        n_spots = max(n_agents, min(n_spots, max(width, height)))
        if len(plains) < n_spots + n_agents + 2:
            continue
        rng.shuffle(plains)
        spot_cells = plains[:n_spots]
        start_cells = plains[n_spots:n_spots + n_agents]
        n_chains = chains if chains is not None else rng.randint(
            max(1, int(n_spots * 0.6)), max(1, int(n_spots * 0.9))
        )
        n_chains = max(1, min(n_chains, n_spots))
        brand_of = [i % n_chains for i in range(n_spots)]
        rng.shuffle(brand_of)
        spot_objs = [
            Spot(brand=brand_of[i], pos=pos, stocks=rng.randint(1, n_agents))
            for i, pos in enumerate(spot_cells)
        ]
        n_days = days if days is not None else rng.randint(4, 10)
        lo, hi = width + height, 4 * (width + height)
        day_steps = sorted(rng.randint(lo, min(hi, lo * 2)) for _ in range(n_days))
        fuel = int(day_steps[0] * rng.uniform(1.0, 3.0))
        busy = rng.randint(1, 5)
        jam = rng.randint(max(2, busy + 1), 10)
        return MatchConfig(
            width=width,
            height=height,
            cells=cells,
            spots=spot_objs,
            agent_starts=start_cells,
            fuel_limits=max(fuel, rules.FUEL_MOUNTAIN),
            day_steps=day_steps,
            day_seconds=[5] * n_days,
            players=rng.choice([2, 4, 8, 10]),
            busy_threshold=busy,
            jammed_threshold=jam,
        )
    raise RuntimeError(f"không sinh được map hợp lệ với seed {seed}")


def load_or_generate(name: str, seed: int = 0) -> MatchConfig:
    """'sample_16' -> map chính thức; 'random24' -> map sinh ngẫu nhiên cỡ 24."""
    import json
    import os

    if name.startswith("sample_"):
        path = os.path.join("data", "sample_maps", f"{name}.json")
        with open(path, encoding="utf-8") as fh:
            return MatchConfig.from_json(json.load(fh))
    if name.startswith("random"):
        size = int(name[6:] or 16)
        return generate(seed=seed, size=size)
    if os.path.exists(name):
        with open(name, encoding="utf-8") as fh:
            obj = json.load(fh)
        return MatchConfig.from_problem_json(obj) if "problem" in obj else MatchConfig.from_json(obj)
    raise ValueError(f"không hiểu tên map: {name}")
