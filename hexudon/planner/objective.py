"""Hàm mục tiêu: phần tăng trong ngày theo thứ tự từ điển + giá trị cuối ngày."""

from typing import Dict, List, Sequence

from .. import rules
from ..simulator import DayResult
from .solution import DayContext
from .value import default_model, default_model_vn, features, learned_parts

INF = float("inf")
#: R1: chuỗi chưa thu mà xe gần nhất cuối ngày đã xa hơn tỉ lệ này của số bước còn lại
#: cả trận thì bị phạt w.deadline × new_brand_value (một bước trước khi thành không thể)
DEADLINE_MARGIN = 0.5


def _dist_to_brand(ctx: DayContext, cell: int, brand: int) -> float:
    table = ctx.dist_next or ctx.dist_step
    best = INF
    for si in ctx.config.spots_by_brand.get(brand, ()):
        d = table.cost(cell, ctx.spot_cells[si])
        if d < best:
            best = d
    return best


def end_value(ctx: DayContext, result: DayResult) -> float:
    """Giá trị cuối ngày V (mục 9 của spec). Ngày cuối trả 0.

    B4: value_weight > 0 và có mô hình (planner/value.py) thì ghép hàm giá trị học
    được, theo `mode` của mô hình: cộng vào end_value cũ, hoặc thay hạng mục mềm.
    """
    if ctx.days_left <= 0:
        return 0.0
    w = ctx.weights
    hard, soft = None, None
    x = None
    # V-VN (18/11/2026): ở ngày D an toàn (soft_unit > 0), cộng value_q_weight × V_vn theo SỐ PHẦN;
    # value_q_replace > 0 thì bỏ V1 (học D + Q/B trên bản đồ kiểu Nhật) ở những ngày đó
    vn = default_model_vn() if (w.value_q_weight > 0 and w.soft_unit > 0) else None
    if w.value_weight > 0 and not (vn is not None and w.value_q_replace > 0):
        model = default_model()
        if model is not None:
            if model.residual:
                hard, soft = _hand_parts(ctx, result)
                x = features(ctx, result)
                soft += w.value_weight * w.soft * model.predict(x)
            else:
                hard, soft = learned_parts(ctx, result, w, model)
    if hard is None:
        hard, soft = _hand_parts(ctx, result)
    if vn is not None:
        if x is None:
            x = features(ctx, result)
        unit = w.serving if vn.unit == "phan" else w.soft
        soft += w.value_q_weight * unit * vn.predict(x)
    if w.soft_cap > 0:
        # D hôm nay (B mỗi chuỗi) luôn thắng: hai nghiệm chênh nhau về phần mềm tối đa 2·cap·B
        cap = w.soft_cap * w.B
        soft = max(-cap, min(cap, soft))
    return hard + soft


def _hand_end_value(ctx: DayContext, result: DayResult) -> float:
    """end_value đặt tay (trước B4)."""
    hard, soft = _hand_parts(ctx, result)
    return hard + soft


def _hand_parts(ctx: DayContext, result: DayResult):
    """end_value đặt tay tách hai phần: (cứng, mềm).

    Cứng giữ L và D tương lai, luôn theo B: chuỗi không kịp ghé, độ gấp, hạn chót, xe kẹt
    xăng. Mềm: kết thúc trên spot, chuỗi gần, xăng còn lại, tự gây tắc; theo Weights.soft.
    """
    w = ctx.weights
    B = w.B
    #: đơn vị của hạng mục mềm (Weights.soft_unit); hạng mục giữ L, D tương lai vẫn theo B
    U = w.soft
    cfg = ctx.config
    value = 0.0
    soft = 0.0

    end_pos = result.end_pos
    end_fuel = result.end_fuel
    patrol_cells = [end_pos[i] for i in ctx.patrols]
    car_cells = [end_pos[i] for i in ctx.cars]

    steps_rest = sum(cfg.day_steps[ctx.day + 1:])
    unvisited = [b for b in cfg.brands if b not in ctx.visited_brands and b not in result.brands]

    # V_cover + V_urgency
    for brand in unvisited:
        best = min((_dist_to_brand(ctx, c, brand) for c in patrol_cells), default=INF)
        if best == INF or best > steps_rest:
            value -= w.cover_penalty * B
        else:
            value -= w.urgency * B * min(1.0, best / max(1, steps_rest))
            if w.deadline > 0 and best > DEADLINE_MARGIN * steps_rest:
                value -= w.deadline * ctx.new_brand_value

    # V_spot: kết thúc trên spot thì hôm sau thu miễn phí ở bước 1
    spot_at = cfg.spot_index_at
    for c in patrol_cells:
        if c in spot_at:
            soft += w.end_on_spot * U + w.serving

    # V_near: chuỗi có thể với tới trong nửa ngày mai
    if ctx.next_steps > 0:
        reach = 0.5 * ctx.next_steps
        near = 0
        for brand in cfg.brands:
            best = min((_dist_to_brand(ctx, c, brand) for c in patrol_cells), default=INF)
            if best <= reach:
                near += 1
        soft += w.near_chain * U * near

    # V_fuel: xe kẹt xăng nhiều ngày; thích kết thúc đầy bình. Xăng tính theo ô
    # ĐANG ĐỨNG: còn 1 xăng trên núi hoặc đường là kẹt, trên đồng bằng thì chưa (cases 7.11)
    table = ctx.dist_next or ctx.dist_step
    for agent in ctx.patrols:
        cell = end_pos[agent]
        fuel = end_fuel[agent]
        if fuel < rules.fuel_cost(cfg.cells[cell]):
            car_dist = min((table.cost(cell, cc) for cc in car_cells), default=INF)
            if car_dist == INF or car_dist > 0.5 * max(1, ctx.next_steps):
                value -= w.strand * B * ctx.days_left
        soft += w.fuel_bonus * U * (fuel / max(1, cfg.fuel_limits)) / max(1, ctx.n_patrols)

    # V_road: tự gây tắc cho chính mình
    own_road_stay = sum(v for cell, v in result.stay.items() if cfg.cells[cell] == rules.ROAD)
    soft -= w.road_park * U * own_road_stay / max(1, cfg.players)
    return value, soft


def score_day(ctx: DayContext, result: DayResult, truncated: int = 0, dropped_meets: int = 0) -> float:
    """Điểm tổng của một kế hoạch ngày: L trước, rồi D, rồi Q (cases 9.7)."""
    w = ctx.weights
    B = w.B
    daily_brands = result.brands
    new_brands = len(daily_brands - ctx.visited_brands)
    score = B * len(daily_brands)
    score += ctx.new_brand_value * new_brands
    score += w.serving * result.servings
    score += end_value(ctx, result)
    score -= w.diag_penalty * B * (truncated + dropped_meets)
    return score


def lexicographic_gain(ctx: DayContext, result: DayResult) -> tuple:
    """Phần tăng thật theo thứ tự từ điển, dùng để so sánh và báo cáo."""
    new = len(result.brands - ctx.visited_brands)
    return (new, len(result.brands), result.servings)
