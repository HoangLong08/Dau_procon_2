"""Simulator: nguồn chân lý duy nhất về luật trong ngày.

Mô hình bước theo Q&A Q6 và tài liệu bổ sung 補足資料 của ban tổ chức:

    bước 0      : chỉ pha HÀNH ĐỘNG
    bước 1..N-1 : pha PHẢN ÁNH rồi pha HÀNH ĐỘNG
    bước N      : chỉ pha PHẢN ÁNH

Pha phản ánh xử lý đúng thứ tự:
    1. trừ nhiên liệu (theo địa hình ô nguồn)
    2. áp dụng di chuyển
    3. thu udon (tranh kho theo chỉ số agent)
    4. tiếp nhiên liệu (xe tiếp cùng ô -> nạp đầy)
    5. cập nhật giao thông (đếm ô sau khi di chuyển)

Một đội được mô phỏng độc lập với đội khác: kho riêng, tiếp nhiên liệu chỉ trong
đội, đi chung ô là hợp lệ. Giao thông cộng dồn giữa các đội nằm ở tầng trên.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import rules
from .model import AgentState, MatchConfig


@dataclass
class PlanError:
    code: str
    agent: int
    token_index: int
    step: int          # bước mà lệnh được phát (pha hành động)
    detail: str = ""

    def __str__(self):
        return f"{self.code}(agent={self.agent}, token={self.token_index}, step={self.step}) {self.detail}"


@dataclass
class DayResult:
    ok: bool
    error: Optional[PlanError] = None
    end_pos: List[int] = field(default_factory=list)
    end_fuel: List[int] = field(default_factory=list)
    collections: List[Tuple[int, int, int]] = field(default_factory=list)  # (bước, agent, spot idx)
    stay: Dict[int, int] = field(default_factory=dict)                     # ô -> số stay step của đội
    stocks_left: List[int] = field(default_factory=list)
    brands: Set[int] = field(default_factory=set)                          # loại udon thu hôm nay

    @property
    def servings(self) -> int:
        return len(self.collections)


def _normalise_plan(plan, n_agents) -> Optional[PlanError]:
    if not isinstance(plan, (list, tuple)) or len(plan) != n_agents:
        return PlanError(rules.E_BAD_FORMAT, -1, -1, -1, f"cần {n_agents} dãy hành động")
    for i, seq in enumerate(plan):
        if not isinstance(seq, (list, tuple)):
            return PlanError(rules.E_BAD_FORMAT, i, -1, -1, "dãy hành động không phải list")
        for k, tok in enumerate(seq):
            if not isinstance(tok, int) or isinstance(tok, bool):
                return PlanError(rules.E_BAD_FORMAT, i, k, -1, f"token không phải số nguyên: {tok!r}")
            if tok > 5:
                return PlanError(rules.E_BAD_FORMAT, i, k, -1, f"token ngoài phạm vi: {tok}")
    return None


def run_day(
    config: MatchConfig,
    steps: int,
    agents: Sequence[AgentState],
    plan: Sequence[Sequence[int]],
    stocks: Sequence[int],
    road_status: Optional[Dict[int, int]] = None,
    trace: Optional[list] = None,
) -> DayResult:
    """Chạy một ngày cho MỘT đội. Trả DayResult; ok=False kèm lỗi đầu tiên gặp phải.

    `trace`, nếu có, nhận (bước, vị trí, nhiên liệu) ở bước 0 và sau pha phản
    ánh của mỗi bước 1..N — đúng các frame replay server gửi cho đội mình.
    Không đổi kết quả.
    """
    n = len(agents)
    err = _normalise_plan(plan, n)
    if err is not None:
        return DayResult(ok=False, error=err)

    road_status = road_status or {}
    cells = config.cells
    neighbors = config.neighbors
    spot_at = config.spot_index_at
    fuel_limit = config.fuel_limits

    pos = [a.pos for a in agents]
    fuel = [a.fuel for a in agents]
    kind = [a.kind for a in agents]
    cursor = [0] * n
    # pending[i] = (bước kết thúc, ô đích hoặc -1 nếu đợi, ô nguồn, chỉ số token, bước phát lệnh)
    pending: List[Optional[Tuple[int, int, int, int, int]]] = [None] * n
    collected: List[Set[int]] = [set() for _ in range(n)]
    stocks = list(stocks)
    stay: Dict[int, int] = {}
    collections: List[Tuple[int, int, int]] = []
    if trace is not None:
        trace.append((0, list(pos), list(fuel)))

    def cell_step_cost(cell: int) -> int:
        terrain = cells[cell]
        if terrain == rules.ROAD:
            return rules.STEP_ROAD[road_status.get(cell, rules.SMOOTH)]
        return rules.step_cost(terrain)

    def issue(i: int, t: int) -> Optional[PlanError]:
        """Pha hành động: nhận lệnh kế tiếp cho agent i tại bước t."""
        seq = plan[i]
        k = cursor[i]
        if k >= len(seq):
            return PlanError(rules.E_STEP_OVERFLOW, i, k, t, "hết lệnh nhưng ngày chưa kết thúc")
        tok = seq[k]
        cursor[i] = k + 1
        if tok < 0:
            wait = -tok
            if t + wait > steps:
                return PlanError(rules.E_STEP_OVERFLOW, i, k, t, f"đợi {wait} bước vượt quá ngày")
            pending[i] = (t + wait, -1, pos[i], k, t)
            return None
        if tok < 0 or tok > 5:
            return PlanError(rules.E_BAD_FORMAT, i, k, t, f"hướng không hợp lệ: {tok}")
        src = pos[i]
        dst = neighbors[src][tok]
        if dst < 0:
            return PlanError(rules.E_NOT_ADJACENT, i, k, t, f"ra ngoài bản đồ từ ô {src} hướng {tok}")
        if cells[dst] == rules.POND:
            return PlanError(rules.E_POND, i, k, t, f"ô đích {dst} là ao")
        cost = cell_step_cost(src)
        if cost == rules.IMPASSABLE:
            return PlanError(rules.E_POND, i, k, t, f"đang ở ao {src}")
        if t + cost > steps:
            return PlanError(rules.E_STEP_OVERFLOW, i, k, t, f"cần {cost} bước, còn {steps - t}")
        pending[i] = (t + cost, dst, src, k, t)
        return None

    # bước 0: chỉ pha hành động
    for i in range(n):
        e = issue(i, 0)
        if e is not None:
            return DayResult(ok=False, error=e)

    for t in range(1, steps + 1):
        # 1. trừ nhiên liệu
        for i in range(n):
            p = pending[i]
            if p is None or p[0] != t or p[1] < 0:
                continue
            if kind[i] != rules.PATROL:
                continue
            cost = rules.fuel_cost(cells[p[2]])
            if fuel[i] < cost:
                return DayResult(
                    ok=False,
                    error=PlanError(rules.E_NO_FUEL, i, p[3], p[4], f"cần {cost}, còn {fuel[i]} tại ô {p[2]}"),
                )
            fuel[i] -= cost
        # 2. áp dụng di chuyển
        for i in range(n):
            p = pending[i]
            if p is not None and p[0] == t and p[1] >= 0:
                pos[i] = p[1]
        # 3. thu udon, theo chỉ số agent
        for i in range(n):
            if kind[i] != rules.PATROL:
                continue
            s = spot_at.get(pos[i])
            if s is None or s in collected[i] or stocks[s] <= 0:
                continue
            stocks[s] -= 1
            collected[i].add(s)
            collections.append((t, i, s))
        # 4. tiếp nhiên liệu
        refuel_cells = {pos[i] for i in range(n) if kind[i] == rules.REFUEL}
        if refuel_cells:
            for i in range(n):
                if kind[i] == rules.PATROL and pos[i] in refuel_cells:
                    fuel[i] = fuel_limit
        # 5. cập nhật giao thông
        for i in range(n):
            c = pos[i]
            stay[c] = stay.get(c, 0) + 1
        if trace is not None:
            trace.append((t, list(pos), list(fuel)))
        # dọn pending đã xong
        for i in range(n):
            if pending[i] is not None and pending[i][0] == t:
                pending[i] = None
        # pha hành động (bước cuối không có)
        if t < steps:
            for i in range(n):
                if pending[i] is None:
                    e = issue(i, t)
                    if e is not None:
                        return DayResult(ok=False, error=e)

    # thừa lệnh cũng là sai định dạng ngày
    for i in range(n):
        if cursor[i] < len(plan[i]):
            return DayResult(
                ok=False,
                error=PlanError(rules.E_STEP_OVERFLOW, i, cursor[i], steps, "còn lệnh thừa sau khi hết ngày"),
            )

    brands = {config.spots[s].brand for (_, _, s) in collections}
    return DayResult(
        ok=True,
        end_pos=pos,
        end_fuel=fuel,
        collections=collections,
        stay=stay,
        stocks_left=stocks,
        brands=brands,
    )


def validate(config, steps, agents, plan, stocks, road_status=None) -> Optional[PlanError]:
    """Chỉ kiểm hợp lệ, bỏ kết quả."""
    return run_day(config, steps, agents, plan, stocks, road_status).error


def wait_plan(n_agents: int, steps: int) -> List[List[int]]:
    """Kế hoạch đứng yên cả ngày: luôn hợp lệ."""
    return [[-steps] for _ in range(n_agents)]


def road_status_from_history(config: MatchConfig, stay_history: Sequence[Dict[int, int]]) -> Dict[int, int]:
    """Trạng thái đường cho ngày kế, từ stay-step CỘNG DỒN MỌI ĐỘI của tối đa 2 ngày trước."""
    status: Dict[int, int] = {}
    recent = list(stay_history)[-2:]
    players = max(1, config.players)
    for cell in config.road_cells:
        volume = sum(day.get(cell, 0) for day in recent) / players
        status[cell] = rules.road_status_for(volume, config.busy_threshold, config.jammed_threshold)
    return status
