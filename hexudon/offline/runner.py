"""Chạy một trận nhiều đội tại chỗ, có giao thông cộng dồn đúng luật."""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .. import rules
from ..model import AgentState, DayState, MatchConfig
from ..simulator import DayResult, road_status_from_history, run_day, wait_plan
from ..traffic import accuracy


@dataclass
class TeamRecord:
    name: str
    kinds: List[int]
    brands: set = field(default_factory=set)
    daily_types: int = 0
    servings: int = 0
    invalid_days: int = 0
    plan_seconds: List[float] = field(default_factory=list)
    #: tổng thời gian phản hồi của bài hợp lệ cuối cùng mỗi ngày (tiêu chí 4 của đề)
    response_seconds: float = 0.0
    agents: List[AgentState] = field(default_factory=list)

    def score(self):
        return (len(self.brands), self.daily_types, self.servings)

    def rank_key(self):
        """Thứ tự xếp hạng của đề: L, rồi D, rồi Q, rồi tổng thời gian phản hồi thấp hơn."""
        return (*self.score(), -self.response_seconds)


@dataclass
class MatchOutcome:
    teams: List[TeamRecord]
    days: List[dict]
    forecast_accuracy: List[dict] = field(default_factory=list)

    def ranking(self) -> List[TeamRecord]:
        return sorted(self.teams, key=lambda t: t.rank_key(), reverse=True)

    def winner(self) -> TeamRecord:
        return self.ranking()[0]


def run_match(config: MatchConfig, bots: Sequence, log=None, collect_forecast=False,
              observer=None) -> MatchOutcome:
    """Chạy trọn trận. Mỗi bot là một đối tượng theo giao diện offline.bots.Bot.

    `observer`, nếu có (web trực quan hoá), được báo khi các đội đã chọn vai trò,
    khi mỗi ngày mở, và sau mỗi (đội, ngày) kèm vết từng bước của simulator.
    """
    log = log or (lambda msg: None)
    records: List[TeamRecord] = []
    for bot in bots:
        kinds = list(bot.select_roles())
        if len(kinds) != config.n_agents:
            kinds = [rules.PATROL] * config.n_agents
        agents = [AgentState(kinds[i], config.agent_starts[i], config.fuel_limits)
                  for i in range(config.n_agents)]
        records.append(TeamRecord(name=getattr(bot, "name", "bot"), kinds=kinds, agents=agents))
    if observer is not None:
        observer.match_ready(records)

    stay_history: List[Dict[int, int]] = []
    day_logs: List[dict] = []
    forecast_rows: List[dict] = []

    for day in range(config.n_days):
        steps = config.steps_of_day(day)
        status = ({c: rules.SMOOTH for c in config.road_cells} if day == 0
                  else road_status_from_history(config, stay_history))
        if observer is not None:
            observer.day_start(day, steps, status, records)
        combined: Dict[int, int] = {}
        day_entry = {"day": day, "steps": steps, "teams": []}
        # mọi đội thấy cùng một ảnh vị trí ĐẦU ngày như server thật (cases 8.11), không
        # phải vị trí cuối ngày của những đội đã được chấm trước trong vòng lặp này
        snapshot = [[AgentState(a.kind, a.pos, a.fuel) for a in rec.agents] for rec in records]

        for idx, (bot, rec) in enumerate(zip(bots, records)):
            others = [
                [AgentState(o.kind, o.pos, o.fuel) for o in team]
                for j, team in enumerate(snapshot) if j != idx
            ]
            state = DayState(day=day, agents=[AgentState(a.kind, a.pos, a.fuel) for a in rec.agents],
                             road_status=dict(status), others=others, ends_at=None)
            t0 = time.monotonic()
            try:
                plan = bot.plan(state)
            except Exception as exc:  # bot lỗi thì coi như không gửi bài
                log(f"ngày {day} đội {rec.name}: bot lỗi {exc!r}")
                plan = None
            elapsed = time.monotonic() - t0
            rec.plan_seconds.append(elapsed)
            # thời gian phản hồi là lúc gửi bài cuối cùng (bot search tự báo), không phải
            # lúc nghĩ xong; ngày không có bài hợp lệ thì không cộng gì, như server thật
            answered = getattr(bot, "last_response_seconds", None)
            response = 0.0

            stocks = [s.stocks for s in config.spots]
            result = None
            error = None
            trace = [] if observer is not None else None
            if plan is not None:
                result = run_day(config, steps, rec.agents, plan, stocks, status, trace=trace)
                if not result.ok:
                    log(f"ngày {day} đội {rec.name}: bài KHÔNG hợp lệ ({result.error})")
                    rec.invalid_days += 1
                    error = result.error
                    result = None
                else:
                    response = answered if answered is not None else elapsed
            rec.response_seconds += response
            if result is None:
                trace = [] if observer is not None else None
                result = run_day(config, steps, rec.agents, wait_plan(config.n_agents, steps),
                                 stocks, status, trace=trace)

            rec.brands |= result.brands
            rec.daily_types += len(result.brands)
            rec.servings += result.servings
            rec.agents = [AgentState(rec.kinds[i], result.end_pos[i], result.end_fuel[i])
                          for i in range(config.n_agents)]
            for cell, n in result.stay.items():
                combined[cell] = combined.get(cell, 0) + n
            if hasattr(bot, "observe"):
                bot.observe(state, result)
            if observer is not None:
                observer.team_day(idx, rec, state, plan, result, elapsed, error, trace)
            day_entry["teams"].append({
                "name": rec.name,
                "types": len(result.brands),
                "servings": result.servings,
                "seconds": round(elapsed, 3),
                "response": round(response, 3),
            })

        stay_history.append(combined)
        if collect_forecast and day + 1 < config.n_days:
            actual_next = road_status_from_history(config, stay_history)
            forecast_rows.append({"day": day + 1, "actual": actual_next})
        day_logs.append(day_entry)
        log(f"ngày {day}: " + " | ".join(
            f"{t['name']} {t['types']} loại / {t['servings']} phần" for t in day_entry["teams"]))

    return MatchOutcome(teams=records, days=day_logs, forecast_accuracy=forecast_rows)
