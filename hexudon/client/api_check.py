"""Đối chiếu đặc tả API công khai của server thi (/openapi.json) với các endpoint client dùng.

Chạy trước ngày thi và sau mỗi lần BTC cập nhật server (R0, 16/11/2026):

    python -m hexudon api-check

Không cần token: /openapi.json đọc công khai. Báo lỗi khi một endpoint hay một trường
bắt buộc mà client dựa vào biến mất hoặc đổi tên, và liệt kê endpoint mới so với bản
đặc tả đã lưu trong tests/fixtures (để biết server có thêm chế độ chơi nào).
Manager service (/manager/api/...) không nằm trong đặc tả này nên không kiểm ở đây.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .net import JsonHttp

#: (method, đường dẫn) mà hexudon/client/web_api.py gọi tới game service
CLIENT_ENDPOINTS: Tuple[Tuple[str, str], ...] = (
    ("get", "/api/game/config"),
    ("get", "/api/game/state"),
    ("get", "/api/game/day"),
    ("get", "/api/game/replay"),
    ("get", "/api/game/result"),
    ("get", "/api/game/actions"),
    ("get", "/api/game/practice/score"),
    ("get", "/api/game/practice/peer"),
    ("post", "/api/game/agent-types"),
    ("post", "/api/game/actions"),
    ("post", "/api/game/practice/actions"),
    ("post", "/api/game/practice/reset"),
)

#: các khoá body mà client gửi, theo đúng web_api.py
CLIENT_BODIES: Dict[str, Set[str]] = {
    "/api/game/agent-types": {"game_id", "types"},
    "/api/game/actions": {"game_id", "day", "actions"},
    "/api/game/practice/actions": {"game_id", "day", "actions"},
    "/api/game/practice/reset": {"game_id"},
}

#: các GET client gửi kèm query game_id
CLIENT_QUERY = {"game_id"}

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "..", "tests", "fixtures", "openapi_2026-11-16.json")


@dataclass
class ApiReport:
    missing: List[str] = field(default_factory=list)          # endpoint client cần mà spec không có
    body_problems: List[str] = field(default_factory=list)    # trường body/query lệch
    new_endpoints: List[str] = field(default_factory=list)    # có trong spec mới, không có trong bản lưu
    removed_endpoints: List[str] = field(default_factory=list)
    title: str = ""
    version: str = ""

    @property
    def ok(self) -> bool:
        return not self.missing and not self.body_problems

    def render(self) -> str:
        lines = [f"đặc tả: {self.title} {self.version}".strip()]
        if self.ok:
            lines.append(f"đủ endpoint: {len(CLIENT_ENDPOINTS)} endpoint client dùng đều có, body khớp")
        for m in self.missing:
            lines.append(f"THIẾU: {m}")
        for b in self.body_problems:
            lines.append(f"LỆCH: {b}")
        if self.new_endpoints:
            lines.append("endpoint mới so với bản đã lưu: " + ", ".join(self.new_endpoints))
        if self.removed_endpoints:
            lines.append("endpoint đã bỏ so với bản đã lưu: " + ", ".join(self.removed_endpoints))
        return "\n".join(lines)


def _endpoints(spec: dict) -> Set[str]:
    out = set()
    for path, ops in (spec.get("paths") or {}).items():
        for method in ops:
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                out.add(f"{method.upper()} {path}")
    return out


def _resolve(spec: dict, schema: Optional[dict]) -> dict:
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 10:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return {}
        node = spec
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema = node
        seen += 1
    return schema if isinstance(schema, dict) else {}


def check(spec: dict, known: Optional[dict] = None) -> ApiReport:
    """So `spec` với các endpoint client dùng; `known` là bản đặc tả đã lưu (nếu có)."""
    info = spec.get("info") or {}
    report = ApiReport(title=str(info.get("title") or ""), version=str(info.get("version") or ""))
    paths = spec.get("paths") or {}
    for method, path in CLIENT_ENDPOINTS:
        op = (paths.get(path) or {}).get(method)
        if op is None:
            report.missing.append(f"{method.upper()} {path}")
            continue
        if method == "get":
            names = {p.get("name") for p in op.get("parameters") or [] if p.get("in") == "query"}
            required = {p.get("name") for p in op.get("parameters") or []
                        if p.get("in") == "query" and p.get("required")}
            if not CLIENT_QUERY <= names:
                report.body_problems.append(f"GET {path}: không còn query {sorted(CLIENT_QUERY - names)}")
            extra = required - CLIENT_QUERY
            if extra:
                report.body_problems.append(f"GET {path}: query bắt buộc mới {sorted(extra)}")
            continue
        sent = CLIENT_BODIES.get(path, set())
        content = ((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
        schema = _resolve(spec, content.get("schema"))
        props = set((schema.get("properties") or {}).keys())
        required = set(schema.get("required") or [])
        if not schema:
            report.body_problems.append(f"POST {path}: không đọc được schema của body")
            continue
        if not sent <= props:
            report.body_problems.append(f"POST {path}: server không còn nhận {sorted(sent - props)}")
        if not required <= sent:
            report.body_problems.append(f"POST {path}: trường bắt buộc mới {sorted(required - sent)}")
    if known is not None:
        now, before = _endpoints(spec), _endpoints(known)
        report.new_endpoints = sorted(now - before)
        report.removed_endpoints = sorted(before - now)
    return report


def load_fixture(path: str = FIXTURE) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return None


def fetch_spec(url: str, timeout: float = 10.0, log=None) -> dict:
    http = JsonHttp(url, timeout=timeout, log=log)
    resp = http.call("GET", "/openapi.json")
    if resp.status != 200 or not isinstance(resp.payload, dict):
        raise RuntimeError(f"không đọc được {url}/openapi.json (HTTP {resp.status})")
    return resp.payload


def run(url: str, timeout: float = 10.0, log=None, known_path: str = FIXTURE) -> int:
    """In báo cáo; trả 0 nếu client vẫn khớp server, 1 nếu thiếu hoặc lệch, 2 nếu không đọc được."""
    try:
        spec = fetch_spec(url, timeout=timeout, log=log)
    except RuntimeError as exc:
        print(str(exc))
        return 2
    report = check(spec, load_fixture(known_path))
    print(report.render())
    return 0 if report.ok else 1
