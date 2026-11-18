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
    new_brand: float = 0.5          # × B, cho loại chưa từng thu trong trận; luôn bị nâng
                                    # lên ít nhất (số chuỗi + 1), xem DayContext.new_brand_value
    serving: float = 1.0            # tuyệt đối, phải nhỏ hơn B
    cover_penalty: float = 1000.0   # × B, chuỗi không thể ghé kịp phần trận còn lại
    urgency: float = 0.6            # × B
    end_on_spot: float = 0.5        # × B
    near_chain: float = 0.4         # × B
    strand: float = 0.8             # × B × số ngày còn lại
    # × B, theo tỉ lệ xăng còn lại. 0,2 cũ định giá xăng quá thấp: `ab` 54 trận nhiều đội
    # (07/10/2026) cho 1,0 hơn 0,2 +1,5 ± 1,0 D mỗi trận; 2,0 ngang 1,0 nhưng có trận thua đậm
    fuel_bonus: float = 1.0
    road_park: float = 0.05         # × B, phạt stay-step trên đường của chính mình
    diag_penalty: float = 0.01      # × B, phạt tour bị cắt / điểm hẹn bị bỏ
    # --- L cứng (R1, 16/11/2026). Đặt new_chain_first=0, deadline=0 là hành vi cũ ---
    #: 1 = xét spot của chuỗi chưa thu ngoài NEAR_K, chèn chúng trước theo regret, và vá L
    #: sau ALNS. Bản cũ chỉ xét 10 spot gần điểm neo nên bỏ sót chuỗi một-spot ở xa
    new_chain_first: float = 1.0
    #: × new_brand_value, cho chuỗi chưa thu mà xe gần nhất cuối ngày đã xa hơn
    #: DEADLINE_MARGIN × số bước còn lại của trận: kéo xe về phía nó trước khi quá muộn
    deadline: float = 1.0
    # --- pha Q (R2). q_share=0 là hành vi cũ ---
    #: phần ngân sách (vòng hoặc giây) cho pha 2: khoá (chuỗi mới, D) hôm nay rồi tối đa Q.
    #: 0,3 tới 17/11; 0,5 (cùng q_B 0,05) từ 18/11 (báo cáo 4.11)
    q_share: float = 0.5
    #: hệ số nhân các hạng mục cuối ngày "mềm" trong pha 2 (urgency, end_on_spot,
    #: near_chain, fuel_bonus, road_park); cover_penalty, strand, deadline giữ nguyên
    q_end_scale: float = 0.1
    #: dải chấp nhận RRT của pha 2, tính theo số phần (× serving)
    q_band: float = 2.0
    #: > 0: trong pha 2 thu B về giá trị này (như bot servings_alns, B = 0,05) để phép chèn
    #: ưu tiên số phần; (chuỗi mới, D) vẫn bị khoá bằng accept, còn cover_penalty, strand,
    #: deadline được nhân bù để giữ nguyên độ lớn tuyệt đối. 0 = giữ B như pha 1. Bật 0,05 từ
    #: 18/11: cả trận trên 4 bản đồ contest +40 Q trên nền soft_unit (báo cáo 4.11)
    q_B: float = 0.05
    # --- rollout cuối ngày (R4). rollout_k=0 là hành vi trước đó ---
    #: số nghiệm khác nhau về trạng thái cuối ngày được chơi tiếp tới hết trận để chọn
    rollout_k: float = 8.0
    #: phần ngân sách cho R4: một nửa chạy hai lượt ALNS biến thể (tiết kiệm xăng, cuối
    #: ngày ×2) để có ứng viên khác hẳn, nửa còn lại chơi tiếp (theo vòng: nửa đầu là vòng
    #: ALNS lấy từ pha 1, phần chơi tiếp không tốn vòng)
    rollout_share: float = 0.2
    #: số ngày chơi tiếp khi chấm một ứng viên; 0 = tới hết trận
    rollout_days: float = 3.0
    #: R3: 1 = ngày cuối, đạt cận trên (chuỗi mới, D, Q) của hôm nay thì dừng tìm và gửi ngay
    ub_stop: float = 1.0
    # --- giao thông (R5, R6). 0 là hành vi trước đó ---
    #: R6: 1 = dự báo tự hiệu chỉnh mỗi sáng theo đường thật (traffic.ForecastCalibrator).
    #: Trước B4 thiệt (D −0,33, báo cáo 4.7); có hàm giá trị học được (B4) thì có lợi: 120 cặp,
    #: D +0,56 ± 0,27, Q +0,87 (báo cáo 4.9). Khi đã có R7 thì trung tính. Bật từ 17/11/2026
    traffic_calib: float = 1.0
    #: R5: chọn vai trò chấm theo 0 = dự báo cũ, 1 = cộng dự báo cũ với giao thông nhiều đội
    #: (traffic.PRIOR), 2 = chỉ giao thông nhiều đội; > 0 còn chặn 0 xe tiếp khi thiếu xăng
    role_traffic: float = 1.0
    # --- hàm giá trị học được (B4, 17/11/2026). 0 là hành vi trước đó ---
    #: > 0: ghép value_weight × B × V(trạng thái cuối ngày) vào end_value theo mô hình đi kèm
    #: (planner/value.py, value_model.json: tuyến tính, cộng vào end_value đặt tay). Giải
    #: đấu 120 cặp ở sảnh 10 đội: D +0,62 ± 0,38, Q +2,51 ± 0,80, không mất L (báo cáo 4.8);
    #: 0,5 trung tính, 2,0 ít Q hơn
    value_weight: float = 1.0
    # --- đơn vị của hạng mục cuối ngày mềm (18/11/2026). soft_unit = 0 là hành vi trước đó ---
    #: > 0: end_on_spot, near_chain, fuel_bonus, road_park và value_weight × V tính theo đơn vị
    #: này (điểm, 1 = một phần) thay cho B. urgency, cover_penalty, strand, deadline (giữ L và
    #: D tương lai) vẫn theo B. Trên server VN (D chạm trần mọi ngày) hạng mục mềm theo B đổi
    #: hàng chục phần lấy bình xăng đầy: thua DAU 2 tới 135 phần một trận (báo cáo 4.11).
    #: Bật 20 từ 18/11: cả trận trên 4 bản đồ contest Q +77, D giữ nguyên
    soft_unit: float = 20.0
    #: 1 = chỉ dùng soft_unit khi D an toàn: đã đủ L và ngày hôm trước đạt trần D (mọi chuỗi);
    #: ngày khác giữ B. 0 = dùng soft_unit mọi ngày (nếu > 0). Chọn vai trò luôn chơi thử theo B
    soft_auto: float = 1.0
    #: > 0: kẹp tổng hạng mục mềm (kể cả value_weight × V) trong ±soft_cap × B, để không bao
    #: giờ đổi một chuỗi hôm nay lấy trạng thái cuối ngày "đẹp" hơn (near_chain 0,4 B × số
    #: chuỗi gần cộng bình xăng tới 1 B từng thắng 1 chuỗi: f06e ngày 5 hụt 1 chuỗi). 0 = như cũ
    soft_cap: float = 0.0
    # --- chia vai trò xe (18/11/2026). role_all_positions = role_d_tolerance = role_finalists = 0,
    # role_seeds = 1 là hành vi trước đó. Bật từ 18/11: bản đồ VN Q +20 ± 11, sảnh 10 đội trung tính ---
    #: 1 = ứng viên gồm MỌI vị trí cho 1 xe tiếp và mọi cặp (≤ 6 xe; không thì cặp trong 4 xe
    #: trung tâm nhất) cho 2 xe tiếp. Bản cũ chỉ thử xe trung tâm nhất + 1 phương án thay thế
    role_all_positions: float = 1.0
    #: > 0: khi xếp hạng, D trong khoảng (tỉ lệ này × D tốt nhất) coi như hoà rồi so Q. D của
    #: lần chơi thử lệch 1–6 so với ALNS thật (vốn chạm trần với mọi phương án trên server VN)
    role_d_tolerance: float = 0.02
    #: > 0: số phương án vào vòng 2 (ALNS); 0 = như cũ (2, hoặc 3 khi chạy song song)
    role_finalists: float = 4.0
    #: số seed ALNS cho mỗi phương án ở vòng 2 (nhiễu giữa hai seed tới 10–40 phần); ≤ 1 = như cũ.
    #: Khi chạy theo giây, số seed tự giảm cho vừa một lượt tiến trình
    role_seeds: float = 2.0
    # --- tìm kiếm cục bộ nước đi nhỏ (Bước 2, planner/local.py). 0 là hành vi trước đó ---
    #: phần ngân sách (vòng hoặc giây) cho ủ mô phỏng trên nước đi nhỏ, lấy từ pha Q (≤ q_share):
    #: pha Q chạy ALNS trên (q_share − ls_share) rồi tìm kiếm cục bộ trên ls_share. Để tắt: cùng
    #: giờ (5 s/ngày) không hơn ALNS (3.138 so với 3.143 Q, báo cáo 4.11)
    ls_share: float = 0.0
    #: số nước đi tìm kiếm cục bộ cho mỗi vòng ALNS được nhường (khi ngân sách tính theo vòng)
    ls_moves_per_iter: float = 1.0
    #: 1 = vá D hôm nay như R1 vá L: chuỗi chưa thu HÔM NAY (dù đã thu trong trận) mà một xe
    #: tuần tra tới được thì thử chèn, được bỏ điểm trùng chuỗi hoặc cắt đuôi để lấy chỗ
    #: (engine.ensure_today_chains). Ngày 69/74 bước của contest 17/11 ta hụt 1 chuỗi, DAU 2 đủ.
    #: Bật từ 18/11: cùng điểm xuất phát với DAU 2, số ngày thiếu D từ 3 về 0
    today_chain_first: float = 1.0

    # --- hàm giá trị V-VN theo số phần (18/11/2026, planner/value_model_vn.json). 0 là như trước ---
    #: > 0: ở ngày D an toàn (soft_unit > 0), phần mềm cộng value_q_weight × V_vn (số phần tương lai
    #: dự đoán, so với các nghiệm cùng ngày). Học bằng thi_nghiem_ai/hoc_v_vn.py
    value_q_weight: float = 0.0
    #: > 0: ở những ngày dùng V-VN thì bỏ V1 (value_weight), chỉ giữ V-VN
    value_q_replace: float = 0.0

    @property
    def soft(self) -> float:
        """Đơn vị của hạng mục cuối ngày mềm: soft_unit nếu > 0, không thì B (hành vi cũ)."""
        return self.soft_unit if self.soft_unit > 0 else self.B


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
    def new_brand_value(self) -> float:
        """Điểm thưởng cho mỗi loại udon mới cả trận (tiêu chí 1, L).

        Một ngày chỉ thêm được tối đa (số chuỗi) × B cho D, nên thưởng L phải lớn
        hơn mức đó: một loại mới luôn hơn mọi số loại đã có (L > D > Q, cases 9.7).
        """
        w = self.weights
        return w.B * max(w.new_brand, len(self.config.brands) + 1)

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
    steps_override: Optional[int] = None,
) -> DayContext:
    patrols = [i for i, a in enumerate(agents) if a.kind == rules.PATROL]
    cars = [i for i, a in enumerate(agents) if a.kind == rules.REFUEL]
    steps = int(steps_override) if steps_override else config.steps_of_day(day)
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
