"""Hàm mục tiêu: phần tăng trong ngày theo thứ tự từ điển + giá trị cuối ngày."""

from typing import Dict, List, Sequence

from .. import rules
from ..simulator import DayResult
from .solution import DayContext

INF = float("inf")


def _dist_to_brand(ctx: DayContext, cell: int, brand: int) -> float:
    table = ctx.dist_next or ctx.dist_step
    best = INF
    for si in ctx.config.spots_by_brand.get(brand, ()):
        d = table.cost(cell, ctx.spot_cells[si])
        if d < best:
            best = d
    return best


def end_value(ctx: DayContext, result: DayResult) -> float:
    """Giá trị cuối ngày V (mục 9 của spec). Ngày cuối trả 0."""
    if ctx.days_left <= 0:
        return 0.0
    w = ctx.weights
    B = w.B
    cfg = ctx.config
    value = 0.0

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

    # V_spot: kết thúc trên spot thì hôm sau thu miễn phí ở bước 1
    spot_at = cfg.spot_index_at
    for c in patrol_cells:
        if c in spot_at:
            value += w.end_on_spot * B + w.serving

    # V_near: chuỗi có thể với tới trong nửa ngày mai
    if ctx.next_steps > 0:
        reach = 0.5 * ctx.next_steps
        near = 0
        for brand in cfg.brands:
            best = min((_dist_to_brand(ctx, c, brand) for c in patrol_cells), default=INF)
            if best <= reach:
                near += 1
        value += w.near_chain * B * near

    # V_fuel: xe kẹt xăng nhiều ngày; thích kết thúc đầy bình
    table = ctx.dist_next or ctx.dist_step
    for agent in ctx.patrols:
        cell = end_pos[agent]
        fuel = end_fuel[agent]
        cheapest_move = min(
            (rules.fuel_cost(cfg.cells[n]) for n in cfg.neighbors[cell] if n >= 0 and cfg.cells[n] != rules.POND),
            default=1,
        )
        if fuel < cheapest_move:
            car_dist = min((table.cost(cell, cc) for cc in car_cells), default=INF)
            if car_dist == INF or car_dist > 0.5 * max(1, ctx.next_steps):
                value -= w.strand * B * ctx.days_left
        value += w.fuel_bonus * B * (fuel / max(1, cfg.fuel_limits)) / max(1, ctx.n_patrols)

    # V_road: tự gây tắc cho chính mình
    own_road_stay = sum(v for cell, v in result.stay.items() if cfg.cells[cell] == rules.ROAD)
    value -= w.road_park * B * own_road_stay / max(1, cfg.players)
    return value


def score_day(ctx: DayContext, result: DayResult, truncated: int = 0, dropped_meets: int = 0) -> float:
    """Điểm tổng của một kế hoạch ngày."""
    w = ctx.weights
    B = w.B
    daily_brands = result.brands
    new_brands = len(daily_brands - ctx.visited_brands)
    score = B * len(daily_brands)
    score += w.new_brand * B * new_brands
    score += w.serving * result.servings
    score += end_value(ctx, result)
    score -= w.diag_penalty * B * (truncated + dropped_meets)
    return score


def lexicographic_gain(ctx: DayContext, result: DayResult) -> tuple:
    """Phần tăng thật theo thứ tự từ điển, dùng để so sánh và báo cáo."""
    new = len(result.brands - ctx.visited_brands)
    return (new, len(result.brands), result.servings)
