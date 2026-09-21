"""Các endpoint của server thi hexudon.hairbui76.id.vn, mỗi endpoint một hàm.

Không có logic game ở đây: chỉ dựng đường dẫn, header, body và đọc /state.
Hai service dùng hai kiểu header (đo ngày 2026-09-21): game service cần
"Bearer <jwt>", manager service cần "<jwt>" trần.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from .net import JsonHttp, Response

DEFAULT_URL = "https://hexudon.hairbui76.id.vn"
LIST_LIMIT = 1000

SELECTING = "selecting_agents"
IN_PROGRESS = "in_progress"
FINISHED = "finished"


def error_detail(payload) -> str:
    """Thông điệp lỗi kiểu FastAPI: {"detail": "..."} hoặc danh sách {loc, msg}."""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            return "; ".join(
                f"{'.'.join(str(p) for p in item.get('loc', []))}: {item.get('msg', '')}"
                for item in detail if isinstance(item, dict))
        for key in ("message", "reason", "error", "raw"):
            if payload.get(key):
                return str(payload[key])
    return "" if payload is None else str(payload)[:300]


def _float_or_none(value) -> Optional[float]:
    return None if value is None else float(value)


@dataclass
class GameStatus:
    """Bản đọc /api/game/state. Các hạn là số giây CÒN LẠI, không phải mốc giờ."""
    status: str
    day: int
    steps_today: Optional[int] = None
    day_deadline_in: Optional[float] = None
    selection_deadline_in: Optional[float] = None
    selection_opens_in: Optional[float] = None
    selection_limit: Optional[float] = None
    team: dict = field(default_factory=dict)
    read_at: float = 0.0          # time.time() lúc đọc; kênh truyền tự điền

    @staticmethod
    def from_json(obj: dict, team_id: Optional[int] = None) -> "GameStatus":
        teams = obj.get("teams") if isinstance(obj.get("teams"), dict) else {}
        team = teams.get(str(team_id), {}) if team_id is not None else {}
        day = obj.get("day")
        steps = obj.get("steps_today")
        return GameStatus(
            status=str(obj.get("status") or ""),
            day=int(day) if day is not None else -1,
            steps_today=int(steps) if steps is not None else None,
            day_deadline_in=_float_or_none(obj.get("day_deadline_in")),
            selection_deadline_in=_float_or_none(obj.get("selection_deadline_in")),
            selection_opens_in=_float_or_none(obj.get("selection_opens_in")),
            selection_limit=_float_or_none(obj.get("agent_selection_time_limit")),
            team=team if isinstance(team, dict) else {},
        )


class WebApi:
    """Mỗi endpoint một phương thức trả Response; token đọc lại ở mỗi lời gọi."""

    def __init__(self, http: JsonHttp, token_provider: Callable[[], str]):
        self.http = http
        self.token_provider = token_provider

    def _game(self, method: str, path: str, params: Optional[dict] = None, body=None,
              deadline: Optional[float] = None) -> Response:
        return self.http.call(method, "/api/game" + path, params=params, body=body,
                              headers={"Authorization": "Bearer " + self.token_provider()},
                              deadline=deadline)

    def _manager(self, path: str) -> Response:
        return self.http.call("GET", "/manager/api" + path, params={"limit": LIST_LIMIT},
                              headers={"Authorization": self.token_provider()})

    # ---- đọc ----

    def config(self, game_id: str) -> Response:
        return self._game("GET", "/config", {"game_id": game_id})

    def state(self, game_id: str) -> Response:
        return self._game("GET", "/state", {"game_id": game_id})

    def day(self, game_id: str) -> Response:
        return self._game("GET", "/day", {"game_id": game_id})

    def replay(self, game_id: str) -> Response:
        return self._game("GET", "/replay", {"game_id": game_id})

    def result(self, game_id: str) -> Response:
        return self._game("GET", "/result", {"game_id": game_id})

    def submitted_actions(self, game_id: str) -> Response:
        return self._game("GET", "/actions", {"game_id": game_id})

    def practice_score(self, game_id: str) -> Response:
        return self._game("GET", "/practice/score", {"game_id": game_id})

    def practice_peer(self, game_id: str) -> Response:
        return self._game("GET", "/practice/peer", {"game_id": game_id})

    # ---- ghi ----

    def agent_types(self, game_id: str, types: Sequence[int],
                    deadline: Optional[float] = None) -> Response:
        return self._game("POST", "/agent-types",
                          body={"game_id": game_id, "types": [int(t) for t in types]},
                          deadline=deadline)

    def actions(self, game_id: str, day: int, plan: List[List[int]],
                deadline: Optional[float] = None) -> Response:
        return self._game("POST", "/actions",
                          body={"game_id": game_id, "day": int(day), "actions": plan},
                          deadline=deadline)

    def practice_actions(self, game_id: str, day: int, plan: List[List[int]],
                         deadline: Optional[float] = None) -> Response:
        return self._game("POST", "/practice/actions",
                          body={"game_id": game_id, "day": int(day), "actions": plan},
                          deadline=deadline)

    def practice_reset(self, game_id: str) -> Response:
        return self._game("POST", "/practice/reset", body={"game_id": game_id})

    # ---- manager service ----

    def questions(self) -> Response:
        return self._manager("/question")

    def matches(self) -> Response:
        return self._manager("/match")
