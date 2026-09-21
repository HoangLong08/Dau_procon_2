"""Tầng HTTP dùng chung cho các kênh mạng: SSL, User-Agent, chặn cứng đồng hồ.

Chỉ dùng thư viện chuẩn. Hai bài học đo được trên server thi thật ngày
2026-09-21 nằm ở đây, thay vì rải trong từng client:

- Cloudflare trả 403 "error code: 1010" cho User-Agent mặc định Python-urllib;
- Python cài từ python.org trên macOS không mang kho CA, HTTPS nào cũng lỗi SSL.
"""

import base64
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

USER_AGENT = "hexudon-bot/1.0"


@dataclass
class Response:
    status: int                                    # 0 nghĩa là lỗi mạng hoặc quá hạn
    payload: object = None                         # JSON đã đọc; body lạ thành {"raw": ...}
    headers: Dict[str, str] = field(default_factory=dict)


def load_env(path: str) -> Dict[str, str]:
    """Đọc file .env dạng KEY=VALUE; thiếu file thì trả rỗng."""
    values: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def jwt_claims(token: str) -> dict:
    """Đọc claims của JWT (id đội, tên đội, hạn dùng) mà không xác minh chữ ký.

    Bot không có khoá và chỉ cần đọc; server mới là bên kiểm tra. Token hỏng
    thì trả {} thay vì ném lỗi.
    """
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def default_ca_files() -> List[str]:
    """Kho CA dự phòng theo thứ tự ưu tiên."""
    return [os.environ.get("SSL_CERT_FILE", ""), "/etc/ssl/cert.pem",
            "/etc/ssl/certs/ca-certificates.crt"]


def make_ssl_context(log: Optional[Callable[[str], None]] = None,
                     base: Optional[ssl.SSLContext] = None,
                     ca_files: Optional[Sequence[str]] = None) -> ssl.SSLContext:
    """Ngữ cảnh SSL luôn có kho CA. Không bao giờ tắt xác minh chứng chỉ."""
    log = log or (lambda msg: None)
    ctx = base if base is not None else ssl.create_default_context()
    if ctx.get_ca_certs():
        return ctx
    for path in (default_ca_files() if ca_files is None else ca_files):
        if path and os.path.isfile(path):
            ctx.load_verify_locations(cafile=path)
            log(f"Python không có kho CA riêng; dùng {path}")
            return ctx
    return ctx


def _parse_body(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw.decode("utf-8", "replace")[:500]}


class JsonHttp:
    """Gọi JSON qua HTTP(S) với chặn cứng theo đồng hồ treo tường."""

    def __init__(self, base_url: str, timeout: float = 2.0,
                 log: Optional[Callable[[str], None]] = None,
                 replay: Optional[Callable[[dict], None]] = None,
                 secrets: Optional[Callable[[], Sequence[str]]] = None,
                 ssl_context: Optional[ssl.SSLContext] = None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.raw_log = log or (lambda msg: None)
        self.replay = replay or (lambda entry: None)
        self.secrets = secrets or (lambda: ())
        if ssl_context is None and self.base.startswith("https://"):
            ssl_context = make_ssl_context(self.log)
        self.ssl_context = ssl_context

    def scrub(self, text: str) -> str:
        """Thay mọi token trong chuỗi bằng <token> trước khi ghi ra ngoài."""
        for secret in self.secrets():
            if secret:
                text = text.replace(secret, "<token>")
        return text

    def log(self, msg: str) -> None:
        self.raw_log(self.scrub(msg))

    def call(self, method: str, path: str, params: Optional[dict] = None, body=None,
             headers: Optional[Dict[str, str]] = None,
             deadline: Optional[float] = None) -> Response:
        """Một lời gọi, không bao giờ sống quá `timeout` hay quá `deadline`.

        `timeout` của socket chỉ áp cho MỖI LẦN ĐỌC: một server nhả body từng
        byte kéo được một request dài vô hạn mà không hề timeout (đo được 58 s
        với timeout 2 s). Nên lời gọi chạy trong luồng riêng và bị bỏ rơi khi
        hết giờ — thà mất một request còn hơn mất cả ngày. `deadline` là mốc
        time.monotonic().
        """
        budget = self.timeout
        if deadline is not None:
            budget = min(budget, deadline - time.monotonic())
        if budget <= 0:
            self.log(f"bỏ qua {method} {path}: đã quá hạn của ngày")
            return Response(0)
        done: List[Response] = []
        worker = threading.Thread(
            target=lambda: done.append(self._blocking(method, path, params, body, headers, budget)),
            daemon=True,
        )
        worker.start()
        worker.join(budget)
        if not done:
            self.log(f"lỗi mạng {method} {path}: quá {budget:.1f}s, bỏ cuộc gọi")
            self.replay({"t": time.time(), "method": method, "path": path,
                         "status": 0, "error": f"wall-clock timeout {budget:.1f}s"})
            return Response(0)
        return done[0]

    def _blocking(self, method, path, params, body, headers, timeout) -> Response:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        all_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        entry = {"t": time.time(), "method": method, "path": path}
        if params:
            entry["params"] = dict(params)
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
        except Exception as exc:
            # ví dụ --url thiếu scheme: phải báo lỗi chứ không được ném ra ngoài
            self.log(f"URL không hợp lệ cho {method} {path}: {exc!r}")
            return Response(0)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ssl_context) as resp:
                status, raw, hdrs = resp.status, resp.read(), dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
            hdrs = dict(exc.headers.items()) if exc.headers is not None else {}
        except Exception as exc:
            error = self.scrub(repr(exc))
            self.log(f"lỗi mạng {method} {path}: {error}")
            self.replay({**entry, "status": 0, "error": error})
            return Response(0)
        payload = _parse_body(raw)
        self.replay({**entry, "status": status, "response": payload})
        return Response(status, payload, hdrs)
