"""Tính đường đi: Dijkstra trên đồ thị trọng số đỉnh (chi phí tính theo ô nguồn)."""

from heapq import heappop, heappush
from typing import Dict, List, Optional, Sequence, Tuple

from . import rules
from .model import MatchConfig

INF = float("inf")


class DayGraph:
    """Chi phí bước/xăng của từng ô cho một trạng thái đường cụ thể."""

    __slots__ = ("config", "step_cost", "fuel_cost", "passable", "neighbors", "n")

    def __init__(self, config: MatchConfig, road_status: Optional[Dict[int, int]] = None,
                 road_override: Optional[int] = None):
        self.config = config
        self.n = config.n_cells
        self.neighbors = config.neighbors
        road_status = road_status or {}
        step = [0] * self.n
        fuel = [0] * self.n
        ok = [False] * self.n
        for c in range(self.n):
            terrain = config.cells[c]
            if terrain == rules.POND:
                step[c] = rules.IMPASSABLE
                fuel[c] = rules.IMPASSABLE
                continue
            ok[c] = True
            if terrain == rules.ROAD:
                status = road_override if road_override is not None else road_status.get(c, rules.SMOOTH)
                step[c] = rules.STEP_ROAD[status]
            else:
                step[c] = rules.step_cost(terrain)
            fuel[c] = rules.fuel_cost(terrain)
        self.step_cost = step
        self.fuel_cost = fuel
        self.passable = ok


def dijkstra(graph: DayGraph, src: int, primary: Sequence[int], secondary: Sequence[int]):
    """Trả (dist chính, dist phụ, pred). Chi phí cạnh u->v là trọng số của ô u."""
    n = graph.n
    d1 = [INF] * n
    d2 = [INF] * n
    pred = [-1] * n
    d1[src] = 0
    d2[src] = 0
    neighbors = graph.neighbors
    passable = graph.passable
    heap = [(0, 0, src)]
    while heap:
        c1, c2, u = heappop(heap)
        if c1 > d1[u] or (c1 == d1[u] and c2 > d2[u]):
            continue
        w1 = primary[u]
        w2 = secondary[u]
        for v in neighbors[u]:
            if v < 0 or not passable[v]:
                continue
            n1 = c1 + w1
            n2 = c2 + w2
            if n1 < d1[v] or (n1 == d1[v] and n2 < d2[v]):
                d1[v] = n1
                d2[v] = n2
                pred[v] = u
                heappush(heap, (n1, n2, v))
    return d1, d2, pred


class DistTable:
    """Khoảng cách từ một tập nguồn tới mọi ô, kèm đường đi tái dựng được."""

    __slots__ = ("graph", "metric", "dist", "alt", "pred", "_path_cache")

    def __init__(self, graph: DayGraph, sources: Sequence[int], metric: str = "step"):
        self.graph = graph
        self.metric = metric
        self.dist: Dict[int, List[float]] = {}
        self.alt: Dict[int, List[float]] = {}
        self.pred: Dict[int, List[int]] = {}
        self._path_cache: Dict[Tuple[int, int], List[int]] = {}
        if metric == "step":
            primary, secondary = graph.step_cost, graph.fuel_cost
        else:
            primary, secondary = graph.fuel_cost, graph.step_cost
        for s in set(sources):
            d1, d2, pred = dijkstra(graph, s, primary, secondary)
            self.dist[s] = d1
            self.alt[s] = d2
            self.pred[s] = pred

    def has(self, src: int) -> bool:
        return src in self.dist

    def cost(self, src: int, dst: int) -> float:
        d = self.dist.get(src)
        return INF if d is None else d[dst]

    def alt_cost(self, src: int, dst: int) -> float:
        d = self.alt.get(src)
        return INF if d is None else d[dst]

    def steps_fuel(self, src: int, dst: int) -> Tuple[float, float]:
        """(bước, xăng) theo chỉ số của bảng này."""
        if self.metric == "step":
            return self.cost(src, dst), self.alt_cost(src, dst)
        return self.alt_cost(src, dst), self.cost(src, dst)

    def path(self, src: int, dst: int) -> List[int]:
        """Dãy ô từ ô ngay sau src tới dst; [] nếu trùng hoặc không tới được."""
        if src == dst:
            return []
        key = (src, dst)
        cached = self._path_cache.get(key)
        if cached is not None:
            return cached
        pred = self.pred.get(src)
        if pred is None or self.dist[src][dst] == INF:
            self._path_cache[key] = []
            return []
        out = []
        cur = dst
        while cur != src and cur >= 0:
            out.append(cur)
            cur = pred[cur]
        out.reverse()
        self._path_cache[key] = out
        return out


class LegOption:
    """Một phương án đi từ src tới dst."""

    __slots__ = ("steps", "fuel", "path", "spots")

    def __init__(self, steps: int, fuel: int, path: List[int], spots: Tuple[int, ...]):
        self.steps = steps
        self.fuel = fuel
        self.path = path
        self.spots = spots

    def __repr__(self):
        return f"Leg(steps={self.steps}, fuel={self.fuel}, n={len(self.path)})"


class LegBook:
    """Hai phương án cho mỗi chặng: ít bước nhất và ít xăng nhất."""

    def __init__(self, config: MatchConfig, dist_step: DistTable, dist_fuel: DistTable):
        self.config = config
        self.dist_step = dist_step
        self.dist_fuel = dist_fuel
        self._cache: Dict[Tuple[int, int], Tuple[LegOption, ...]] = {}

    def _make(self, table: DistTable, src: int, dst: int) -> Optional[LegOption]:
        steps, fuel = table.steps_fuel(src, dst)
        if steps == INF:
            return None
        path = table.path(src, dst)
        if not path and src != dst:
            return None
        spot_at = self.config.spot_index_at
        spots = tuple(spot_at[c] for c in path if c in spot_at)
        return LegOption(int(steps), int(fuel), path, spots)

    def options(self, src: int, dst: int) -> Tuple[LegOption, ...]:
        key = (src, dst)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        out = []
        a = self._make(self.dist_step, src, dst) if self.dist_step.has(src) else None
        if a is not None:
            out.append(a)
        if self.dist_fuel.has(src):
            b = self._make(self.dist_fuel, src, dst)
            if b is not None and not (a is not None and b.steps == a.steps and b.fuel == a.fuel):
                out.append(b)
        result = tuple(out)
        self._cache[key] = result
        return result

    def best_steps(self, src: int, dst: int) -> float:
        return self.dist_step.cost(src, dst)

    def min_fuel(self, src: int, dst: int) -> float:
        return self.dist_fuel.cost(src, dst)
