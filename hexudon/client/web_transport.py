"""Kênh truyền cho server thi hexudon.hairbui76.id.vn: contest và practice.

Contest (Mock Contest, thi thật): nhiều đội chung bàn, có giờ; gửi bài an toàn
rồi gửi lại bài tốt hơn tới sát hạn. Practice: mỗi đội một bản tự nhịp, mỗi lần
nộp là chốt ngày, nên chỉ gửi đúng một lần mỗi ngày.

Điều đã đo trên server thật (2026-09-21) mà kênh này dựa vào:
- /day trả 409 "no day is open yet" lúc chọn xe và giữa hai ngày: đó là "chưa
  mở", không bao giờ là hết trận;
- hạn trong /state là số giây CÒN LẠI, nên không phụ thuộc đồng hồ máy;
- bản đồ bị giấu (board_withheld) tới khi cửa sổ chọn xe mở;
- /practice/score chỉ nhận ID ghép "<câu>:<đội>".
"""

import json
import os
import time
from typing import Callable, Dict, List, Optional, Sequence

from ..model import Ack, DayState, MatchConfig, ProtocolError
from ..simulator import wait_plan
from .net import Response
from .replay_check import check as check_replay
from .transport import MatchEnded, SelectionWindow, Transport
from .web_api import FINISHED, IN_PROGRESS, SELECTING, GameStatus, WebApi, error_detail
from .web_games import PRACTICE, GameRef

#: dậy trước khi cửa sổ chọn xe mở ngần này giây (website cũng tải lại bản đồ sớm 15 s)
WAKE_BEFORE_WINDOW = 15.0
#: mỗi giấc chờ tối đa ngần này, để vẫn thấy BTC reset hay dời giờ
MAX_NAP = 30.0
#: nhịp gửi lại request và đọc lại .env khi token bị từ chối
TOKEN_RETRY_SECONDS = 5.0
RETRY_SECONDS = 0.3
#: giờ dành riêng cho POST vai trò trước khi cửa sổ đóng: một cuộc gọi 5 s cộng lề thử lại
SELECTION_SEND_RESERVE = 6.0
#: sau trận không còn hạn nào: /result và /replay được chờ lâu hơn hạn mỗi cuộc gọi
POST_MATCH_TIMEOUT = 30.0


def score_of(result: Optional[dict], team_id: Optional[int]) -> Optional[dict]:
    """(L, D, Q), hạng và số ngày đã nộp của đội, đọc từ /result hoặc /practice/score."""
    if not isinstance(result, dict) or team_id is None:
        return None
    detail = (result.get("detail") or {}).get(str(team_id))
    if not isinstance(detail, dict):
        return None
    ranking = [str(t) for t in result.get("ranking") or []]
    return {"L": int(detail.get("distinct_types") or 0),
            "D": int(detail.get("cumulative_daily_types") or 0),
            "Q": int(detail.get("total_servings") or 0),
            "rank": ranking.index(str(team_id)) + 1 if str(team_id) in ranking else None,
            "teams": len(ranking),
            "days_submitted": detail.get("days_submitted")}


def _cloudflare(resp: Response) -> bool:
    return resp.status == 403 and "1010" in error_detail(resp.payload)


def _retry_after(resp: Response) -> Optional[float]:
    for key, value in (resp.headers or {}).items():
        if key.lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except ValueError:
                return None
    return None


def _rejected(payload) -> bool:
    """Server có thể báo lỗi luật trong thân mà vẫn trả 2xx."""
    if not isinstance(payload, dict):
        return False
    return payload.get("ok") is False or str(payload.get("reason", "")).upper().startswith("E_")


def _accepted(resp: Response) -> bool:
    """Được nhận: 2xx VÀ thân không báo lỗi luật."""
    payload = resp.payload if isinstance(resp.payload, dict) else None
    return 200 <= resp.status < 300 and not _rejected(payload)


class WebTransport(Transport):
    """Một câu trên server web; mỗi tiến trình bot giữ đúng một đối tượng này."""

    def __init__(self, api: WebApi, ref: GameRef, team_id: Optional[int], *,
                 keep: bool = False, live: bool = False,
                 poll_seconds: float = 0.5, give_up_seconds: float = 120.0,
                 token_reload: Optional[Callable[[], bool]] = None, token_var: str = "token",
                 log: Optional[Callable[[str], None]] = None,
                 replay: Optional[Callable[[dict], None]] = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.practice = ref.mode == PRACTICE
        if live and self.practice:
            raise ValueError("--live không bao giờ chơi practice")
        self.api = api
        self.ref = ref
        self.team_id = team_id
        self.keep = keep
        self.poll_seconds = poll_seconds
        self.give_up_seconds = give_up_seconds
        self.token_reload = token_reload or (lambda: False)
        self.token_var = token_var
        self.log = log or (lambda msg: None)
        self.replay = replay or (lambda entry: None)
        self.clock = clock
        self.sleep = sleep
        #: practice: mỗi lần nộp là chốt ngày, nên session chỉ được gửi một lần
        self.supports_resubmit = not self.practice
        self.config: Optional[MatchConfig] = None
        self.window: Optional[SelectionWindow] = None
        self.selection_limit: Optional[float] = None
        self.kinds: Optional[List[int]] = None
        self.last_day = -1
        self.accepted_plans: Dict[int, List[List[int]]] = {}
        self.result: Optional[dict] = None
        self._bare: set = set()          # endpoint practice đã phải lùi về ID trần
        self._sent_at = 0.0              # mốc ngay trước lời gọi gần nhất

    # ---- tầng gọi ----

    def _gid(self, endpoint: str) -> str:
        """Practice dùng ID ghép "<câu>:<đội>" (/practice/score chỉ nhận kiểu này)."""
        if self.practice and self.team_id is not None and endpoint not in self._bare:
            return f"{self.ref.game_id}:{self.team_id}"
        return self.ref.game_id

    def _call(self, endpoint: str, fn: Callable[[str], Response],
              deadline: Optional[float] = None) -> Response:
        """Gọi một endpoint với game_id đúng kiểu; tự lo 401/403 (gửi lại, đọc lại .env) và 429."""
        backoff = 1.0
        waited = 0.0          # số giây đã ngủ vì token bị từ chối, trong riêng lời gọi này
        while True:
            gid = self._gid(endpoint)
            self._sent_at = self.clock()
            resp = fn(gid)
            if resp.status == 404 and gid != self.ref.game_id:
                self.log(f"{endpoint}: ID ghép bị 404, thử ID câu trần")
                self._bare.add(endpoint)
                continue
            if _cloudflare(resp):
                raise MatchEnded("Cloudflare chặn chữ ký của bot (403, error code 1010)")
            if resp.status in (401, 403):
                # có khi chỉ thoáng qua: gửi lại đúng request này, có token mới trong .env thì dùng luôn
                if not waited:
                    self.log(f"server từ chối token (HTTP {resp.status}) — lấy token mới trên website "
                             f"(icon 🔑) rồi cập nhật {self.token_var} trong .env; bot đang chờ và tự đọc lại")
                if waited >= self.give_up_seconds:
                    raise MatchEnded(f"token vẫn bị từ chối sau {self.give_up_seconds:.0f}s thử lại")
                self.sleep(TOKEN_RETRY_SECONDS)
                waited += TOKEN_RETRY_SECONDS
                if self.token_reload():
                    self.log("đã đọc được token mới, chơi tiếp")
                continue
            if resp.status == 429:
                wait = _retry_after(resp)
                wait = backoff if wait is None else wait
                if deadline is not None and time.monotonic() + wait >= deadline:
                    return resp
                self.sleep(wait)
                backoff = min(backoff * 2, 5.0)
                continue
            return resp

    def _status(self) -> Optional[GameStatus]:
        resp = self._call("state", self.api.state)
        if resp.status == 200 and isinstance(resp.payload, dict):
            st = GameStatus.from_json(resp.payload, self.team_id)
            # mốc TRƯỚC khi gọi: hạn tính ra chỉ có thể sớm hơn thật, không bao giờ muộn hơn
            st.read_at = self._sent_at
            return st
        if resp.status == 404:
            raise MatchEnded(f"không có câu {self.ref.game_id} (HTTP 404: {error_detail(resp.payload)})")
        self.log(f"/state lỗi (HTTP {resp.status}): {error_detail(resp.payload)}")
        return None

    def _check_silence(self, last_good: float) -> None:
        silence = self.clock() - last_good
        if silence > self.give_up_seconds:
            raise MatchEnded(f"mất liên lạc với server {silence:.0f}s")

    def _post(self, endpoint: str, fn: Callable[[str], Response],
              deadline: Optional[float], max_attempts: int) -> Response:
        """Một POST, thử lại lỗi tạm thời (mạng, 5xx) trong hạn; trả response cuối cùng."""
        attempts = 0
        while True:
            resp = self._call(endpoint, fn, deadline=deadline)
            if _accepted(resp):
                return resp
            attempts += 1
            transient = resp.status == 0 or resp.status >= 500
            room = deadline is None or time.monotonic() + RETRY_SECONDS < deadline
            if not (transient and room and attempts < max_attempts):
                return resp
            self.sleep(RETRY_SECONDS)

    # ---- trước trận ----

    def get_setup(self) -> MatchConfig:
        if self.practice:
            self._reset_practice("trước khi chơi")
        last_good = self.clock()
        while True:
            st = self._status()
            if st is None:
                self._check_silence(last_good)
                self.sleep(min(2.0, max(self.poll_seconds, 0.25)))
                continue
            last_good = self.clock()
            if st.status == FINISHED:
                self.get_result()          # kết quả nằm ở self.result; thông điệp chỉ là một câu ngắn
                raise MatchEnded("câu đã kết thúc")
            opens_in = st.selection_opens_in or 0.0
            if st.status == SELECTING and not self.practice and opens_in > 0:
                if opens_in > WAKE_BEFORE_WINDOW:
                    nap = min(opens_in - WAKE_BEFORE_WINDOW, MAX_NAP)
                    self.log(f"cửa sổ chọn xe mở sau {opens_in:.0f}s; chờ")
                else:
                    nap = min(1.0, opens_in)
                self.sleep(nap)
                continue
            config = self._fetch_config()
            if config is None:
                self.sleep(1.0)
                continue
            self.config = config
            self.window = self._window_for(st)
            self.log(f"setup: {config.width}x{config.height}, {config.n_agents} xe, "
                     f"{config.n_days} ngày, {len(config.spots)} spot — {self.ref.label()}")
            return config

    def _fetch_config(self) -> Optional[MatchConfig]:
        resp = self._call("config", self.api.config)
        payload = resp.payload
        if resp.status != 200 or not isinstance(payload, dict):
            if resp.status != 409:
                self.log(f"/config lỗi (HTTP {resp.status}): {error_detail(payload)}")
            return None
        if payload.get("board_withheld") or not (payload.get("map") or {}).get("cells"):
            return None          # bản đồ chưa công bố
        try:
            config = MatchConfig.from_json(payload, warn=self.log)
        except ProtocolError as exc:
            self.log(f"config không đọc được ({exc}); hỏi lại")
            return None
        limit = payload.get("agent_selection_time_limit")
        self.selection_limit = float(limit) if limit is not None else None
        return config

    def _window_for(self, st: GameStatus) -> SelectionWindow:
        # 0 là cửa sổ dài 0 giây, không phải "không biết": không được rơi về giá trị trong /config
        limit = st.selection_limit if st.selection_limit is not None else self.selection_limit
        if st.status != SELECTING:
            return SelectionWindow(open=False, limit=limit)
        if self.practice:
            return SelectionWindow(open=True, deadline=None, limit=limit)
        if st.selection_deadline_in is not None:
            deadline = st.read_at + st.selection_deadline_in
        else:
            # server không báo hạn: chặn theo giờ mở ngày 0, không bao giờ để không giới hạn
            deadline = self.config.starts_at if self.config is not None and self.config.starts_at \
                else self.clock()
        return SelectionWindow(open=True, deadline=deadline, limit=limit, reserve=SELECTION_SEND_RESERVE)

    def selection_window(self) -> Optional[SelectionWindow]:
        return self.window

    def send_assignment(self, kinds: Sequence[int], deadline: Optional[float] = None) -> Ack:
        types = [int(k) for k in kinds]
        resp = self._post("agent-types",
                          lambda gid: self.api.agent_types(gid, types, deadline=deadline),
                          deadline, max_attempts=10)
        payload = resp.payload if isinstance(resp.payload, dict) else None
        if _accepted(resp):
            self.kinds = types
            return Ack(ok=True, raw=payload)
        reason = error_detail(resp.payload) or f"HTTP {resp.status}"
        self.log(f"gửi vai trò thất bại (HTTP {resp.status}): {reason}")
        return Ack(ok=False, reason=reason, raw=payload)

    # ---- mỗi ngày ----

    def wait_day_state(self, kinds: Optional[Sequence[int]] = None) -> DayState:
        kinds = list(kinds) if kinds is not None else self.kinds
        failures = 0
        last_good = self.clock()
        while True:
            st = self._status()
            if st is None:
                failures += 1
                self._check_silence(last_good)
                self.sleep(min(max(self.poll_seconds, 0.05) * (1 + failures // 4), 2.0))
                continue
            failures = 0
            last_good = self.clock()
            if st.status == FINISHED:
                self.get_result()          # kết quả nằm ở self.result; thông điệp chỉ là một câu ngắn
                raise MatchEnded("câu đã kết thúc")
            if st.status == IN_PROGRESS and st.day > self.last_day:
                state = self._fetch_day(st, kinds)
                if state is not None:
                    self.last_day = state.day
                    return state
            elif self._practice_stuck(st):
                continue
            nap = self.poll_seconds
            if st.status == SELECTING and st.selection_deadline_in is not None:
                # vai trò đã gửi, cửa sổ còn mở: ngủ tới sát giờ mở ngày 0 thay vì hỏi mỗi nhịp
                nap = min(max(self.poll_seconds, st.selection_deadline_in - 1.0), MAX_NAP)
            self.sleep(nap)

    def _practice_stuck(self, st: GameStatus) -> bool:
        """Practice chỉ sang ngày khi có bài: ngày chưa có bài nào được nhận thì gửi bài đứng yên."""
        if not self.practice or st.status != IN_PROGRESS or st.day != self.last_day:
            return False
        if st.day in self.accepted_plans or self.config is None:
            return False
        self.log(f"Ngày {st.day + 1}: chưa có bài nào được nhận; gửi bài đứng yên để trận đi tiếp")
        self.send_actions(st.day, wait_plan(self.config.n_agents, self.config.steps_of_day(st.day)))
        self.sleep(1.0)
        return True

    def _fetch_day(self, st: GameStatus, kinds) -> Optional[DayState]:
        resp = self._call("day", self.api.day)
        if resp.status != 200 or not isinstance(resp.payload, dict):
            if resp.status != 409:
                self.log(f"/day lỗi (HTTP {resp.status}): {error_detail(resp.payload)}")
            return None
        try:
            state = DayState.from_json(resp.payload, kinds, width=self.config.width,
                                       fuel_limits=self.config.fuel_limits)
        except ProtocolError as exc:
            self.log(f"/day không đọc được ({exc}); hỏi lại")
            return None
        if state.day <= self.last_day:
            return None
        if state.day != st.day:
            # ngày vừa sang giữa hai lời gọi: hạn trong st là của ngày cũ
            st = self._status()
            if st is None or st.day != state.day:
                return None
        if self.practice:
            state.ends_at = None
        elif st.day_deadline_in is not None:
            state.ends_at = st.read_at + st.day_deadline_in
        if state.agents:
            self.kinds = [a.kind for a in state.agents]
        return state

    def send_actions(self, day: int, plan: List[List[int]],
                     deadline: Optional[float] = None) -> Ack:
        endpoint = "practice/actions" if self.practice else "actions"
        post = self.api.practice_actions if self.practice else self.api.actions
        resp = self._post(endpoint, lambda gid: post(gid, day, plan, deadline=deadline),
                          deadline, max_attempts=5)
        payload = resp.payload if isinstance(resp.payload, dict) else None
        if _accepted(resp):
            self.accepted_plans[day] = [list(seq) for seq in plan]
            return Ack(ok=True, raw=payload)
        reason = error_detail(resp.payload) or f"HTTP {resp.status}"
        return Ack(ok=False, reason=reason, raw=payload)

    # ---- sau trận ----

    def _read(self, endpoint: str, fn: Callable[[str], Response], attempts: int) -> Response:
        """Một lời đọc; lỗi tạm thời (mạng, 5xx) được thử lại tới `attempts` lần, cách nhau 1 s."""
        resp = self._call(endpoint, fn)
        for _ in range(attempts - 1):
            if resp.status != 0 and resp.status < 500:
                break
            self.sleep(1.0)
            resp = self._call(endpoint, fn)
        return resp

    def get_result(self, timeout: Optional[float] = None, attempts: int = 1) -> Optional[dict]:
        if self.practice:
            resp = self._read("practice/score",
                              lambda gid: self.api.practice_score(gid, timeout=timeout), attempts)
        else:
            resp = self._read("result", lambda gid: self.api.result(gid, timeout=timeout), attempts)
        if resp.status == 200 and isinstance(resp.payload, dict):
            self.result = resp.payload
        return self.result

    def fetch_replay(self, timeout: Optional[float] = None, attempts: int = 1) -> Optional[dict]:
        resp = self._read("replay", lambda gid: self.api.replay(gid, timeout=timeout), attempts)
        return resp.payload if resp.status == 200 and isinstance(resp.payload, dict) else None

    def finish(self, summary_path: Optional[str] = None, extra: Optional[dict] = None) -> dict:
        """Sau trận: lấy kết quả và replay, lưu tổng kết; practice thì reset (trừ khi keep)."""
        result = self.get_result(timeout=POST_MATCH_TIMEOUT, attempts=3)
        replay = self.fetch_replay(timeout=POST_MATCH_TIMEOUT, attempts=3)
        summary = {"game_id": self.ref.game_id, "label": self.ref.label(), "mode": self.ref.mode,
                   "team_id": self.team_id, "finished_at": time.time(),
                   "score": score_of(result, self.team_id), "result": result,
                   "accepted_days": sorted(self.accepted_plans),
                   "replay_days": len((replay or {}).get("days") or [])}
        summary.update(extra or {})
        if self.config is not None and replay is not None:
            try:
                report = check_replay(self.config, self.team_id, replay, self.accepted_plans)
            except Exception as exc:
                # đối chiếu chỉ để chẩn đoán: replay lạ không được làm mất kết quả trận
                self.log(f"đối chiếu replay lỗi ({exc!r}); vẫn lưu kết quả")
                summary["replay_check"] = {"error": repr(exc)}
            else:
                self.log(report.summary())
                summary["replay_check"] = report.to_json()
        if summary_path:
            directory = os.path.dirname(summary_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(summary_path, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, ensure_ascii=False, indent=2)
            if replay is not None:
                base = summary_path[:-len("-summary.json")] if summary_path.endswith("-summary.json") \
                    else summary_path
                with open(base + "-replay.json", "w", encoding="utf-8") as fh:
                    json.dump(replay, fh)
        if self.practice and not self.keep:
            self._reset_practice("sau khi đã lưu kết quả")
        return summary

    def _reset_practice(self, why: str) -> None:
        resp = self._call("practice/reset", self.api.practice_reset)
        if 200 <= resp.status < 300:
            self.log(f"đã reset practice {why}")
        else:
            self.log(f"reset practice thất bại (HTTP {resp.status}): {error_detail(resp.payload)}")
