"""Biểu diễn nghiệm một ngày và ngữ cảnh lập kế hoạch."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .. import rules
from ..model import AgentState, MatchConfig
from ..pathing import DistTable, LegBook


@dataclass(frozen=True)
class Meet:
    """Điểm hẹn tiếp nhiên liệu.

    patrol/car là chỉ số SLOT trong danh sách xe tuần tra / xe tiếp của ngữ cảnh.
    stop_idx = -1 nghĩa là gặp ngay tại ô xuất phát của xe tuần tra (cứu xe hết xăng).
    """
    patrol: int
    stop_idx: int
    car: int


@dataclass
class DaySolution:
    tours: List[List[int]]                          # theo slot xe tuần tra -> dãy chỉ số spot
    meets: List[Meet] = field(default_factory=list)
    waits: Dict[Tuple[int, int], int] = field(default_factory=dict)

    def copy(self) -> "DaySolution":
        return DaySolution(
            tours=[list(t) for t in self.tours],
            meets=list(self.meets),
            waits=dict(self.waits),
        )

    def stops(self) -> List[Tuple[int, int]]:
        return [(p, i) for p, tour in enumerate(self.tours) for i in range(len(tour))]

    def total_stops(self) -> int:
        return sum(len(t) for t in self.tours)


@dataclass
class Weights:
    """Trọng số hàm mục tiêu. B là đơn vị 'một loại udon trong ngày'."""
    B: float = 1000.0
    new_brand: float = 0.5          # × B, cho loại chưa từng thu trong trận
    serving: float = 1.0            # tuyệt đối, phải nhỏ hơn B
    cover_penalty: float = 1000.0   # × B, chuỗi không thể ghé kịp phần trận còn lại
    urgency: float = 0.6            # × B
    end_on_spot: float = 0.5        # × B
    near_chain: float = 0.4         # × B
    strand: float = 0.8             # × B × số ngày còn lại
    fuel_bonus: float = 0.2         # × B, theo tỉ lệ xăng còn lại
    road_park: float = 0.05         # × B, phạt stay-step trên đường của chính mình
    diag_penalty: float = 0.01      # × B, phạt tour bị cắt / điểm hẹn bị bỏ


@dataclass
class DayContext:
    """Mọi thứ planner cần để lập kế hoạch cho đúng một ngày."""
    config: MatchConfig
    day: int
    steps: int
    agents: List[AgentState]
    road_status: Dict[int, int]
    stocks: List[int]
    visited_brands: Set[int]
    book: LegBook
    dist_step: DistTable
    patrols: List[int]
    cars: List[int]
    weights: Weights = field(default_factory=Weights)
    dist_next: Optional[DistTable] = None        # bảng dự báo ngày mai (mục 7 của spec)
    next_steps: int = 0
    days_left: int = 0

    # dẫn xuất
    spot_cells: List[int] = field(init=False)
    spot_brand: List[int] = field(init=False)
    step_cost_of: List[int] = field(init=False)

    def __post_init__(self):
        self.spot_cells = [s.pos for s in self.config.spots]
        self.spot_brand = [s.brand for s in self.config.spots]
        cost = []
        for c in range(self.config.n_cells):
            terrain = self.config.cells[c]
            if terrain == rules.ROAD:
                cost.append(rules.STEP_ROAD[self.road_status.get(c, rules.SMOOTH)])
            else:
                cost.append(rules.step_cost(terrain))
        self.step_cost_of = cost

    @property
    def n_patrols(self) -> int:
        return len(self.patrols)

    @property
    def n_cars(self) -> int:
        return len(self.cars)

    def patrol_agent(self, slot: int) -> int:
        return self.patrols[slot]

    def car_agent(self, slot: int) -> int:
        return self.cars[slot]

    def start_of_patrol(self, slot: int) -> int:
        return self.agents[self.patrols[slot]].pos

    def fuel_of_patrol(self, slot: int) -> int:
        return self.agents[self.patrols[slot]].fuel

    def start_of_car(self, slot: int) -> int:
        return self.agents[self.cars[slot]].pos

    def empty_solution(self) -> DaySolution:
        return DaySolution(tours=[[] for _ in self.patrols])


def build_context(
    config: MatchConfig,
    day: int,
    agents: Sequence[AgentState],
    road_status: Dict[int, int],
    stocks: Sequence[int],
    visited_brands: Set[int],
    book: LegBook,
    dist_step: DistTable,
    dist_next: Optional[DistTable] = None,
    weights: Optional[Weights] = None,
) -> DayContext:
    patrols = [i for i, a in enumerate(agents) if a.kind == rules.PATROL]
    cars = [i for i, a in enumerate(agents) if a.kind == rules.REFUEL]
    steps = config.steps_of_day(day)
    next_steps = config.steps_of_day(day + 1) if day + 1 < config.n_days else 0
    return DayContext(
        config=config,
        day=day,
        steps=steps,
        agents=list(agents),
        road_status=road_status,
        stocks=list(stocks),
        visited_brands=set(visited_brands),
        book=book,
        dist_step=dist_step,
        patrols=patrols,
        cars=cars,
        weights=weights or Weights(),
        dist_next=dist_next,
        next_steps=next_steps,
        days_left=max(0, config.n_days - day - 1),
    )
