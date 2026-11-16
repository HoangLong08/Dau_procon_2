"""R7 (17/11/2026): nhiều seed ALNS mỗi ngày, trên nhiều tiến trình.

Một ngày thật dài tới 28–45 giây mà bot chỉ dùng một lõi. Ở đây mỗi seed phụ chạy
trọn `engine.plan_day` (kể cả rollout R4 và pha Q) trên một tiến trình riêng, rồi
cộng (chuỗi mới, D, Q) của hôm nay với phần chơi tiếp các ngày sau của bài cuối.
Strategist so tổng đó với bài của chính nó và gửi bài tốt nhất ở lượt cuối.

  - `DayJob`: đầu vào của một ngày, picklable (config gửi một lần lúc mở Pool);
  - `run_job`: chạy một seed, trả (bài, tổng); dùng cả trong tiến trình con lẫn khi
    chạy tuần tự theo số vòng (giải đấu, test: tất định);
  - `SeedPool`: Pool spawn giữ suốt trận, vì mở tiến trình trên Windows mất 1–2 s.
    Config đi kèm từng việc, không qua `initializer`: trên Windows, mở một tiến trình
    con ghi đối tượng tiến trình vào ống dẫn, và nếu con chết ngay lúc khởi động
    (không nạp được module chính) thì lệnh ghi một gói lớn bị chặn mãi (đo 17/11).
    Gói nhỏ thì không. Strategist còn mở Pool trong luồng nền và chỉ dùng sau khi
    `ping` trả lời, nên không bao giờ treo vì tiến trình con.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..model import AgentState, MatchConfig
from ..traffic import TrafficForecast
from .decoder import Decoded
from .engine import make_context, plan_day
from .objective import lexicographic_gain
from .solution import Weights


@dataclass
class DayJob:
    day: int
    kinds: List[int]
    agents: List[AgentState]
    status: Dict[int, int]
    visited: Set[int]
    weights: Weights
    forecast_status: Optional[Dict[int, int]]
    steps_override: Optional[int]
    seed: int
    #: theo giây: hạn của plan_day theo time.time() (đổi sang monotonic trong tiến trình con)
    wall_deadline: Optional[float] = None
    #: theo vòng: số vòng ALNS (chạy tuần tự, tất định)
    max_iterations: int = 1_000_000
    #: dữ liệu của hàm chơi tiếp (R4); forecast None nghĩa là không chơi tiếp
    forecast: Optional[TrafficForecast] = None
    status_yesterday: Optional[Dict[int, int]] = None
    stay_yesterday: Optional[Dict[int, int]] = None
    horizon: Optional[int] = None
    rollout_seed: int = 0
    extra: dict = field(default_factory=dict)


def make_lookahead(config: MatchConfig, job: DayJob):
    """Hàm chơi tiếp các ngày sau (như MatchStrategist._lookahead), hoặc None."""
    if job.forecast is None or job.weights.rollout_k < 1 or job.day + 1 >= config.n_days:
        return None
    from .lookahead import future_gain

    kinds, forecast, visited = list(job.kinds), job.forecast, set(job.visited)
    status_today, status_yesterday, stay_yesterday = dict(job.status), job.status_yesterday, job.stay_yesterday
    weights, seed, day, horizon = job.weights, job.rollout_seed, job.day, job.horizon

    def look(decoded: Decoded):
        return future_gain(config, kinds, forecast, day, decoded.result, visited, status_today,
                           status_yesterday, stay_yesterday, weights=weights, seed=seed, max_days=horizon)

    return look


def total_of(ctx, decoded: Decoded, lookahead) -> Tuple[int, int, int]:
    """(chuỗi mới, D, Q) của hôm nay cộng phần chơi tiếp các ngày sau (nếu có).

    Dùng lại tổng plan_day đã tính cho đúng bài này (Decoded.total) nếu có."""
    if decoded.total is not None:
        return tuple(decoded.total)
    today = lexicographic_gain(ctx, decoded.result)
    if lookahead is None or not decoded.result.ok:
        return today
    return tuple(a + b for a, b in zip(today, lookahead(decoded)))


def run_job(config: MatchConfig, job: DayJob, reserve: float = 0.0) -> Tuple[Decoded, Tuple[int, int, int]]:
    """Chạy một seed của ngày: (bài cuối, tổng hôm nay + các ngày sau).

    `reserve`: giây chừa cho lần chơi tiếp chấm bài cuối, trừ vào hạn của plan_day.
    """
    ctx = make_context(config, job.day, job.agents, job.status, [s.stocks for s in config.spots],
                       job.visited, weights=job.weights, forecast_status=job.forecast_status,
                       steps_override=job.steps_override)
    deadline = None
    if job.wall_deadline is not None:
        deadline = time.monotonic() + (job.wall_deadline - time.time()) - reserve
    look = make_lookahead(config, job)
    decoded = plan_day(ctx, deadline=deadline, seed=job.seed, max_iterations=job.max_iterations,
                       lookahead=look)
    return decoded, total_of(ctx, decoded, look)


def _worker(args):
    config, job, reserve = args
    return run_job(config, job, reserve)


def _ping(_):
    return True


class SeedPool:
    """Pool tiến trình (spawn) cho các seed phụ, giữ suốt trận. Hỏng thì `ok` = False."""

    def __init__(self, workers: int):
        import multiprocessing

        self.workers = max(1, int(workers))
        self.pool = multiprocessing.get_context("spawn").Pool(self.workers)
        self.ok = True

    def ping(self, timeout: float) -> bool:
        """Mọi tiến trình con đã nạp xong và trả lời được trong `timeout` giây chưa."""
        try:
            return all(self.pool.map_async(_ping, range(self.workers), chunksize=1).get(timeout=timeout))
        except Exception:
            return False

    def start(self, config: MatchConfig, jobs: Sequence[DayJob], reserve: float):
        """Giao việc cho các tiến trình con, không chờ; None nếu Pool hỏng."""
        if not self.ok:
            return None
        try:
            return [self.pool.apply_async(_worker, ((config, job, reserve),)) for job in jobs]
        except Exception:
            self.ok = False
            return None

    @staticmethod
    def collect(handles, until: float) -> List[Optional[tuple]]:
        """Kết quả theo thứ tự; chờ tới `until` (monotonic); việc lỡ hạn hoặc lỗi là None."""
        out = []
        for res in handles or []:
            try:
                out.append(res.get(timeout=max(0.01, until - time.monotonic())))
            except Exception:
                out.append(None)
        return out

    def close(self) -> None:
        try:
            self.pool.terminate()
        except Exception:
            pass
        self.ok = False
