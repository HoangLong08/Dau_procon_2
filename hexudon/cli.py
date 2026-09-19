"""Giao diện dòng lệnh: thi đấu, sandbox, luyện offline, benchmark."""

import argparse
import json
import random
import sys
import time
from typing import List, Optional

from . import rules
from .model import MatchConfig


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _load_config(path: str) -> MatchConfig:
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if "problem" in obj:
        return MatchConfig.from_problem_json(obj)
    return MatchConfig.from_json(obj)


def cmd_play(args) -> int:
    from .client.http_client import HttpTransport
    from .client.session import MatchSession, ReplayWriter

    replay = ReplayWriter(args.replay)
    transport = HttpTransport(args.url, args.match, args.token, log=_log, replay=replay)
    session = MatchSession(
        transport, search_seconds=args.search_seconds, net_margin=args.net_margin,
        role_seconds=args.role_seconds, seed=args.seed, log=_log, replay=replay,
    )
    try:
        result = session.run()
    finally:
        replay.close()
    if result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_sandbox(args) -> int:
    from .client.sandbox_client import SandboxTransport
    from .client.session import MatchSession, ReplayWriter

    replay = ReplayWriter(args.replay)
    transport = SandboxTransport(log=_log, replay=replay)
    session = MatchSession(
        transport, search_seconds=args.search_seconds, net_margin=args.net_margin,
        role_seconds=args.role_seconds, seed=args.seed, log=_log, replay=replay,
    )
    try:
        session.run()
    finally:
        replay.close()
    return 0


def cmd_sim(args) -> int:
    from .offline.bots import BOT_TYPES
    from .offline.runner import run_match

    config = _load_config(args.map) if args.map else None
    if config is None:
        from .offline.mapgen import generate
        config = generate(seed=args.seed, size=args.size)
    if args.players:
        config.players = args.players
    if args.days:
        config.day_steps = config.day_steps[:args.days] or config.day_steps
        config.day_seconds = config.day_seconds[:args.days] or config.day_seconds
        config.__post_init__()

    names = args.bots.split(",")
    bots = []
    for i, name in enumerate(names):
        cls = BOT_TYPES[name.strip()]
        if name.strip() == "search":
            bots.append(cls(config, search_seconds=args.search_seconds,
                            role_seconds=args.role_seconds, seed=args.seed + i))
        else:
            bots.append(cls(config, seed=args.seed + i))
    config.players = max(config.players, len(bots))
    outcome = run_match(config, bots, log=_log)
    print("\nxếp hạng:")
    for rank, rec in enumerate(outcome.ranking(), 1):
        L, D, Q = rec.score()
        print(f"  {rank}. {rec.name:8s} L={L:3d} D={D:4d} Q={Q:4d}  "
              f"bài lỗi={rec.invalid_days} vai trò={rec.kinds}")
    return 0


def cmd_bench(args) -> int:
    from .offline.report import run_benchmark

    run_benchmark(
        maps=args.maps.split(","),
        seeds=range(args.seed, args.seed + args.count),
        search_seconds=args.search_seconds,
        role_seconds=args.role_seconds,
        opponents=args.opponents.split(","),
        out_path=args.out,
        log=_log,
    )
    return 0


def cmd_serve(args) -> int:
    """Server luyện tập tại chỗ: đội tự nối bot vào như server thi đấu thật."""
    import time as _time

    from .offline.bots import BOT_TYPES
    from .offline.local_server import serve

    config = _load_config(args.map) if args.map else None
    if config is None:
        from .offline.mapgen import generate
        config = generate(seed=args.seed, size=args.size)
    if args.day_seconds:
        config.day_seconds = [args.day_seconds] * config.n_days
        config.__post_init__()
    bots = []
    for i, name in enumerate((args.bots or "").split(",")):
        name = name.strip()
        if not name:
            continue
        cls = BOT_TYPES[name]
        bots.append(cls(config, seed=args.seed + i + 1) if name != "search"
                    else cls(config, search_seconds=1.0, role_seconds=3.0, seed=args.seed + i + 1))
    tokens = [f"tok-{i}" for i in range(args.teams)]
    httpd, thread, match, url = serve(config, tokens=tokens, bots=bots,
                                      host=args.host, port=args.port, log=_log)
    print(f"server luyện tập: {url}")
    print(f"map {config.width}x{config.height} | {config.n_agents} agent | {config.n_days} ngày "
          f"| bước {config.day_steps} | {config.seconds_of_day(0):.0f}s mỗi ngày")
    for t in tokens:
        print(f"  nối bot:  python -m hexudon play --url {url} --match m-local --token {t}")
    try:
        while not match.finished:
            match.maybe_advance()
            _time.sleep(0.2)
        print(json.dumps(match.standings(), ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


def cmd_roles(args) -> int:
    from .planner.roles import select_roles

    config = _load_config(args.map)
    kinds, options = select_roles(config, budget_seconds=args.role_seconds, seed=args.seed, log=_log)
    for cand in options:
        mark = "->" if cand.kinds == kinds else "  "
        print(f"{mark} {cand.label:24s} L={cand.score[0]:3d} D={cand.score[1]:4d} Q={cand.score[2]:4d}"
              f"{'' if cand.evaluated else '  (chưa kịp đánh giá)'}")
    print("\nmảng loại xe:", json.dumps(kinds))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hexudon", description="Bot HEXUDON — Procon Việt Nam 2026")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--search-seconds", type=float, default=3.0, help="ngân sách tìm kiếm mỗi ngày")
        p.add_argument("--role-seconds", type=float, default=20.0, help="ngân sách chọn vai trò")
        p.add_argument("--net-margin", type=float, default=1.5, help="lề thời gian chừa cho mạng")
        p.add_argument("--seed", type=int, default=0)

    p = sub.add_parser("play", help="thi đấu qua HTTP")
    p.add_argument("--url", required=True, help="ví dụ http://10.0.0.5:8099")
    p.add_argument("--match", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--replay", default=None, help="đường dẫn ghi replay JSONL")
    common(p)
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("sandbox", help="thi đấu qua stdin/stdout (JSON Lines)")
    p.add_argument("--replay", default=None)
    common(p)
    p.set_defaults(func=cmd_sandbox)

    p = sub.add_parser("sim", help="chạy một trận offline")
    p.add_argument("--map", default=None, help="file JSON cấu hình; bỏ trống thì sinh ngẫu nhiên")
    p.add_argument("--size", type=int, default=16)
    p.add_argument("--players", type=int, default=0)
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--bots", default="search,greedy,sample")
    common(p)
    p.set_defaults(func=cmd_sim)

    p = sub.add_parser("bench", help="benchmark nhiều map và seed")
    p.add_argument("--maps", default="sample_16,sample_24,sample_32,random16,random24")
    p.add_argument("--count", type=int, default=5, help="số seed")
    p.add_argument("--opponents", default="greedy,sample,sample")
    p.add_argument("--out", default=None, help="ghi báo cáo JSON")
    common(p)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("serve", help="mở server luyện tập tại chỗ")
    p.add_argument("--map", default=None)
    p.add_argument("--size", type=int, default=16)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--teams", type=int, default=1, help="số đội nối từ ngoài vào")
    p.add_argument("--bots", default="greedy,sample", help="đối thủ nội bộ")
    p.add_argument("--day-seconds", type=float, default=0, help="ghi đè hạn phản hồi mỗi ngày")
    common(p)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("roles", help="chạy riêng bước chọn vai trò")
    p.add_argument("--map", required=True)
    common(p)
    p.set_defaults(func=cmd_roles)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
