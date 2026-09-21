"""Đối chiếu replay của server với simulator của mình, từng bước.

Server gửi cho đội gọi API một frame ở mỗi bước 0…N (đã quan sát 2026-09-21),
nên so được tới đúng bước đầu tiên lệch. Mỗi ngày bắt đầu từ frame bước 0 CỦA
SERVER, để lỗi của một ngày không lan sang ngày sau; tồn kho lấy từ config mỗi
ngày, đúng như planner và simulator đang làm. Lệch ở đây là lệch luật giữa
simulator và server, không phải lỗi của planner.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .. import rules
from ..model import AgentState, MatchConfig
from ..simulator import run_day, wait_plan

KIND_OF = {"patrol": rules.PATROL, "refuel": rules.REFUEL}


@dataclass
class Mismatch:
    day: int
    step: Optional[int]
    agent: Optional[int]
    what: str
    ours: object
    server: object

    def describe(self) -> str:
        where = f"Ngày {self.day + 1}"
        if self.step is not None:
            where += f" bước {self.step}"
        if self.agent is not None:
            where += f" xe {self.agent}"
        return f"{where}: {self.what} ta={self.ours} server={self.server}"


@dataclass
class CheckReport:
    days_checked: int = 0
    mismatches: List[Mismatch] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def days_with_mismatch(self) -> List[int]:
        return sorted({m.day for m in self.mismatches})

    def summary(self) -> str:
        if self.ok:
            return f"đối chiếu {self.days_checked} ngày với replay của server: khớp hoàn toàn"
        head = "; ".join(m.describe() for m in self.mismatches[:5])
        more = f" (và {len(self.mismatches) - 5} chỗ khác)" if len(self.mismatches) > 5 else ""
        return (f"LỆCH LUẬT ở {len(self.days_with_mismatch())}/{self.days_checked} ngày: "
                f"{head}{more}")

    def to_json(self) -> dict:
        return {"days_checked": self.days_checked, "ok": self.ok,
                "days_with_mismatch": self.days_with_mismatch(),
                "mismatches": [vars(m) for m in self.mismatches], "notes": list(self.notes)}


def _team_entry(day: dict, team_id) -> Optional[dict]:
    for team in day.get("teams") or []:
        if str(team.get("team_id")) == str(team_id):
            return team
    return None


def _agents_from(frame: dict, kinds: Sequence[int]) -> List[AgentState]:
    agents = []
    for i, a in enumerate(frame.get("agents") or []):
        kind = kinds[i] if i < len(kinds) else KIND_OF.get(a.get("type"), rules.PATROL)
        agents.append(AgentState(kind, int(a["cell"]), int(a.get("fuel") or 0)))
    return agents


def _compare_steps(report: CheckReport, day: int, trace, frames) -> None:
    """Chỉ báo lệch đầu tiên của mỗi (xe, trường): một bước lệch kéo theo mọi bước sau."""
    ours = {t: (pos, fuel) for t, pos, fuel in trace}
    seen = set()
    for frame in frames:
        t = int(frame.get("step", -1))
        if t not in ours:
            continue
        pos, fuel = ours[t]
        for i, agent in enumerate(frame.get("agents") or []):
            if i >= len(pos):
                break
            for what, mine, theirs in (("cell", pos[i], agent.get("cell")),
                                       ("fuel", fuel[i], agent.get("fuel"))):
                if theirs is None or (i, what) in seen or int(theirs) == mine:
                    continue
                seen.add((i, what))
                report.mismatches.append(Mismatch(day, t, i, what, mine, int(theirs)))


def check(config: MatchConfig, team_id, replay: Optional[dict],
          accepted_plans: Dict[int, List[List[int]]]) -> CheckReport:
    """Chạy lại từng ngày bằng simulator của ta với đúng bài server đã nhận, rồi so."""
    report = CheckReport()
    days = sorted((replay or {}).get("days") or [], key=lambda d: int(d.get("day", 0)))
    previous_end: Optional[List[AgentState]] = None
    for day in days:
        d = int(day.get("day", 0))
        team = _team_entry(day, team_id)
        frames = sorted((team or {}).get("frames") or [], key=lambda f: int(f.get("step", 0)))
        if not frames:
            report.notes.append(f"Ngày {d + 1}: replay không có frame của đội {team_id}")
            previous_end = None
            continue
        kinds = [int(k) for k in team.get("kinds") or []] or [rules.PATROL] * config.n_agents
        steps = int(day.get("steps") or config.steps_of_day(d))
        if int(frames[0].get("step", -1)) == 0:
            start = _agents_from(frames[0], kinds)
        elif previous_end is not None:
            start = previous_end
        elif d == 0:
            start = [AgentState(k, p, config.fuel_limits) for k, p in zip(kinds, config.agent_starts)]
        else:
            report.notes.append(f"Ngày {d + 1}: không biết trạng thái đầu ngày, bỏ qua")
            previous_end = _agents_from(frames[-1], kinds)
            continue
        if len(frames) not in (1, steps + 1):
            report.notes.append(f"Ngày {d + 1}: server gửi {len(frames)} frame cho {steps} bước")
        road = {int(c): int(s) for c, s in (day.get("road_condition") or {}).items()}
        plan = accepted_plans.get(d) or wait_plan(config.n_agents, steps)
        trace: list = []
        result = run_day(config, steps, start, plan, [s.stocks for s in config.spots], road, trace=trace)
        report.days_checked += 1
        previous_end = _agents_from(frames[-1], kinds)
        if not result.ok:
            report.mismatches.append(Mismatch(d, result.error.step, result.error.agent, "plan",
                                              str(result.error), "server đã nhận bài này"))
            continue
        _compare_steps(report, d, trace, frames)
        final = frames[-1]
        if int(final.get("step", -1)) == steps:
            for what, ours in (("servings", result.servings), ("types", len(result.brands))):
                theirs = final.get(what)
                if theirs is not None and int(theirs) != ours:
                    report.mismatches.append(Mismatch(d, steps, None, what, ours, int(theirs)))
    return report
