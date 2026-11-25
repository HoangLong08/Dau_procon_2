"""Cầu nối tới bộ giải tour bằng Rust (rust/tour, 25/11/2026).

Bộ giải nhận ma trận số bước giữa ô xuất phát của các xe tuần tra và các spot, rồi giải team
orienteering có phủ chuỗi bằng LNS trong một số mili giây, thứ tự từ điển (chuỗi mới, số chuỗi
trong ngày, số phần). Python chỉ dựng ma trận và đọc tour; giải mã, điểm hẹn xe tiếp xăng và vá
chuỗi vẫn dùng bộ giải mã của planner, nên bài ra luôn hợp lệ theo đúng bộ mô phỏng.

Build một lần: `cargo build --release` trong rust/tour. Không có file nhị phân thì available()
trả False và planner bỏ qua bước này.
"""

import os
import subprocess
import sys
from typing import List, Optional

from .solution import DayContext

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "..", "rust", "tour", "target", "release",
                   "tour.exe" if sys.platform.startswith("win") else "tour")
#: số bước "không tới được" gửi sang Rust (đủ lớn, không tràn i64)
BIG = 10 ** 9


def available() -> bool:
    return os.path.isfile(BIN)


def problem_text(ctx: DayContext, millis: int, seed: int = 1, cap_fuel: bool = False) -> str:
    """Đầu vào cho bộ giải, đúng định dạng ở đầu rust/tour/src/main.rs."""
    table = ctx.dist_step
    P = ctx.n_patrols
    cells = list(ctx.spot_cells)
    N = len(cells)
    spot_at = ctx.config.spot_index_at
    starts = [ctx.agents[ctx.patrols[v]].pos for v in range(P)]
    out: List[str] = [f"{P} {N} {max(1, int(millis))} {int(seed)} {1 if cap_fuel else 0}", str(int(ctx.steps))]
    out.append(" ".join(str(int(max(0, ctx.agents[ctx.patrols[v]].fuel or 0))) for v in range(P)))
    out.append(" ".join(str(spot_at.get(s, -1)) for s in starts))
    brands = sorted(set(ctx.spot_brand))
    index = {b: i for i, b in enumerate(brands)}
    out.append(" ".join(str(index[b]) for b in ctx.spot_brand))
    out.append(" ".join(str(int(max(0, s))) for s in ctx.stocks))
    out.append(" ".join("0" if b in ctx.visited_brands else "1" for b in ctx.spot_brand))

    def cost(a: int, b: int) -> int:
        c = table.cost(a, b)
        return BIG if c is None or c == float("inf") or c >= BIG else int(c)

    for src in starts + cells:
        out.append(" ".join(str(cost(src, dst)) for dst in cells))
    if cap_fuel:
        for src in starts + cells:
            row = []
            for dst in cells:
                if cost(src, dst) >= BIG:
                    row.append(str(BIG))
                else:
                    row.append(str(int(table.steps_fuel(src, dst)[1])))
            out.append(" ".join(row))
    return "\n".join(out) + "\n"


def solve(ctx: DayContext, seconds: float, seed: int = 1, cap_fuel: bool = False) -> Optional[List[List[int]]]:
    """Tour (dãy chỉ số spot) của từng xe tuần tra, hoặc None nếu không chạy được."""
    if not available() or not ctx.patrols or not ctx.spot_cells or seconds <= 0.02:
        return None
    text = problem_text(ctx, int(seconds * 1000), seed=seed, cap_fuel=cap_fuel)
    try:
        res = subprocess.run([BIN], input=text, capture_output=True, text=True, timeout=seconds + 2.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    tours = []
    for line in res.stdout.strip().splitlines()[:ctx.n_patrols]:
        nums = [int(x) for x in line.split()]
        if not nums or nums[0] != len(nums) - 1:
            return None
        tours.append(nums[1:])
    if len(tours) != ctx.n_patrols:
        return None
    return tours
