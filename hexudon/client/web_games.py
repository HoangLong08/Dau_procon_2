"""Tra trận và câu trên server thi theo ID hoặc tên — không bao giờ đoán gần đúng.

Ngày thi mà bot tự chọn một trận "gần giống" thì còn tệ hơn dừng lại: sai tên
hay trùng tên đều dừng và in danh sách để người dùng chọn lại. Gọi thẳng bằng
ID câu (--game) thì không cần manager API, để vẫn chơi được khi service đó lỗi.
"""

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .web_api import FINISHED, IN_PROGRESS, SELECTING, GameStatus, error_detail

CONTEST = "contest"
PRACTICE = "practice"
COMPETITIVE_PRACTICE = "competitive_practice"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ResolveError(Exception):
    """Không xác định được câu cần chơi; thông điệp in thẳng cho người dùng."""


@dataclass(frozen=True)
class GameRef:
    game_id: str
    question: Optional[str] = None
    match_id: Optional[int] = None
    match_name: Optional[str] = None
    mode: str = CONTEST
    auto_reset_minutes: int = 0

    def label(self) -> str:
        if self.match_id is not None:
            return f"trận {self.match_id} «{self.match_name}» câu {self.question}"
        return f"câu {self.question or self.game_id}"


def mode_of(match: dict) -> str:
    if not match.get("is_practice"):
        return CONTEST
    return COMPETITIVE_PRACTICE if match.get("no_reset") else PRACTICE


def fetch_questions(api, log: Optional[Callable[[str], None]] = None) -> List[GameRef]:
    """Mọi câu đội này thấy, lấy qua manager API."""
    resp = api.questions()
    if resp.status != 200 or not isinstance(resp.payload, dict):
        raise ResolveError(f"không lấy được danh sách câu (HTTP {resp.status}: "
                           f"{error_detail(resp.payload)}); có thể chơi thẳng bằng --game <ID câu>")
    rows = resp.payload.get("data") or []
    count = resp.payload.get("count")
    if log and isinstance(count, int) and count > len(rows):
        log(f"server báo {count} câu nhưng chỉ trả {len(rows)}; danh sách có thể thiếu")
    refs = []
    for row in rows:
        match = row.get("match") or {}
        refs.append(GameRef(game_id=str(row["id"]), question=row.get("name"),
                            match_id=row.get("match_id"), match_name=match.get("name"),
                            mode=mode_of(match),
                            auto_reset_minutes=int(row.get("auto_reset_minutes") or 0)))
    refs.sort(key=lambda r: (r.match_id or 0, r.question or ""))
    return refs


def format_table(refs: List[GameRef], statuses: Optional[Dict[str, str]] = None) -> str:
    """Bảng cho người đọc: ID trận, tên trận, loại, tên câu, ID câu, trạng thái."""
    lines = []
    for r in refs:
        status = (statuses or {}).get(r.game_id, "")
        again = f" (tự chạy lại mỗi {r.auto_reset_minutes} phút)" if r.auto_reset_minutes else ""
        lines.append(f"  trận {r.match_id!s:>4}  {r.match_name or '':<20} {r.mode:<21} "
                     f"{r.question or '':<5} {r.game_id}  {status}{again}".rstrip())
    return "\n".join(lines)


def describe_status(st: GameStatus) -> str:
    if st.status == IN_PROGRESS:
        left = f", còn {st.day_deadline_in:.0f}s" if st.day_deadline_in is not None else ""
        return f"đang chạy Ngày {st.day + 1}{left}"
    if st.status == SELECTING:
        if st.selection_opens_in:
            return f"chờ mở cửa sổ chọn xe ({st.selection_opens_in:.0f}s)"
        if st.selection_deadline_in is not None:
            return f"đang chọn xe (còn {st.selection_deadline_in:.0f}s)"
        return "chờ chọn xe (tự nhịp)"
    if st.status == FINISHED:
        return "đã kết thúc"
    return st.status or "?"


def resolve(api, match=None, question=None, game=None, practice: bool = False,
            label: Optional[str] = None,
            log: Optional[Callable[[str], None]] = None) -> List[GameRef]:
    """Các câu cần chơi. `game` đi thẳng; còn lại tra theo ID/tên trận rồi tên/ID câu."""
    if game:
        return [GameRef(game_id=game, question=label, mode=PRACTICE if practice else CONTEST)]
    if not match:
        raise ResolveError("cần --game <ID câu> hoặc --match <ID hoặc tên trận>")
    refs = fetch_questions(api, log)
    key = str(match)
    if key.isdigit():
        in_match = [r for r in refs if r.match_id == int(key)]
    else:
        in_match = [r for r in refs if (r.match_name or "").casefold() == key.casefold()]
    if not in_match:
        raise ResolveError(f"không có trận {match!r}. Các trận đội bạn thấy:\n" + format_table(refs))
    if len({r.match_id for r in in_match}) > 1:
        raise ResolveError(f"tên trận {match!r} trùng nhiều trận; dùng ID trận:\n" + format_table(in_match))
    if in_match[0].mode == COMPETITIVE_PRACTICE:
        raise ResolveError(f"trận {match!r} là competitive_practice — chế độ này chưa được hỗ trợ")
    if question is None:
        return in_match
    q = str(question)
    if UUID_RE.match(q):
        picked = [r for r in in_match if r.game_id.lower() == q.lower()]
    else:
        picked = [r for r in in_match if (r.question or "").casefold() == q.casefold()]
    if len(picked) != 1:
        why = "không có" if not picked else "trùng"
        raise ResolveError(f"{why} câu {question!r} trong trận {match!r}:\n" + format_table(in_match))
    return picked
