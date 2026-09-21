"""Kênh HTTP cho server kiểu PTIT.

Trình tự: GET /setup -> POST /assignment -> lặp GET /state + POST /actions -> GET /result.
Chỉ dùng thư viện chuẩn. Poll tối thiểu 200 ms để tránh HTTP 429. Tầng HTTP (chặn
cứng đồng hồ, SSL, User-Agent) nằm ở net.JsonHttp, dùng chung với kênh web.
"""

import time
from typing import Callable, List, Optional, Sequence, Tuple

from ..model import Ack, DayState, MatchConfig, ProtocolError
from .net import JsonHttp
from .transport import MatchEnded, Transport

POLL_SECONDS = 0.25
REQUEST_TIMEOUT = 2.0
RETRY_BACKOFF = (0.25, 0.5, 1.0)
#: mã HTTP nghĩa là trận đã đóng, khác với sự cố tạm thời
TERMINAL_STATUSES = (409, 410)
#: cứ ngần này lần /state hỏng thì hỏi /result một lần
RESULT_PROBE_EVERY = 8
#: im lặng quá lâu thì thoát hẳn thay vì quay vô hạn
GIVE_UP_SECONDS = 120.0


class HttpTransport(Transport):
    def __init__(self, base_url: str, match_id: str, token: str,
                 poll_seconds: float = POLL_SECONDS,
                 timeout: float = REQUEST_TIMEOUT,
                 give_up_seconds: float = GIVE_UP_SECONDS,
                 log: Optional[Callable[[str], None]] = None,
                 replay: Optional[Callable[[dict], None]] = None):
        base = base_url.rstrip("/")
        self.api = f"{base}/api/v1/matches/{match_id}"
        self.token = token
        self.poll_seconds = poll_seconds
        self.timeout = timeout
        self.give_up_seconds = give_up_seconds
        self.log = log or (lambda msg: None)
        self.replay = replay or (lambda entry: None)
        self.http = JsonHttp(self.api, timeout=timeout, log=self.log, replay=self.replay,
                             secrets=lambda: (self.token,))
        self.last_day = -1
        self.config: Optional[MatchConfig] = None
        self.kinds: Optional[List[int]] = None
        self.actions_style: Optional[str] = None   # 'bare' hoặc 'wrapped', học từ lần gửi đầu
        self.assignment_body = None
        self.body_style = None

    # ---- tầng HTTP ----

    def _call(self, method: str, path: str, body=None,
              deadline: Optional[float] = None) -> Tuple[int, Optional[dict]]:
        """Gọi server với chặn cứng theo đồng hồ treo tường (xem JsonHttp.call)."""
        resp = self.http.call(method, path, body=body,
                              headers={"Authorization": "Bearer " + self.token},
                              deadline=deadline)
        return resp.status, resp.payload

    def _call_with_retry(self, method: str, path: str, body=None,
                         accept=(200,),
                         deadline: Optional[float] = None) -> Tuple[int, Optional[dict]]:
        """Thử lại có giãn cách, nhưng không bao giờ vượt hạn của ngày."""
        status, payload = self._call(method, path, body, deadline=deadline)
        if status in accept:
            return status, payload
        for delay in RETRY_BACKOFF:
            if status in (400, 404, 409, 422):
                break
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    self.log(f"ngừng thử lại {method} {path}: hết hạn của ngày")
                    break
            time.sleep(delay)
            status, payload = self._call(method, path, body, deadline=deadline)
            if status in accept:
                return status, payload
        return status, payload

    # ---- giao diện Transport ----

    def get_setup(self) -> MatchConfig:
        while True:
            status, payload = self._call("GET", "/setup")
            if status == 200 and payload:
                try:
                    self.config = MatchConfig.from_json(payload, warn=self.log)
                except ProtocolError as exc:
                    # trường lạ không được giết bot: server có thể sửa ở lần sau
                    self.log(f"setup không đọc được ({exc}); hỏi lại")
                    time.sleep(self.poll_seconds)
                    continue
                self.log(f"setup: {self.config.width}x{self.config.height}, "
                         f"{self.config.n_agents} agent, {self.config.n_days} ngày, "
                         f"{len(self.config.spots)} spot, {len(self.config.brands)} chuỗi")
                return self.config
            if status in (401, 403):
                raise ProtocolError(f"server từ chối token (HTTP {status}) — kiểm tra --match và --token")
            self.log(f"chờ setup (HTTP {status})")
            time.sleep(self.poll_seconds)

    def send_assignment(self, kinds: Sequence[int], deadline: Optional[float] = None) -> Ack:
        """Gửi mảng loại xe. Thử các kiểu bọc JSON khác nếu server từ chối kiểu mặc định."""
        self.kinds = list(kinds)
        bodies = [list(kinds), {"types": list(kinds)}, {"kinds": list(kinds)},
                  {"agents": list(kinds)}]
        if self.body_style is not None:
            bodies.insert(0, self.body_style("assignment", list(kinds)))
        last = None
        for body in bodies:
            status, payload = self._call_with_retry("POST", "/assignment", body, deadline=deadline)
            if status == 200:
                self.assignment_body = body
                return Ack(ok=True, raw=payload)
            last = (status, payload)
            if status in (401, 403, 404, 425, 429, 0):
                # không phải lỗi định dạng: đổi kiểu bọc cũng vô ích
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
        self.log(f"gửi vai trò thất bại (HTTP {last[0]}): {last[1]}")
        return Ack(ok=False, reason=str(last[0]), raw=last[1] if isinstance(last[1], dict) else None)

    def parse_state(self, payload: dict, kinds: Optional[Sequence[int]]) -> DayState:
        """Đọc /state với bối cảnh từ setup (bề rộng map, dung tích bình)."""
        return DayState.from_json(
            payload, kinds,
            width=self.config.width if self.config else None,
            fuel_limits=self.config.fuel_limits if self.config else None,
        )

    def looks_finished(self, result: Optional[dict], status: int) -> bool:
        """Chỉ coi là hết trận khi server nói vậy.

        Trước đây bất kỳ payload nào từ /result cũng kết thúc trận, nên một lỗi
        503 thoáng qua làm bot bỏ cuộc giữa chừng mà vẫn in bảng điểm như thường.
        """
        if not isinstance(result, dict):
            return False
        if result.get("finished") is True:
            return True
        # 409/410 là mã kết thúc tường minh; các mã khác chỉ là sự cố
        return status in TERMINAL_STATUSES

    def wait_day_state(self, kinds: Optional[Sequence[int]] = None) -> DayState:
        kinds = kinds if kinds is not None else self.kinds
        failures = 0
        last_good = time.monotonic()
        while True:
            status, payload = self._call("GET", "/state")
            if status == 200 and payload is not None:
                failures = 0
                last_good = time.monotonic()
                day = int(payload.get("day", 0))
                if day != self.last_day:
                    try:
                        state = self.parse_state(payload, kinds)
                    except ProtocolError as exc:
                        self.log(f"trạng thái ngày không đọc được ({exc}); hỏi lại")
                        time.sleep(self.poll_seconds)
                        continue
                    self.last_day = day
                    return state
                time.sleep(self.poll_seconds)
                continue

            failures += 1
            terminal = status in TERMINAL_STATUSES
            if terminal or failures % RESULT_PROBE_EVERY == 0:
                result = self.get_result()
                if self.looks_finished(result, status):
                    raise MatchEnded(result)
            silence = time.monotonic() - last_good
            if silence > self.give_up_seconds:
                # thà thoát có mã lỗi còn hơn quay vô hạn không ai biết
                raise MatchEnded(
                    f"mất liên lạc với server {silence:.0f}s (HTTP {status} lần cuối)")
            if failures == 1 or failures % RESULT_PROBE_EVERY == 0:
                self.log(f"/state lỗi (HTTP {status}), đã {failures} lần, "
                         f"im lặng {silence:.0f}/{self.give_up_seconds:.0f}s")
            # giãn dần để không đập server đang ốm
            time.sleep(min(self.poll_seconds * (1 + failures // RESULT_PROBE_EVERY), 2.0))

    def send_actions(self, day: int, plan: List[List[int]],
                     deadline: Optional[float] = None) -> Ack:
        """Gửi kế hoạch ngày. Ghi nhớ kiểu bọc JSON mà server chấp nhận."""
        bodies = [plan]
        if self.actions_style == "wrapped":
            bodies = [{"day": day, "actions": plan}, plan]
        elif self.actions_style is None:
            bodies = [plan, {"day": day, "actions": plan}, {"actions": plan}]
        last = (0, None)
        for body in bodies:
            status, payload = self._call_with_retry("POST", "/actions", body, deadline=deadline)
            reason = None
            ok = status == 200
            if isinstance(payload, dict):
                reason = payload.get("reason") or payload.get("error")
                if payload.get("ok") is False:
                    ok = False
                elif ok and reason and str(reason).upper().startswith("E_"):
                    # server có thể báo lỗi luật trong thân mà vẫn trả HTTP 200;
                    # coi đó là thành công thì cả trận bị từ chối mà log vẫn xanh
                    ok = False
            if ok:
                self.actions_style = "wrapped" if isinstance(body, dict) else "bare"
                return Ack(ok=True, reason=None, raw=payload if isinstance(payload, dict) else None)
            last = (status, payload)
            # lỗi luật chơi thì đổi kiểu bọc cũng vô ích
            if reason and reason.startswith("E_") and reason != "E_BAD_FORMAT":
                break
            if status in (401, 403, 409, 425, 0):
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
        status, payload = last
        reason = payload.get("reason") or payload.get("error") if isinstance(payload, dict) else None
        self.log(f"bài ngày {day} bị từ chối (HTTP {status}): {reason or payload}")
        return Ack(ok=False, reason=reason or str(status),
                   raw=payload if isinstance(payload, dict) else None)

    def get_result(self) -> Optional[dict]:
        status, payload = self._call("GET", "/result")
        return payload if status == 200 else None
