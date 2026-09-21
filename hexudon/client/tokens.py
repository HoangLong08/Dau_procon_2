"""Chọn token theo TÊN biến trong .env và chặn dùng nhầm token thi thật.

Phân biệt theo tên chứ không theo giá trị: TEST_DAU_01 trùng từng byte với
TOKEN (cùng đội DAU 1), nên so giá trị không cho biết người dùng định thi thật
hay đang thử. Token không bao giờ được in ra; thông báo lỗi chỉ nêu tên biến.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .net import jwt_claims, load_env

LIVE_VAR = "TOKEN"
DEFAULT_TEST_VAR = "TEST_DAU_02"
EXPIRY_WARN_SECONDS = 2 * 3600


class TokenError(Exception):
    """Không được chạy với token này; thông điệp in thẳng cho người dùng."""


@dataclass
class TeamToken:
    var: str
    value: str
    env_path: str
    team_id: Optional[int] = None
    team_name: str = "?"
    expires_at: Optional[float] = None
    warnings: List[str] = field(default_factory=list)

    def reload(self) -> bool:
        """Đọc lại .env; True nếu biến đã có giá trị mới (người dùng vừa dán token mới)."""
        value = load_env(self.env_path).get(self.var)
        if not value or value == self.value:
            return False
        self.value = value
        self.expires_at = jwt_claims(value).get("exp", self.expires_at)
        return True

    def describe(self) -> str:
        return f"{self.team_name} (id {self.team_id})"


def _fmt(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def pick_tokens(env_path: str, token_vars: Sequence[str], live: bool,
                now: Optional[float] = None) -> List[TeamToken]:
    """Token cho lần chạy này; ném TokenError nếu vi phạm quy tắc an toàn."""
    now = time.time() if now is None else now
    if live and token_vars:
        raise TokenError("--live luôn dùng TOKEN; bỏ --token-env đi (hai cờ mâu thuẫn nhau)")
    names = [LIVE_VAR] if live else list(dict.fromkeys(token_vars or [DEFAULT_TEST_VAR]))
    if not live and LIVE_VAR in names:
        raise TokenError("TOKEN dành cho thi thật — thêm --live nếu đúng là đang thi")
    env = load_env(env_path)
    picked = []
    for name in names:
        value = env.get(name)
        if not value:
            raise TokenError(f"không thấy {name} trong {env_path}; các biến đang có: "
                             f"{', '.join(sorted(env)) or '(trống)'}")
        claims = jwt_claims(value)
        tok = TeamToken(var=name, value=value, env_path=env_path, team_id=claims.get("id"),
                        team_name=str(claims.get("name", "?")), expires_at=claims.get("exp"))
        if tok.expires_at is not None:
            if tok.expires_at <= now:
                raise TokenError(f"{name} của {tok.describe()} đã hết hạn lúc {_fmt(tok.expires_at)} — "
                                 f"lấy token mới trên website (icon 🔑) rồi cập nhật {env_path}")
            if tok.expires_at - now < EXPIRY_WARN_SECONDS:
                tok.warnings.append(f"{name} của {tok.describe()} hết hạn lúc {_fmt(tok.expires_at)}, "
                                    "chưa tới 2 giờ nữa — lấy token mới trước khi thi")
        if not live and value == env.get(LIVE_VAR):
            tok.warnings.append(f"{name} trùng TOKEN thi thật: đang chơi dưới tên {tok.describe()}")
        picked.append(tok)
    return picked
