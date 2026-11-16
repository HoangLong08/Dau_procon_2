"""Bot đối thủ giả lập: các kiểu chơi mà đội khác có thể dùng.

Mỗi kiểu dựng theo một nguồn thật (xem de-xuat-chien-thuat/phan-tich-va-de-xuat-chien-thuat.md):

    nearest        đội tham lam đơn giản: spot gần nhất, không để ý chuỗi, toàn xe tuần tra
    chain_greedy   tham lam theo chuỗi (hxn, hướng 1 của note.md), 1 xe tiếp đuổi xe ít xăng nhất
    escort         đoàn xe: mỗi xe tiếp đi kèm một xe tuần tra nên xe đó luôn đầy (Q&A A48)
    hub            1 xe tiếp đứng ở trạm trung tâm, xe tuần tra quay về khi thiếu xăng
    road_block     xe tiếp đỗ cả ngày trên ô đường nút cổ chai (chỉ để thử phòng thủ)
    servings_alns  ALNS của ta nhưng gần như chỉ tối đa số phần (lỗi kiểu Naprock-18)
    all_patrol     ALNS của ta, toàn xe tuần tra (taiki288 khi dư xăng)

Các bot tham lam lập kế hoạch bằng một vòng mô phỏng từng bước chép đúng thứ tự
pha của simulator.run_day, rồi tự kiểm bằng run_day(strict_fuel=True); bài sai
(không nên xảy ra) được thay bằng idle_plan và đếm vào `fallbacks`.
"""

from typing import Dict, List, Optional, Sequence, Set, Tuple

from .. import rules
from ..model import AgentState, DayState, MatchConfig
from ..pathing import INF, DayGraph, dijkstra
from ..planner.solution import Weights
from ..simulator import compact_plan, idle_plan, run_day
from .bots import Bot, SearchBot

MOVE = "move"
WAIT = "wait"


class _Paths:
    """Dijkstra theo số bước (phụ: xăng) cho một ngày, nhớ theo ô nguồn."""

    def __init__(self, config: MatchConfig, road_status: Dict[int, int]):
        self.config = config
        self.graph = DayGraph(config, road_status)
        self._cache: Dict[int, Tuple[List[float], List[float], List[int]]] = {}

    def table(self, src: int):
        got = self._cache.get(src)
        if got is None:
            got = dijkstra(self.graph, src, self.graph.step_cost, self.graph.fuel_cost)
            self._cache[src] = got
        return got

    def steps(self, src: int, dst: int) -> float:
        return self.table(src)[0][dst]

    def fuel(self, src: int, dst: int) -> float:
        return self.table(src)[1][dst]

    def path(self, src: int, dst: int) -> List[int]:
        """Các ô từ ngay sau src tới dst; [] nếu trùng hoặc không tới được."""
        if src == dst:
            return []
        d1, _, pred = self.table(src)
        if d1[dst] == INF:
            return []
        out = []
        cur = dst
        while cur != src and cur >= 0:
            out.append(cur)
            cur = pred[cur]
        out.reverse()
        return out


class _Sim:
    """Một ngày của một đội, mô phỏng từng bước như simulator.run_day.

    Chính sách được hỏi mỗi khi một xe rảnh (pha hành động) và trả (MOVE, ô kề)
    hoặc (WAIT, k). Lệnh không đi được (thiếu bước, thiếu xăng lúc nhận lệnh)
    được đổi thành chờ, nên kế hoạch luôn hợp lệ theo cả cách hiểu chặt.
    """

    def __init__(self, config: MatchConfig, steps: int, agents: Sequence[AgentState],
                 road_status: Dict[int, int], stocks: Sequence[int]):
        self.config = config
        self.steps = steps
        self.n = len(agents)
        self.kind = [a.kind for a in agents]
        self.pos = [a.pos for a in agents]
        self.fuel = [a.fuel for a in agents]
        self.status = road_status
        self.stocks = list(stocks)
        self.collected: List[Set[int]] = [set() for _ in agents]
        self.brands_today: Set[int] = set()
        self.tokens: List[List[int]] = [[] for _ in agents]
        self.pending: List[Optional[Tuple[int, int, int]]] = [None] * self.n  # (kết thúc, đích, nguồn)
        #: lệnh đã thực sự phát cho từng xe: (bước, (MOVE, ô) hoặc (WAIT, k))
        self.issued: List[Optional[Tuple[int, Tuple[str, int]]]] = [None] * self.n
        self.t = 0

    def cost_from(self, cell: int) -> int:
        terrain = self.config.cells[cell]
        if terrain == rules.ROAD:
            return rules.STEP_ROAD[self.status.get(cell, rules.SMOOTH)]
        return rules.step_cost(terrain)

    def can_move(self, i: int, dst: int) -> bool:
        cfg = self.config
        src = self.pos[i]
        if dst < 0 or cfg.cells[dst] == rules.POND or dst not in cfg.dir_index[src]:
            return False
        if self.t + self.cost_from(src) > self.steps:
            return False
        if self.kind[i] == rules.PATROL and self.fuel[i] < rules.fuel_cost(cfg.cells[src]):
            return False
        return True

    def _issue(self, i: int, action: Tuple[str, int]) -> None:
        t = self.t
        left = self.steps - t
        what, arg = action
        if what == MOVE and self.can_move(i, arg):
            src = self.pos[i]
            self.tokens[i].append(self.config.dir_index[src][arg])
            self.pending[i] = (t + self.cost_from(src), arg, src)
            self.issued[i] = (t, (MOVE, arg))
            return
        wait = max(1, min(left, arg if what == WAIT else 1))
        self.tokens[i].append(-wait)
        self.pending[i] = (t + wait, -1, self.pos[i])
        self.issued[i] = (t, (WAIT, wait))

    def run(self, decide, order: Sequence[int]) -> List[List[int]]:
        """decide(sim, i) -> hành động. `order`: thứ tự hỏi các xe trong cùng một bước."""
        cfg = self.config
        fuel_limit = cfg.fuel_limits
        for i in order:
            self._issue(i, decide(self, i))
        for t in range(1, self.steps + 1):
            self.t = t
            for i in range(self.n):
                p = self.pending[i]
                if p is not None and p[0] == t and p[1] >= 0 and self.kind[i] == rules.PATROL:
                    self.fuel[i] -= rules.fuel_cost(cfg.cells[p[2]])
            for i in range(self.n):
                p = self.pending[i]
                if p is not None and p[0] == t and p[1] >= 0:
                    self.pos[i] = p[1]
            for i in range(self.n):
                if self.kind[i] != rules.PATROL:
                    continue
                s = cfg.spot_index_at.get(self.pos[i])
                if s is None or s in self.collected[i] or self.stocks[s] <= 0:
                    continue
                self.stocks[s] -= 1
                self.collected[i].add(s)
                self.brands_today.add(cfg.spots[s].brand)
            cars = {self.pos[i] for i in range(self.n) if self.kind[i] == rules.REFUEL}
            for i in range(self.n):
                if self.kind[i] == rules.PATROL and self.pos[i] in cars:
                    self.fuel[i] = fuel_limit
            for i in range(self.n):
                if self.pending[i] is not None and self.pending[i][0] == t:
                    self.pending[i] = None
            if t < self.steps:
                for i in order:
                    if self.pending[i] is None:
                        self._issue(i, decide(self, i))
        return compact_plan(self.tokens)


class ScriptedBot(Bot):
    """Khung chung cho các bot tham lam: chọn vai trò, rồi mỗi ngày chạy _Sim."""

    name = "scripted"
    #: chế độ chọn spot: "nearest" (mù chuỗi) hoặc "chain" (ưu tiên chuỗi mới)
    target_mode = "chain"

    def __init__(self, config: MatchConfig, seed: int = 0):
        self.config = config
        self.seed = seed
        self.kinds: List[int] = []
        self.match_brands: Set[int] = set()
        self.fallbacks = 0
        self.last_response_seconds: Optional[float] = None

    # --- vai trò ---------------------------------------------------------
    def n_cars(self) -> int:
        return 0

    def select_roles(self) -> List[int]:
        n = self.config.n_agents
        k = max(0, min(self.n_cars(), n - 1))
        kinds = [rules.PATROL] * n
        for i in range(k):
            kinds[n - 1 - i] = rules.REFUEL
        self.kinds = kinds
        return kinds

    def observe(self, state: DayState, result) -> None:
        if result is not None:
            self.match_brands |= set(result.brands)

    # --- một ngày --------------------------------------------------------
    def plan(self, state: DayState) -> List[List[int]]:
        cfg = self.config
        steps = state.steps or cfg.steps_of_day(state.day)
        agents = [AgentState(self.kinds[i] if i < len(self.kinds) else a.kind, a.pos, a.fuel)
                  for i, a in enumerate(state.agents)]
        status = dict(state.road_status)
        stocks = [s.stocks for s in cfg.spots]
        self.paths = _Paths(cfg, status)
        self.day = state.day
        self.targets: Dict[int, int] = {}       # xe tuần tra -> spot đang nhắm
        self.routes: Dict[int, List[int]] = {}  # xe -> các ô còn phải đi
        self.start_day(agents)
        sim = _Sim(cfg, steps, agents, status, stocks)
        patrols = [i for i, a in enumerate(agents) if a.kind == rules.PATROL]
        cars = [i for i, a in enumerate(agents) if a.kind != rules.PATROL]
        plan = sim.run(self.decide, patrols + cars)
        check = run_day(cfg, steps, agents, plan, stocks, status, strict_fuel=True)
        if not check.ok:
            self.fallbacks += 1
            plan = idle_plan(cfg, agents, steps, status)
        self.last_response_seconds = 0.0
        return plan

    def start_day(self, agents: Sequence[AgentState]) -> None:
        pass

    def decide(self, sim: _Sim, i: int) -> Tuple[str, int]:
        if sim.kind[i] == rules.PATROL:
            return self.decide_patrol(sim, i)
        return self.decide_car(sim, i)

    def decide_car(self, sim: _Sim, i: int) -> Tuple[str, int]:
        return (WAIT, sim.steps - sim.t)

    # --- xe tuần tra ------------------------------------------------------
    def follow(self, sim: _Sim, i: int) -> Optional[Tuple[str, int]]:
        """Đi tiếp lộ trình đang có; None nếu không còn ô nào phải đi."""
        route = self.routes.get(i)
        if not route:
            return None
        nxt = route[0]
        if sim.can_move(i, nxt):
            route.pop(0)
            return (MOVE, nxt)
        if sim.t + sim.cost_from(sim.pos[i]) > sim.steps:
            self.routes[i] = []
            return (WAIT, sim.steps - sim.t)
        return (WAIT, 1)  # thiếu xăng: chờ xe tiếp

    def fuel_ok(self, sim: _Sim, i: int, spot_cell: int, fuel_need: float) -> bool:
        """Mặc định: chỉ nhắm spot đi tới được bằng số xăng đang có."""
        return fuel_need <= sim.fuel[i]

    def choose_spot(self, sim: _Sim, i: int) -> Optional[int]:
        cfg = self.config
        here = sim.pos[i]
        left = sim.steps - sim.t
        claimed_brands = {cfg.spots[s].brand for j, s in self.targets.items() if j != i}
        claimed_spots: Dict[int, int] = {}
        for j, s in self.targets.items():
            if j != i:
                claimed_spots[s] = claimed_spots.get(s, 0) + 1
        best = None
        for s, spot in enumerate(cfg.spots):
            if s in sim.collected[i] or sim.stocks[s] - claimed_spots.get(s, 0) <= 0:
                continue
            d = self.paths.steps(here, spot.pos)
            if d == INF or d > left:
                continue
            if not self.fuel_ok(sim, i, spot.pos, self.paths.fuel(here, spot.pos)):
                continue
            if self.target_mode == "nearest":
                key = (d, s)
            else:
                if spot.brand not in self.match_brands and spot.brand not in sim.brands_today \
                        and spot.brand not in claimed_brands:
                    cls = 0
                elif spot.brand not in sim.brands_today and spot.brand not in claimed_brands:
                    cls = 1
                else:
                    cls = 2
                key = (cls, d, s)
            if best is None or key < best[0]:
                best = (key, s)
        return None if best is None else best[1]

    def decide_patrol(self, sim: _Sim, i: int) -> Tuple[str, int]:
        step = self.follow(sim, i)
        if step is not None:
            return step
        self.targets.pop(i, None)
        s = self.choose_spot(sim, i)
        if s is None:
            return self.no_target(sim, i)
        self.targets[i] = s
        self.routes[i] = self.paths.path(sim.pos[i], self.config.spots[s].pos)
        step = self.follow(sim, i)
        return step if step is not None else (WAIT, 1)

    def no_target(self, sim: _Sim, i: int) -> Tuple[str, int]:
        return (WAIT, sim.steps - sim.t)


class NearestBot(ScriptedBot):
    """Đội tham lam đơn giản: spot gần nhất theo số bước, toàn xe tuần tra, không tiếp xăng."""

    name = "nearest"
    target_mode = "nearest"


class ChainGreedyBot(ScriptedBot):
    """Tham lam theo chuỗi; 1 xe tiếp đuổi theo xe tuần tra ít xăng nhất."""

    name = "chain_greedy"
    target_mode = "chain"

    def n_cars(self) -> int:
        return 1

    def fuel_ok(self, sim, i, spot_cell, fuel_need) -> bool:
        # có xe tiếp đi theo: chấp nhận cạn xăng giữa đường rồi chờ được nạp
        return True

    def decide_car(self, sim: _Sim, i: int) -> Tuple[str, int]:
        patrols = [j for j in range(sim.n) if sim.kind[j] == rules.PATROL]
        if not patrols:
            return (WAIT, sim.steps - sim.t)
        target = min(patrols, key=lambda j: (sim.fuel[j], j))
        if sim.pos[target] == sim.pos[i]:
            return (WAIT, 1)
        path = self.paths.path(sim.pos[i], sim.pos[target])
        if not path or not sim.can_move(i, path[0]):
            return (WAIT, 1)
        return (MOVE, path[0])

    def no_target(self, sim: _Sim, i: int) -> Tuple[str, int]:
        return (WAIT, 1)


class EscortBot(ScriptedBot):
    """Đoàn xe: mỗi xe tiếp đi kèm một xe tuần tra (cùng ô, cùng lệnh), xe đó luôn đầy xăng.

    Ngày đầu xe tuần tra chờ xe tiếp tới ô của mình rồi mới đi. Xe tuần tra
    không có cặp chạy một mình với số xăng đang có.
    """

    name = "escort"
    target_mode = "chain"

    def n_cars(self) -> int:
        return self.config.n_agents // 2

    def start_day(self, agents) -> None:
        patrols = [i for i, a in enumerate(agents) if a.kind == rules.PATROL]
        cars = [i for i, a in enumerate(agents) if a.kind != rules.PATROL]
        self.pair_of_car: Dict[int, int] = {}
        self.pair_of_patrol: Dict[int, int] = {}
        free = list(patrols)
        for c in cars:
            if not free:
                break
            p = min(free, key=lambda j: (self.paths.steps(agents[c].pos, agents[j].pos), j))
            free.remove(p)
            self.pair_of_car[c] = p
            self.pair_of_patrol[p] = c

    def fuel_ok(self, sim, i, spot_cell, fuel_need) -> bool:
        return i in self.pair_of_patrol or fuel_need <= sim.fuel[i]

    def decide_patrol(self, sim: _Sim, i: int) -> Tuple[str, int]:
        car = self.pair_of_patrol.get(i)
        if car is not None and sim.pos[car] != sim.pos[i]:
            return (WAIT, 1)
        return super().decide_patrol(sim, i)

    def decide_car(self, sim: _Sim, i: int) -> Tuple[str, int]:
        p = self.pair_of_car.get(i)
        if p is None:
            return (WAIT, sim.steps - sim.t)
        if sim.pos[p] == sim.pos[i]:
            got = sim.issued[p]
            if got is not None and got[0] == sim.t:
                return got[1]  # cùng ô, cùng lệnh: luôn tới cùng lúc nên luôn nạp được
            return (WAIT, 1)
        path = self.paths.path(sim.pos[i], sim.pos[p])
        if not path or not sim.can_move(i, path[0]):
            return (WAIT, 1)
        return (MOVE, path[0])


def _central_plain(config: MatchConfig) -> int:
    """Ô đồng bằng có tổng số bước từ mọi spot tới nó nhỏ nhất (đường thông thoáng)."""
    paths = _Paths(config, {})
    from_spot = [paths.table(s.pos)[0] for s in config.spots]
    best = None
    for c in range(config.n_cells):
        if config.cells[c] != rules.PLAIN:
            continue
        total = sum(d[c] for d in from_spot)
        if best is None or total < best[0]:
            best = (total, c)
    return best[1] if best else config.agent_starts[0]


class HubBot(ScriptedBot):
    """Trạm cố định: 1 xe tiếp đứng ở ô trung tâm cả trận; xe tuần tra về trạm khi thiếu xăng."""

    name = "hub"
    target_mode = "chain"

    def __init__(self, config: MatchConfig, seed: int = 0):
        super().__init__(config, seed)
        self.hub: Optional[int] = None

    def n_cars(self) -> int:
        return 1

    def start_day(self, agents) -> None:
        if self.hub is None:
            self.hub = _central_plain(self.config)

    def fuel_ok(self, sim, i, spot_cell, fuel_need) -> bool:
        back = self.paths.fuel(spot_cell, self.hub)
        return fuel_need + back <= sim.fuel[i]

    def no_target(self, sim: _Sim, i: int) -> Tuple[str, int]:
        if sim.pos[i] == self.hub:
            car_here = any(sim.kind[j] != rules.PATROL and sim.pos[j] == self.hub for j in range(sim.n))
            if car_here or sim.fuel[i] >= self.config.fuel_limits:
                return (WAIT, sim.steps - sim.t)
            return (WAIT, 1)  # chờ xe tiếp tới trạm (ngày đầu)
        self.routes[i] = self.paths.path(sim.pos[i], self.hub)
        step = self.follow(sim, i)
        return step if step is not None else (WAIT, sim.steps - sim.t)

    def decide_car(self, sim: _Sim, i: int) -> Tuple[str, int]:
        if sim.pos[i] == self.hub:
            return (WAIT, sim.steps - sim.t)
        path = self.paths.path(sim.pos[i], self.hub)
        if not path or not sim.can_move(i, path[0]):
            return (WAIT, sim.steps - sim.t)
        return (MOVE, path[0])


def _choke_roads(config: MatchConfig, k: int) -> List[int]:
    """k ô đường mà nhiều đường ngắn nhất giữa các spot/điểm xuất phát đi qua nhất."""
    paths = _Paths(config, {})
    anchors = sorted({s.pos for s in config.spots} | set(config.agent_starts))
    count: Dict[int, int] = {}
    for a in anchors:
        for b in anchors:
            if a == b:
                continue
            for c in paths.path(a, b)[:-1]:
                if config.cells[c] == rules.ROAD:
                    count[c] = count.get(c, 0) + 1
    ranked = sorted(count, key=lambda c: (-count[c], c))
    return ranked[:k] or list(config.road_cells[:k])


class RoadBlockBot(ScriptedBot):
    """Đội chặn đường: xe tiếp đỗ cả trận trên các ô đường nhiều lộ trình đi qua nhất.

    Chỉ dùng để thử phòng thủ. Mỗi xe đỗ để lại `steps` lượt dừng mỗi ngày trên
    ô đó, đủ đẩy ô qua ngưỡng tắc trong hầu hết cấu hình. Xe tuần tra chạy tham
    lam theo chuỗi với số xăng mình có.
    """

    name = "road_block"
    target_mode = "chain"

    def __init__(self, config: MatchConfig, seed: int = 0):
        super().__init__(config, seed)
        self.parking: Dict[int, int] = {}

    def n_cars(self) -> int:
        return max(1, (self.config.n_agents - 1) // 2)

    def start_day(self, agents) -> None:
        if not self.parking:
            cars = [i for i, a in enumerate(agents) if a.kind != rules.PATROL]
            spots = _choke_roads(self.config, len(cars))
            for c, cell in zip(cars, spots):
                self.parking[c] = cell

    def decide_car(self, sim: _Sim, i: int) -> Tuple[str, int]:
        goal = self.parking.get(i)
        if goal is None or sim.pos[i] == goal:
            return (WAIT, sim.steps - sim.t)
        path = self.paths.path(sim.pos[i], goal)
        if not path or not sim.can_move(i, path[0]):
            return (WAIT, sim.steps - sim.t)
        return (MOVE, path[0])


class ServingsAlnsBot(SearchBot):
    """ALNS của ta với B rất nhỏ: điểm gần như chỉ là số phần (lỗi thiếu chuỗi của Naprock-18)."""

    name = "servings_alns"

    def __init__(self, config: MatchConfig, seed: int = 0, **kw):
        kw.setdefault("weights", Weights(B=0.05))
        super().__init__(config, seed=seed, **kw)
        self.name = "servings_alns"


class AllPatrolBot(SearchBot):
    """ALNS của ta, cố định toàn xe tuần tra (không xe tiếp)."""

    name = "all_patrol"

    def __init__(self, config: MatchConfig, seed: int = 0, **kw):
        kw.setdefault("kinds", [rules.PATROL] * config.n_agents)
        super().__init__(config, seed=seed, **kw)
        self.name = "all_patrol"


OPPONENT_TYPES = {
    "nearest": NearestBot,
    "chain_greedy": ChainGreedyBot,
    "escort": EscortBot,
    "hub": HubBot,
    "road_block": RoadBlockBot,
    "servings_alns": ServingsAlnsBot,
    "all_patrol": AllPatrolBot,
}

from .bots import BOT_TYPES  # noqa: E402

BOT_TYPES.update(OPPONENT_TYPES)
