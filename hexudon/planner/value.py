"""Hàm giá trị học được V(trạng thái cuối ngày) (B4, 17/11/2026).

E2 đo được `end_value` đặt tay tương quan 0,00 với phần thu thật các ngày sau; E3
cho thấy một mô hình cây học từ chưa tới 1.000 dòng đã chọn kế hoạch tốt hơn. Ở đây:

  - `features(ctx, result)`: đặc trưng trạng thái cuối ngày (khoảng cách tới chuỗi,
    xăng, xe tiếp, độ phủ spot...), đo trên bảng đường dự báo ngày mai như end_value;
  - `ValueModel`: mô hình xuất ra JSON (thi_nghiem_ai/hoc_v.py), dự đoán phần thu các
    ngày sau so với các nghiệm của CÙNG trạng thái, đơn vị B (1,0 ≈ một chuỗi theo
    ngày). Hai loại, chạy bằng Python thuần:
      "tuyen_tinh": V = Σ hệ số × đặc trưng, học trên đặc trưng đã trừ trung bình của
                    trạng thái (phần chung của trạng thái tự triệt tiêu khi so nghiệm);
      "cay":        cây tăng cường (GradientBoostingRegressor);
  - hai cách ghép (`mode` của mô hình), khi Weights.value_weight > 0:
      "phan_du":  end_value cũ + value_weight × B × V (V học phần end_value còn thiếu);
      "thay":     luật cứng của end_value (cover_penalty, deadline, strand) + value_weight
                  × B × V, bỏ các hạng mục mềm (learned_end_value).
"""

import json
import os
from typing import Dict, List, Optional, Sequence

from .. import rules
from .solution import DayContext, Weights

INF = float("inf")
#: mô hình đi kèm package; không có file thì value_weight > 0 rơi về end_value cũ
MODEL_PATH = os.path.join(os.path.dirname(__file__), "value_model.json")
#: biến môi trường chọn file mô hình khác (thử mô hình mới mà không ghi đè file mặc định)
ENV_MODEL = "HEXUDON_VALUE_MODEL"
#: như objective.DEADLINE_MARGIN (không import để tránh vòng import)
DEADLINE_MARGIN = 0.5
#: trọng số cố định của đặc trưng "end_value mềm cũ": không đổi theo núm của trận, để
#: đặc trưng lúc thi giống hệt lúc học (pha Q và các biến thể nhân trọng số mềm)
_SOFT = {"urgency": 0.6, "end_on_spot": 0.5, "near_chain": 0.4, "fuel_bonus": 1.0, "road_park": 0.05}

FEATURE_NAMES = [
    "ngay_con_lai", "buoc_con_lai_chuan", "buoc_mai_chuan", "so_xe_tuan_tra", "so_xe_tiep",
    "binh_chuan", "binh_so_voi_ngay_mai",
    "ti_le_chuoi_chua_thu", "chua_thu_toi_duoc_nua_ngay", "chua_thu_toi_duoc_1_ngay", "chua_thu_khong_kip",
    "chua_thu_kc_tb", "chua_thu_kc_xa_nhat", "chuoi_toi_duoc_1_4_ngay", "chuoi_toi_duoc_nua_ngay",
    "chuoi_toi_duoc_1_ngay", "phu_trung_nua_ngay", "spot_toi_duoc_nua_ngay",
    "xang_tb", "xang_min", "xang_so_voi_can", "xe_ket_xang", "xe_thieu_xang_nua_ngay",
    "kc_toi_xe_tiep_tb", "kc_xe_can_xang_toi_xe_tiep",
    "dung_tren_spot", "dung_tren_duong", "vet_tren_duong", "kc_giua_xe_tuan_tra", "end_value_mem_B",
]


class _EndState:
    """Các đại lượng của trạng thái cuối ngày, tính một lần cho cả đặc trưng lẫn luật cứng."""

    __slots__ = ("patrol_cells", "car_cells", "fuels", "dsp", "dbrand", "dmin", "steps_rest", "ns", "table")

    def __init__(self, ctx: DayContext, result):
        cfg = ctx.config
        table = ctx.dist_next or ctx.dist_step
        self.table = table
        self.ns = max(1, ctx.next_steps)
        self.steps_rest = sum(cfg.day_steps[ctx.day + 1:])
        end_pos, end_fuel = result.end_pos, result.end_fuel
        self.patrol_cells = [end_pos[i] for i in ctx.patrols]
        self.car_cells = [end_pos[i] for i in ctx.cars]
        self.fuels = [end_fuel[i] for i in ctx.patrols]
        spot_cells = ctx.spot_cells
        dsp = []
        for c in self.patrol_cells:
            row = table.dist.get(c)
            dsp.append([INF] * len(spot_cells) if row is None else [row[sc] for sc in spot_cells])
        self.dsp = dsp
        # dbrand[p][b]: khoảng cách từ xe tuần tra p tới spot gần nhất của chuỗi b
        dbrand = []
        for row in dsp:
            d = {}
            for b, idxs in cfg.spots_by_brand.items():
                best = INF
                for si in idxs:
                    v = row[si]
                    if v < best:
                        best = v
                d[b] = best
            dbrand.append(d)
        self.dbrand = dbrand
        self.dmin = {b: min((d[b] for d in dbrand), default=INF) for b in cfg.brands}


def _clip(x: float, hi: float = 3.0) -> float:
    return hi if x == INF else min(hi, x)


def features(ctx: DayContext, result, state: Optional[_EndState] = None) -> List[float]:
    """Đặc trưng trạng thái cuối ngày, theo đúng thứ tự FEATURE_NAMES."""
    st = state or _EndState(ctx, result)
    cfg = ctx.config
    W = cfg.width + cfg.height
    ns = st.ns
    steps_rest = st.steps_rest
    nb = max(1, len(cfg.brands))
    F = max(1, cfg.fuel_limits)
    patrols = st.patrol_cells
    P = max(1, len(patrols))
    fuels = st.fuels
    dmin = st.dmin
    visited = ctx.visited_brands | result.brands
    unv = [b for b in cfg.brands if b not in visited]
    table = st.table

    half = 0.5 * ns
    per_patrol_reach = sum(1 for d in st.dbrand for b in cfg.brands if d[b] <= half)
    n_spots = max(1, len(ctx.spot_cells))
    spots_reach = 0
    for si in range(len(ctx.spot_cells)):
        if min((row[si] for row in st.dsp), default=INF) <= half:
            spots_reach += 1

    car_d = []
    for c in patrols:
        d = min((table.cost(c, cc) for cc in st.car_cells), default=INF)
        car_d.append(_clip(d / ns))
    stranded = sum(1 for c, f in zip(patrols, fuels) if f < rules.fuel_cost(cfg.cells[c]))
    short = sum(1 for f in fuels if f < half)
    if fuels:
        needy = min(range(len(fuels)), key=lambda i: fuels[i])
        needy_car = car_d[needy]
    else:
        needy_car = 3.0
    road = rules.ROAD
    own_road_stay = sum(v for cell, v in result.stay.items() if cfg.cells[cell] == road)
    on_spot = sum(1 for c in patrols if c in cfg.spot_index_at)
    on_road = sum(1 for c in patrols if cfg.cells[c] == road)
    spread, pairs = 0.0, 0
    for i in range(len(patrols)):
        row = table.dist.get(patrols[i])
        for j in range(i + 1, len(patrols)):
            spread += _clip((INF if row is None else row[patrols[j]]) / ns)
            pairs += 1
    need = max(1.0, len(patrols) * steps_rest / 2.0)

    # end_value mềm cũ với trọng số cố định, đơn vị B
    soft = 0.0
    for b in unv:
        best = dmin[b]
        if best != INF and best <= steps_rest:
            soft -= _SOFT["urgency"] * min(1.0, best / max(1, steps_rest))
    soft += _SOFT["end_on_spot"] * on_spot
    if ctx.next_steps > 0:
        soft += _SOFT["near_chain"] * sum(1 for b in cfg.brands if dmin[b] <= half)
    soft += _SOFT["fuel_bonus"] * sum(f / F for f in fuels) / P
    soft -= _SOFT["road_park"] * own_road_stay / max(1, cfg.players)

    return [
        float(ctx.days_left),
        steps_rest / W,
        ctx.next_steps / W,
        float(len(patrols)),
        float(len(st.car_cells)),
        F / W,
        min(3.0, F / ns),
        len(unv) / nb,
        sum(1 for b in unv if dmin[b] <= half) / nb,
        sum(1 for b in unv if dmin[b] <= ns) / nb,
        sum(1 for b in unv if dmin[b] == INF or dmin[b] > steps_rest) / nb,
        (sum(_clip(dmin[b] / ns) for b in unv) / len(unv)) if unv else 0.0,
        max((_clip(dmin[b] / max(1, steps_rest), 2.0) for b in unv), default=0.0),
        sum(1 for b in cfg.brands if dmin[b] <= 0.25 * ns) / nb,
        sum(1 for b in cfg.brands if dmin[b] <= half) / nb,
        sum(1 for b in cfg.brands if dmin[b] <= ns) / nb,
        per_patrol_reach / nb,
        spots_reach / n_spots,
        (sum(fuels) / len(fuels) / F) if fuels else 0.0,
        (min(fuels) / F) if fuels else 0.0,
        min(3.0, sum(fuels) / need),
        float(stranded),
        short / P,
        sum(car_d) / len(car_d) if car_d else 3.0,
        needy_car,
        on_spot / P,
        on_road / P,
        own_road_stay / max(1, cfg.players),
        spread / pairs if pairs else 0.0,
        soft,
    ]


def hard_terms(ctx: DayContext, result, w: Weights, state: Optional[_EndState] = None) -> float:
    """Phần luật cứng của end_value: chuỗi không kịp ghé, quá hạn chót, xe kẹt xăng."""
    st = state or _EndState(ctx, result)
    cfg = ctx.config
    B = w.B
    value = 0.0
    steps_rest = st.steps_rest
    for b in cfg.brands:
        if b in ctx.visited_brands or b in result.brands:
            continue
        best = st.dmin[b]
        if best == INF or best > steps_rest:
            value -= w.cover_penalty * B
        elif w.deadline > 0 and best > DEADLINE_MARGIN * steps_rest:
            value -= w.deadline * ctx.new_brand_value
    table = st.table
    for c, fuel in zip(st.patrol_cells, st.fuels):
        if fuel < rules.fuel_cost(cfg.cells[c]):
            car_dist = min((table.cost(c, cc) for cc in st.car_cells), default=INF)
            if car_dist == INF or car_dist > 0.5 * st.ns:
                value -= w.strand * B * ctx.days_left
    return value


class ValueModel:
    """Mô hình giá trị chạy thuần Python, đọc từ JSON. Ba loại (`kind`):

      "tuyen_tinh": V = init + Σ hệ số × đặc trưng;
      "cay":        cây tăng cường (scikit-learn GradientBoostingRegressor) ở dạng mảng;
      "mlp":        mạng nơ-ron truyền thẳng (scikit-learn MLPRegressor, ReLU): chuẩn hoá đầu
                    vào (x − mean) / scale, các lớp ẩn ReLU, lớp ra tuyến tính, rồi
                    y_mean + y_scale × đầu ra (18/11/2026, hàm giá trị V-VN).
    """

    def __init__(self, data: dict):
        names = list(data["features"])
        if names != FEATURE_NAMES:
            raise ValueError("đặc trưng của mô hình không khớp FEATURE_NAMES")
        self.kind = data.get("kind", "cay")
        self.residual = data.get("mode", "thay") == "phan_du"
        self.layers = None
        if self.kind == "tuyen_tinh":
            self.coef = [float(c) for c in data["coef"]]
            if len(self.coef) != len(names):
                raise ValueError("số hệ số không khớp số đặc trưng")
            self.init = float(data.get("init", 0.0))
            self.trees = []
        elif self.kind == "mlp":
            self.coef = None
            self.trees = []
            self.init = 0.0
            self.mean = [float(v) for v in data["mean"]]
            self.scale = [float(v) if v else 1.0 for v in data["scale"]]
            if len(self.mean) != len(names) or len(self.scale) != len(names):
                raise ValueError("chuẩn hoá của mô hình không khớp số đặc trưng")
            #: [(W theo hàng là nơ-ron vào: W[i][j], b[j])]
            self.layers = [([[float(v) for v in row] for row in layer["W"]], [float(v) for v in layer["b"]])
                           for layer in data["layers"]]
            if len(self.layers[0][0]) != len(names):
                raise ValueError("lớp vào của mạng không khớp số đặc trưng")
            self.y_mean = float(data.get("y_mean", 0.0))
            self.y_scale = float(data.get("y_scale", 1.0))
        else:
            self.coef = None
            self.init = float(data["init"])
            self.rate = float(data["learning_rate"])
            self.trees = [(t["f"], t["t"], t["l"], t["r"], [self.rate * v for v in t["v"]])
                          for t in data["trees"]]
        self.meta = {k: v for k, v in data.items() if k not in ("trees", "coef", "layers")}
        #: "phan": đầu ra tính theo số phần (V-VN), nhân Weights.serving; mặc định theo đơn vị mềm
        self.unit = data.get("don_vi", "")

    @staticmethod
    def load(path: str = MODEL_PATH) -> "ValueModel":
        with open(path, encoding="utf-8") as fh:
            return ValueModel(json.load(fh))

    def predict(self, x: Sequence[float]) -> float:
        if self.layers is not None:
            h = [(v - m) / s for v, m, s in zip(x, self.mean, self.scale)]
            last = len(self.layers) - 1
            for k, (W, b) in enumerate(self.layers):
                out = list(b)
                for i, hi in enumerate(h):
                    if hi:
                        row = W[i]
                        for j in range(len(out)):
                            out[j] += hi * row[j]
                h = out if k == last else [v if v > 0.0 else 0.0 for v in out]
            return self.y_mean + self.y_scale * h[0]
        if self.coef is not None:
            return self.init + sum(c * v for c, v in zip(self.coef, x))
        total = self.init
        for f, t, left, right, v in self.trees:
            node = 0
            while left[node] >= 0:
                node = left[node] if x[f[node]] <= t[node] else right[node]
            total += v[node]
        return total


_CACHE: Dict[str, Optional[ValueModel]] = {}


def default_model() -> Optional[ValueModel]:
    """Mô hình đang dùng: file trong HEXUDON_VALUE_MODEL nếu có, không thì file đi kèm
    package. Nạp một lần mỗi đường dẫn; None nếu không đọc được."""
    path = os.environ.get(ENV_MODEL) or MODEL_PATH
    if path not in _CACHE:
        try:
            _CACHE[path] = ValueModel.load(path)
        except (OSError, ValueError, KeyError):
            _CACHE[path] = None
    return _CACHE[path]


#: V-VN (18/11/2026): mô hình học SỐ PHẦN tương lai trên bản đồ kiểu server VN, dùng ở ngày
#: D an toàn (soft_unit > 0) với trọng số Weights.value_q_weight
MODEL_VN_PATH = os.path.join(os.path.dirname(__file__), "value_model_vn.json")
ENV_MODEL_VN = "HEXUDON_VALUE_MODEL_VN"


def default_model_vn() -> Optional[ValueModel]:
    """Mô hình V-VN: file trong HEXUDON_VALUE_MODEL_VN nếu có, không thì value_model_vn.json."""
    path = os.environ.get(ENV_MODEL_VN) or MODEL_VN_PATH
    key = "vn:" + path
    if key not in _CACHE:
        try:
            _CACHE[key] = ValueModel.load(path)
        except (OSError, ValueError, KeyError):
            _CACHE[key] = None
    return _CACHE[key]


def learned_parts(ctx: DayContext, result, w: Weights, model: ValueModel):
    """(phần cứng, phần mềm) của learned_end_value, để objective kẹp riêng phần mềm."""
    st = _EndState(ctx, result)
    return hard_terms(ctx, result, w, st), w.value_weight * w.soft * model.predict(features(ctx, result, st))


def learned_end_value(ctx: DayContext, result, w: Weights, model: ValueModel) -> float:
    """end_value khi value_weight > 0: luật cứng giữ nguyên, phần mềm là value_weight × đơn vị mềm × V
    (đơn vị mềm = Weights.soft: B, hoặc soft_unit khi > 0)."""
    st = _EndState(ctx, result)
    return hard_terms(ctx, result, w, st) + w.value_weight * w.soft * model.predict(features(ctx, result, st))
