"""Dự báo giao thông (mức 1, phòng thủ).

Hành lang đường mà mọi đội cùng đi sẽ đông đúc rồi ùn tắc dù không ai cố ý.
Bot dự báo điều đó để khai thác đường ở ngày 0 và không kết thúc ngày ở phía
sai của một hành lang sắp tắc.
"""

from typing import Dict, List, Optional, Sequence

from . import rules
from .model import MatchConfig
from .pathing import DayGraph, DistTable

# Số lượt trung bình MỘT ĐỘI đi qua ô đường phổ biến nhất trong một ngày.
# Không nhân theo số xe: một đội đông xe thì các xe rải ra nhiều hành lang khác nhau.
# Giá trị mặc định là ước lượng ban đầu, phải hiệu chỉnh bằng benchmark (mục 7 của spec).
EXPECTED_PASSES_TOP_CELL = 1.5

# Hệ số né: đường càng xấu thì các đội càng đi vòng, nên số lượt giảm.
AVOIDANCE = (1.0, 0.7, 0.35)  # thông thoáng, đông đúc, ùn tắc


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
                 pop: Optional[Dict[int, float]] = None):
        self.config = config
        self.pop = popularity(config) if pop is None else pop
        # đội nhiều xe hơn đi qua hành lang chính nhiều hơn một chút, không tuyến tính
        scale = 1.0 + 0.1 * max(0, n_patrols - 3)
        self.expected_passes = expected_passes * scale

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
    ) -> Dict[int, int]:
        """Trạng thái đường dự báo cho ngày mai.

        Lưu lượng ngày mai gồm vết của hôm nay và hôm qua (cases 2), nên vết đã biết
        chắc của chính đội ở cả hai ngày đều được cộng (cases 8.13).

        `floor_today=True` khi hôm nay là ngày 2 (day = 1): lưu lượng ngày 3 bằng vết
        ngày 1 cộng vết ngày 2, còn ngày 2 chỉ có vết ngày 1, nên mọi ô ngày mai ít
        nhất cũng tệ như hôm nay (cases 10.3).
        """
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
