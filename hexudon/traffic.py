"""Dự báo giao thông (mức 1, phòng thủ).

Hành lang đường mà mọi đội cùng đi sẽ đông đúc rồi ùn tắc dù không ai cố ý.
Bot dự báo điều đó để khai thác đường ở ngày 0 và không kết thúc ngày ở phía
sai của một hành lang sắp tắc.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import rules
from .model import MatchConfig
from .pathing import DayGraph, DistTable

# Số lượt trung bình MỘT ĐỘI đi qua ô đường phổ biến nhất trong một ngày.
# Không nhân theo số xe: một đội đông xe thì các xe rải ra nhiều hành lang khác nhau.
# Giá trị mặc định là ước lượng ban đầu, phải hiệu chỉnh bằng benchmark (mục 7 của spec).
EXPECTED_PASSES_TOP_CELL = 1.5

# Hệ số né: đường càng xấu thì các đội càng đi vòng, nên số lượt giảm.
AVOIDANCE = (1.0, 0.7, 0.35)  # thông thoáng, đông đúc, ùn tắc


@dataclass(frozen=True)
class ForecastModel:
    """Một giả định về lưu lượng của các đội khác (R6, 16/11/2026).

    Lưu lượng một ngày của MỖI đội khác trên một ô được ước bằng
    like_us × vết của ta + pop_scale × độ phổ biến × lượt dừng một lần qua ô.
    `persist` là cách ngây thơ "mai như hôm nay" (E5: đúng hơn dự báo cũ ở 6/6
    trận 10 đội). `legacy` là công thức trước R6, giữ làm mốc so.
    """

    like_us: float = 0.0
    pop_scale: float = 0.0
    persist: bool = False
    legacy: bool = False
    name: str = ""

    def label(self) -> str:
        if self.name:
            return self.name
        if self.persist:
            return "mai như hôm nay"
        if self.legacy:
            return "công thức cũ"
        return f"như ta {self.like_us:g}, phổ biến {self.pop_scale:g}"


#: mô hình dùng khi chưa quan sát được ngày nào (ngày 0, chọn vai trò). Lấy theo
#: đo trên sảnh 10 đội (báo cáo mục 4.7, 40 đội-trận L1 + L2): mô hình đúng nhất trung
#: bình, sai số 0,385 mỗi ô mỗi sáng (công thức cũ 0,583; "mai như hôm nay" 0,436)
PRIOR = ForecastModel(like_us=0.5, pop_scale=0.0)
PERSIST = ForecastModel(persist=True)
LEGACY = ForecastModel(legacy=True)


def default_models() -> List[ForecastModel]:
    """Bộ ứng viên của ForecastCalibrator: lưới (như ta, phổ biến), cộng hai mốc."""
    grid = [ForecastModel(like_us=a, pop_scale=k)
            for a in (0.0, 0.25, 0.5, 1.0) for k in (0.0, 0.5, 1.0, 2.0)]
    return grid + [PERSIST, LEGACY]


def popularity(config: MatchConfig) -> Dict[int, float]:
    """Độ phổ biến tĩnh của từng ô đường, trong [0, 1].

    Đếm số đường ít bước nhất (giả định mọi đường thông thoáng) giữa mọi cặp
    nút chính đi qua mỗi ô đường, rồi chuẩn hoá theo giá trị lớn nhất.
    """
    if not config.road_cells:
        return {}
    graph = DayGraph(config, road_override=rules.SMOOTH)
    nodes = sorted({s.pos for s in config.spots} | set(config.agent_starts))
    table = DistTable(graph, nodes, "step")
    road = set(config.road_cells)
    count: Dict[int, int] = {c: 0 for c in road}
    for a in nodes:
        for b in nodes:
            if a == b:
                continue
            for cell in table.path(a, b):
                if cell in road:
                    count[cell] += 1
    top = max(count.values()) if count else 0
    if top <= 0:
        return {c: 0.0 for c in road}
    return {c: count[c] / top for c in road}


def _pass_cost(status: int) -> int:
    """Số stay-step một lượt đi qua để lại trên ô đường (bằng số bước rời ô)."""
    return rules.STEP_ROAD[status]


class TrafficForecast:
    """Dự báo trạng thái đường của ngày kế tiếp."""

    def __init__(self, config: MatchConfig, n_patrols: int = 0,
                 expected_passes: float = EXPECTED_PASSES_TOP_CELL,
                 pop: Optional[Dict[int, float]] = None,
                 model: Optional[ForecastModel] = None):
        self.config = config
        self.pop = popularity(config) if pop is None else pop
        self.n_patrols = n_patrols
        # đội nhiều xe hơn đi qua hành lang chính nhiều hơn một chút, không tuyến tính
        scale = 1.0 + 0.1 * max(0, n_patrols - 3)
        self.expected_passes = expected_passes * scale
        #: None (hoặc LEGACY) = công thức trước R6, đúng từng số như cũ
        self.model = model

    def with_model(self, model: Optional[ForecastModel]) -> "TrafficForecast":
        """Bản sao dùng chung bảng độ phổ biến, khác giả định lưu lượng."""
        out = TrafficForecast.__new__(TrafficForecast)
        out.config = self.config
        out.pop = self.pop
        out.n_patrols = self.n_patrols
        out.expected_passes = self.expected_passes
        out.model = model
        return out

    def _pass_weight(self, status: int) -> float:
        """Stay-step kỳ vọng một đội để lại trên ô, đã tính cả việc né đường xấu."""
        return AVOIDANCE[status] * _pass_cost(status)

    def predict(
        self,
        status_today: Dict[int, int],
        status_yesterday: Optional[Dict[int, int]] = None,
        own_stay_today: Optional[Dict[int, int]] = None,
        opponents_parked: Optional[Dict[int, int]] = None,
        steps_today: int = 0,
        own_stay_yesterday: Optional[Dict[int, int]] = None,
        floor_today: bool = False,
        parked_stay: Optional[Dict[int, int]] = None,
    ) -> Dict[int, int]:
        """Trạng thái đường dự báo cho ngày mai.

        Lưu lượng ngày mai gồm vết của hôm nay và hôm qua (cases 2), nên vết đã biết
        chắc của chính đội ở cả hai ngày đều được cộng (cases 8.13).

        `floor_today=True` khi hôm nay là ngày 2 (day = 1): lưu lượng ngày 3 bằng vết
        ngày 1 cộng vết ngày 2, còn ngày 2 chỉ có vết ngày 1, nên mọi ô ngày mai ít
        nhất cũng tệ như hôm nay (cases 10.3).

        Có `self.model` (R6) thì lưu lượng các đội khác theo mô hình đó, và xe đỗ
        của đối thủ lấy theo `parked_stay` (lượt dừng, xem parked_stay()) thay cho
        `opponents_parked`.
        """
        model = self.model
        if model is not None and not model.legacy:
            return self._predict_model(model, status_today, status_yesterday, own_stay_today,
                                       own_stay_yesterday, parked_stay, floor_today)
        config = self.config
        players = max(1, config.players)
        own_today = own_stay_today or {}
        own_yesterday = own_stay_yesterday or {}
        parked = opponents_parked or {}
        out: Dict[int, int] = {}
        for cell in config.road_cells:
            pop = self.pop.get(cell, 0.0)
            est = pop * self.expected_passes * self._pass_weight(status_today.get(cell, rules.SMOOTH))
            if status_yesterday is not None:
                est += pop * self.expected_passes * self._pass_weight(status_yesterday.get(cell, rules.SMOOTH))
            est += (own_today.get(cell, 0) + own_yesterday.get(cell, 0)) / players
            est += parked.get(cell, 0) * steps_today / players
            status = rules.road_status_for(est, config.busy_threshold, config.jammed_threshold)
            if floor_today:
                status = max(status, status_today.get(cell, rules.SMOOTH))
            out[cell] = status
        return out

    def _predict_model(self, model: ForecastModel, status_today, status_yesterday,
                       own_stay_today, own_stay_yesterday, parked_stay, floor_today) -> Dict[int, int]:
        config = self.config
        if model.persist:
            return {c: status_today.get(c, rules.SMOOTH) for c in config.road_cells}
        players = max(1, config.players)
        others = players - 1
        own_today = own_stay_today or {}
        own_yesterday = own_stay_yesterday or {}
        parked = parked_stay or {}
        a, k = model.like_us, model.pop_scale
        out: Dict[int, int] = {}
        for cell in config.road_cells:
            pop = self.pop.get(cell, 0.0)
            mine_today = own_today.get(cell, 0)
            mine_yesterday = own_yesterday.get(cell, 0)
            today = status_today.get(cell, rules.SMOOTH)
            volume = mine_today + mine_yesterday + parked.get(cell, 0)
            volume += others * (a * mine_today + k * pop * self._pass_weight(today))
            if status_yesterday is not None:
                volume += others * (a * mine_yesterday
                                    + k * pop * self._pass_weight(status_yesterday.get(cell, rules.SMOOTH)))
            status = rules.road_status_for(volume / players, config.busy_threshold, config.jammed_threshold)
            if floor_today:
                status = max(status, today)
            out[cell] = status
        return out

    def static_guess(self, day_index: int) -> Dict[int, int]:
        """Dự báo khi chưa có quan sát (dùng trong rollout chọn vai trò)."""
        if day_index <= 0:
            return {c: rules.SMOOTH for c in self.config.road_cells}
        status = {c: rules.SMOOTH for c in self.config.road_cells}
        for _ in range(min(day_index, 3)):
            status = self.predict(status, status if day_index > 1 else None)
        return status


def accuracy(predicted: Dict[int, int], actual: Dict[int, int]) -> Dict[str, float]:
    """Độ chính xác dự báo: tỉ lệ đúng và tỉ lệ lạc quan (dự báo nhẹ hơn thực tế)."""
    cells = set(predicted) | set(actual)
    if not cells:
        return {"correct": 1.0, "optimistic": 0.0, "n": 0}
    correct = 0
    optimistic = 0
    for c in cells:
        p = predicted.get(c, rules.SMOOTH)
        a = actual.get(c, rules.SMOOTH)
        if p == a:
            correct += 1
        elif p < a:
            optimistic += 1
    n = len(cells)
    return {"correct": correct / n, "optimistic": optimistic / n, "n": n}


def counts_from_positions(positions: Sequence[int], road_cells: Sequence[int]) -> Dict[int, int]:
    """Đếm số agent đối thủ đang đứng trên từng ô đường."""
    road = set(road_cells)
    out: Dict[int, int] = {}
    for p in positions:
        if p in road:
            out[p] = out.get(p, 0) + 1
    return out


def parked_stay(
    previous: Optional[Sequence[Sequence[int]]],
    current: Sequence[Sequence[int]],
    road_cells: Sequence[int],
    status: Dict[int, int],
    steps: int,
) -> Dict[int, int]:
    """Lượt dừng dự kiến hôm nay của xe đối thủ đang đứng trên ô đường lúc sáng.

    `previous`, `current`: vị trí theo [đội][xe] của sáng hôm qua và sáng nay.
    Xe đứng cùng một ô đường hai sáng liền coi như đỗ cả ngày (`steps` lượt);
    xe chỉ đứng đó sáng nay đang đi qua, để lại đúng số bước rời ô theo trạng thái
    hôm nay (báo cáo 5.4.1).
    """
    road = set(road_cells)
    out: Dict[int, int] = {}
    for ti, team in enumerate(current):
        before = previous[ti] if previous is not None and ti < len(previous) else None
        for ai, pos in enumerate(team):
            if pos not in road:
                continue
            parked = before is not None and ai < len(before) and before[ai] == pos
            stay = steps if parked else rules.STEP_ROAD[status.get(pos, rules.SMOOTH)]
            out[pos] = out.get(pos, 0) + stay
    return out


def _abs_error(predicted: Dict[int, int], actual: Dict[int, int],
               cells: Sequence[int]) -> Tuple[int, int, int, int]:
    """(tổng |lệch|, số ô đúng, số ô thật sự không thông, số ô không thông đoán đúng)."""
    err = right = busy = busy_right = 0
    for c in cells:
        p = predicted.get(c, rules.SMOOTH)
        a = actual.get(c, rules.SMOOTH)
        err += abs(p - a)
        if p == a:
            right += 1
        if a != rules.SMOOTH:
            busy += 1
            if p == a:
                busy_right += 1
    return err, right, busy, busy_right


class ForecastCalibrator:
    """Chọn giả định lưu lượng theo trạng thái đường thật thấy mỗi sáng (R6).

    Mỗi sáng từ ngày 1, server cho trạng thái đường thật của hôm nay; nó chỉ phụ
    thuộc vết của hai ngày trước. Với từng mô hình ứng viên, dự báo lại hôm nay
    từ đúng những gì đã biết tới tối hôm qua (vết thật của ta, xe đỗ thấy sáng
    hôm qua), rồi cộng điểm −Σ|dự báo − thật| theo thang thông/đông/tắc. Mô hình
    điểm cao nhất dùng cho dự báo của các ngày sau; hoà thì gần PRIOR hơn thắng.
    """

    def __init__(self, forecast: TrafficForecast, models: Optional[Sequence[ForecastModel]] = None,
                 prior: ForecastModel = PRIOR):
        self.config = forecast.config
        self.prior = prior
        self.models: List[ForecastModel] = list(models) if models is not None else default_models()
        self.forecasts = [forecast.with_model(m) for m in self.models]
        self.scores = [0] * len(self.models)
        #: ngày -> {"status", "own", "parked", "steps"}
        self.days: Dict[int, dict] = {}
        self.chosen: ForecastModel = prior
        self.observed = 0
        #: độ chính xác ngoài mẫu [đúng, số ô, ô không thông đúng, ô không thông, tổng lệch]
        #: của mô hình đã chọn từ hôm trước, và của hai mốc so
        self.out_of_sample: Dict[str, List[int]] = {
            k: [0, 0, 0, 0, 0] for k in ("chon", "cu", "nhu_hom_nay")}

    def _order_key(self, i: int):
        m = self.models[i]
        if m.persist or m.legacy:
            far = 10.0
        else:
            far = abs(m.like_us - self.prior.like_us) + abs(m.pop_scale - self.prior.pop_scale)
        return (-self.scores[i], far, i)

    def best(self) -> ForecastModel:
        if self.observed == 0:
            return self.prior
        i = min(range(len(self.models)), key=self._order_key)
        return self.models[i]

    def morning(self, day: int, status: Dict[int, int], parked: Optional[Dict[int, int]] = None,
                steps: int = 0, counts: Optional[Dict[int, int]] = None) -> ForecastModel:
        """Ghi trạng thái đường thật sáng nay, chấm mọi mô hình, trả mô hình dùng từ nay.

        `parked`: lượt dừng của xe đối thủ (parked_stay); `counts`: số xe đối thủ trên
        từng ô đường (counts_from_positions), cho mô hình cũ.
        """
        entry = self.days.setdefault(day, {})
        if "status" in entry:          # đã chấm sáng nay (lập kế hoạch lại cùng ngày)
            return self.chosen
        entry["status"] = dict(status)
        entry["parked"] = dict(parked or {})
        entry["counts"] = dict(counts or {})
        entry["steps"] = steps
        prev = self.days.get(day - 1)
        if day >= 1 and prev is not None and "own" in prev and "status" in prev:
            before = self.days.get(day - 2) if day - 1 >= 1 else None
            yesterday_status = before.get("status") if before is not None else None
            yesterday_own = before.get("own") if before is not None else None
            cells = self.config.road_cells
            chosen = self.chosen
            for i, fc in enumerate(self.forecasts):
                pred = fc.predict(prev["status"], yesterday_status, own_stay_today=prev["own"],
                                  own_stay_yesterday=yesterday_own,
                                  opponents_parked=prev.get("counts"),
                                  parked_stay=prev.get("parked"),
                                  steps_today=prev.get("steps", 0),
                                  floor_today=day - 1 == 1)
                err, right, busy, busy_right = _abs_error(pred, status, cells)
                self.scores[i] -= err
                m = self.models[i]
                keys = []
                if m == chosen:
                    keys.append("chon")
                if m.legacy:
                    keys.append("cu")
                if m.persist:
                    keys.append("nhu_hom_nay")
                for key in keys:
                    acc = self.out_of_sample[key]
                    acc[0] += right
                    acc[1] += len(cells)
                    acc[2] += busy_right
                    acc[3] += busy
                    acc[4] += err
            self.observed += 1
        self.chosen = self.best()
        return self.chosen

    def evening(self, day: int, own_stay: Dict[int, int]) -> None:
        """Ghi vết thật của ta trong ngày (bài server đã áp dụng)."""
        self.days.setdefault(day, {})["own"] = dict(own_stay)

    def report(self) -> dict:
        """Số liệu cho giải đấu và log: điểm từng mô hình, độ chính xác ngoài mẫu."""
        return {
            "chon": self.chosen.label(),
            "so_sang": self.observed,
            "diem": {m.label(): s for m, s in zip(self.models, self.scores)},
            "ngoai_mau": {k: list(v) for k, v in self.out_of_sample.items()},
        }

