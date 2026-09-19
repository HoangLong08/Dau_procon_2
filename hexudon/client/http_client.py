"""Kênh HTTP cho server kiểu PTIT.

Trình tự: GET /setup -> POST /assignment -> lặp GET /state + POST /actions -> GET /result.
Chỉ dùng thư viện chuẩn. Poll tối thiểu 200 ms để tránh HTTP 429.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Callable, List, Optional, Sequence, Tuple

from ..model import Ack, DayState, MatchConfig
from .transport import MatchEnded, Transport

POLL_SECONDS = 0.25
REQUEST_TIMEOUT = 2.0
RETRY_BACKOFF = (0.25, 0.5, 1.0)


class HttpTransport(Transport):
    def __init__(self, base_url: str, match_id: str, token: str,
                 poll_seconds: float = POLL_SECONDS,
                 timeout: float = REQUEST_TIMEOUT,
                 log: Optional[Callable[[str], None]] = None,
                 replay: Optional[Callable[[dict], None]] = None):
        base = base_url.rstrip("/")
        self.api = f"{base}/api/v1/matches/{match_id}"
        self.token = token
        self.poll_seconds = poll_seconds
        self.timeout = timeout
        self.log = log or (lambda msg: None)
        self.replay = replay or (lambda entry: None)
        self.last_day = -1
        self.config: Optional[MatchConfig] = None
        self.kinds: Optional[List[int]] = None

    # ---- tầng HTTP ----

    def _call(self, method: str, path: str, body=None) -> Tuple[int, Optional[dict]]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.api + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                parsed = json.loads(raw) if raw else None
                self.replay({"t": time.time(), "method": method, "path": path,
                             "status": resp.status, "response": parsed})
                return resp.status, parsed
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw) if raw else None
            except Exception:
                parsed = {"raw": raw.decode("utf-8", "replace")[:500]}
            self.replay({"t": time.time(), "method": method, "path": path,
                         "status": exc.code, "response": parsed})
            return exc.code, parsed
        except Exception as exc:
            self.log(f"lỗi mạng {method} {path}: {exc!r}")
            self.replay({"t": time.time(), "method": method, "path": path,
                         "status": 0, "error": repr(exc)})
            return 0, None

    def _call_with_retry(self, method: str, path: str, body=None,
                         accept=(200,)) -> Tuple[int, Optional[dict]]:
        status, payload = self._call(method, path, body)
        if status in accept:
            return status, payload
        for delay in RETRY_BACKOFF:
            if status in (400, 404, 409, 422):
                break
            time.sleep(delay)
            status, payload = self._call(method, path, body)
            if status in accept:
                return status, payload
        return status, payload

    # ---- giao diện Transport ----

    def get_setup(self) -> MatchConfig:
        while True:
            status, payload = self._call("GET", "/setup")
            if status == 200 and payload:
                self.config = MatchConfig.from_json(payload)
                self.log(f"setup: {self.config.width}x{self.config.height}, "
                         f"{self.config.n_agents} agent, {self.config.n_days} ngày, "
                         f"{len(self.config.spots)} spot, {len(self.config.brands)} chuỗi")
                return self.config
            self.log(f"chờ setup (HTTP {status})")
            time.sleep(self.poll_seconds)

    def send_assignment(self, kinds: Sequence[int]) -> Ack:
        self.kinds = list(kinds)
        status, payload = self._call_with_retry("POST", "/assignment", list(kinds))
        ok = status == 200
        if not ok:
            self.log(f"gửi vai trò thất bại (HTTP {status}): {payload}")
        return Ack(ok=ok, reason=None if ok else str(status), raw=payload)

    def wait_day_state(self, kinds: Optional[Sequence[int]] = None) -> DayState:
        kinds = kinds if kinds is not None else self.kinds
        while True:
            status, payload = self._call("GET", "/state")
            if status == 200 and payload is not None:
                day = int(payload.get("day", 0))
                if day != self.last_day:
                    self.last_day = day
                    return DayState.from_json(payload, kinds)
            elif status not in (200, 425, 429, 0):
                result = self.get_result()
                if result is not None:
                    raise MatchEnded(result)
            time.sleep(self.poll_seconds)

    def send_actions(self, day: int, plan: List[List[int]]) -> Ack:
        status, payload = self._call_with_retry("POST", "/actions", plan)
        ok = status == 200
        reason = None
        if isinstance(payload, dict):
            reason = payload.get("reason") or payload.get("error")
            if payload.get("ok") is False:
                ok = False
        if not ok:
            self.log(f"bài ngày {day} bị từ chối (HTTP {status}): {reason or payload}")
        return Ack(ok=ok, reason=reason, raw=payload if isinstance(payload, dict) else None)

    def get_result(self) -> Optional[dict]:
        status, payload = self._call("GET", "/result")
        return payload if status == 200 else None
