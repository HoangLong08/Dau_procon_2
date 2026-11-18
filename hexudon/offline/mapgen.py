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
    """Sinh một cấu hình trận hợp lệ, mọi tham số nằm trong giới hạn BTC.

    Rộng và cao bốc riêng khi không truyền `size` (đề: mỗi chiều 8–32, có thể
    khác nhau, cases 6.15); số bước từng ngày bốc riêng, không sắp xếp (cases 5.7).
    """
    rng = random.Random(seed)
    if size is not None:
        width = height = size
    width = width or rng.randint(8, 32)
    height = height or rng.randint(8, 32)
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
        day_steps = [rng.randint(lo, min(hi, lo * 2)) for _ in range(n_days)]
        # đề không cho F nhỏ nhất: bình có thể nhỏ hơn một ngày đi (cases 5.9)
        fuel = int(min(day_steps) * rng.uniform(0.5, 3.0))
        busy = rng.randint(1, 5)
        # hai ngưỡng có thể bằng nhau: khi đó không có trạng thái đông đúc (cases 8.3)
        jam = rng.randint(max(2, busy), 10)
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


#: tỉ lệ địa hình của server VN: giống hệt nhau trên cả 5 cấu hình thật (Mock Q06 và 4 trận
#: contest 17/11/2026): đồng bằng 21%, đường 34%, núi 20%, ao 25%
VN_TERRAIN = ((rules.PLAIN, 0.21), (rules.ROAD, 0.34), (rules.MOUNTAIN, 0.20), (rules.POND, 0.25))


def generate_vn(seed: int = 0, players: int = 59) -> MatchConfig:
    """Bản đồ theo phân bố của server luyện tập VN (5 cấu hình thật, 16–17/11/2026).

    Cỡ 30–32 mỗi chiều; 4–6 xe; số spot = cạnh lớn nhất; 15 hoặc 20 chuỗi (mỗi chuỗi ít
    nhất 1 spot, phần dư chia ngẫu nhiên nên có chuỗi 3–4 spot); kho = số xe; 10 ngày, mỗi
    ngày bước bốc đều trong [W+H, 4(W+H)]; F = bước ngày 0 × U(1,3; 2,8); ngưỡng đông 2–5,
    tắc 4–9; 59 đội đăng ký (gần như chỉ 2 đội chơi: giao thông ≈ 0).
    """
    rng = random.Random(seed)
    width = rng.choice([30, 31, 32])
    height = rng.choice([30, 31, 32])
    n = width * height
    neighbors = build_neighbor_table(width, height)
    n_agents = rng.choice([4, 4, 5, 5, 6])
    for _ in range(60):
        cells = []
        for terrain, share in VN_TERRAIN:
            cells += [terrain] * int(round(share * n))
        cells = (cells + [rules.PLAIN] * n)[:n]
        rng.shuffle(cells)
        comp = _largest_component(cells, neighbors)
        if len(comp) < n // 2:
            continue
        in_comp = set(comp)
        for c in range(n):
            if cells[c] != rules.POND and c not in in_comp:
                cells[c] = rules.POND
        plains = [c for c in comp if cells[c] == rules.PLAIN]
        n_spots = max(width, height)
        if len(plains) < n_spots + n_agents + 2:
            continue
        rng.shuffle(plains)
        spot_cells = plains[:n_spots]
        start_cells = plains[n_spots:n_spots + n_agents]
        n_chains = min(n_spots, rng.choice([15, 20]))
        brand_of = list(range(n_chains)) + [rng.randrange(n_chains) for _ in range(n_spots - n_chains)]
        rng.shuffle(brand_of)
        spot_objs = [Spot(brand=brand_of[i], pos=pos, stocks=n_agents) for i, pos in enumerate(spot_cells)]
        lo, hi = width + height, 4 * (width + height)
        day_steps = [rng.randint(lo, hi) for _ in range(10)]
        fuel = int(round(day_steps[0] * rng.uniform(1.3, 2.8)))
        busy = rng.randint(2, 5)
        jam = rng.randint(max(4, busy + 1), 9)
        return MatchConfig(
            width=width,
            height=height,
            cells=cells,
            spots=spot_objs,
            agent_starts=start_cells,
            fuel_limits=max(fuel, rules.FUEL_MOUNTAIN),
            day_steps=day_steps,
            day_seconds=[30] * 10,
            players=players,
            busy_threshold=busy,
            jammed_threshold=jam,
        )
    raise RuntimeError(f"không sinh được bản đồ VN với seed {seed}")


def load_or_generate(name: str, seed: int = 0) -> MatchConfig:
    """'sample_16' -> map chính thức; 'random24' -> map ngẫu nhiên 24x24;
    'random8x32' -> rộng 8, cao 32 (map dài và hẹp, cases 6.15); 'random' -> cả rộng
    lẫn cao đều bốc ngẫu nhiên."""
    import json
    import os

    if name.startswith("sample_"):
        path = os.path.join("data", "sample_maps", f"{name}.json")
        with open(path, encoding="utf-8") as fh:
            return MatchConfig.from_json(json.load(fh))
    if name.startswith("random"):
        spec = name[6:]
        if not spec:
            return generate(seed=seed)
        if "x" in spec:
            width, height = (int(v) for v in spec.split("x", 1))
            return generate(seed=seed, width=width, height=height)
        return generate(seed=seed, size=int(spec))
    if os.path.exists(name):
        with open(name, encoding="utf-8") as fh:
            obj = json.load(fh)
        return MatchConfig.from_problem_json(obj) if "problem" in obj else MatchConfig.from_json(obj)
    raise ValueError(f"không hiểu tên map: {name}")
