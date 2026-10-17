"""Giao diện dòng lệnh: thi đấu, sandbox, luyện offline, benchmark."""

import argparse
import json
import random
import signal
import sys
import time
from typing import List, Optional

from . import rules
from .model import MatchConfig
from .client.web_api import DEFAULT_URL
from .procs import install_break_as_interrupt

#: tín hiệu dừng web như Ctrl-C; máy không có tín hiệu nào (SIGHUP trên Windows) thì bỏ qua
EXIT_SIGNALS = ("SIGTERM", "SIGHUP", "SIGBREAK")


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _seconds_or_auto(text: str) -> Optional[float]:
    """'auto' (không phân biệt hoa thường) nghĩa là để server quyết định."""
    return None if text.strip().lower() == "auto" else float(text)


def _load_config(path: str) -> MatchConfig:
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    if "problem" in obj:
        return MatchConfig.from_problem_json(obj)
    return MatchConfig.from_json(obj)


def _trim_days(config: MatchConfig, days: int) -> None:
    """Chỉ giữ `days` ngày đầu, để thử nhanh."""
    config.day_steps = config.day_steps[:days] or config.day_steps
    config.day_seconds = config.day_seconds[:days] or config.day_seconds
    config.__post_init__()


def cmd_play(args) -> int:
    from .client.http_client import HttpTransport
    from .client.session import MatchSession, ReplayWriter

    replay = ReplayWriter(args.replay)
    transport = HttpTransport(
        args.url, args.match, args.token,
        poll_seconds=args.poll_seconds, timeout=args.timeout,
        give_up_seconds=args.give_up_seconds, log=_log, replay=replay,
    )
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
    if args.days:
        _trim_days(config, args.days)

    names = args.bots.split(",")
    bots = []
    for i, name in enumerate(names):
        cls = BOT_TYPES[name.strip()]
        if name.strip() == "search":
            bots.append(cls(config, search_seconds=args.search_seconds,
                            role_seconds=args.role_seconds, seed=args.seed + i))
        else:
            bots.append(cls(config, seed=args.seed + i))
    # lưu lượng chia cho số đội trong trận (cases 3, 8.8); --players lớn hơn số bot
    # giả lập thêm các đội có tên nhưng đứng yên cả trận
    config.players = max(args.players, len(bots)) if args.players else len(bots)
    outcome = run_match(config, bots, log=_log)
    print("\nxếp hạng:")
    for rank, rec in enumerate(outcome.ranking(), 1):
        L, D, Q = rec.score()
        print(f"  {rank}. {rec.name:8s} L={L:3d} D={D:4d} Q={Q:4d}  "
              f"phản hồi={rec.response_seconds:.2f}s bài lỗi={rec.invalid_days} vai trò={rec.kinds}")
    return 0


def cmd_arena(args) -> int:
    """Trận offline n bot, ghi sự kiện suy luận cho web trực quan hoá."""
    from .offline.mapgen import load_or_generate
    from .viz.arena import BotSpec, run_arena
    from .viz.events import EventWriter

    try:
        specs = [BotSpec.parse(text) for text in (args.bot or ["search:1", "greedy", "sample"])]
        config = load_or_generate(args.map, seed=args.seed)
    except (ValueError, OSError) as exc:
        _log(str(exc))
        return 2
    if args.days:
        _trim_days(config, args.days)
    writer = EventWriter(args.events)
    try:
        outcome = run_arena(config, specs, writer.for_bot, seed=args.seed, role_seconds=args.role_seconds)
    except KeyboardInterrupt:
        writer.write(None, "log", {"text": "dừng theo yêu cầu (Ctrl-C)", "level": "warn"})
        return 130
    finally:
        writer.close()
    for rank, rec in enumerate(outcome.ranking(), 1):
        L, D, Q = rec.score()
        _log(f"{rank}. {rec.name}: L={L} D={D} Q={Q}, bài lỗi {rec.invalid_days}")
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
        jobs=args.jobs,
        search_iterations=args.search_iterations,
        role_iterations=args.role_iterations,
    )
    return 0


def cmd_ab(args) -> int:
    """Hai bộ trọng số của bot chơi cùng trận; so từng cặp trận."""
    from .offline.report import parse_weights, run_ab

    try:
        a = parse_weights(args.a)
        b = parse_weights(args.b)
    except ValueError as exc:
        _log(str(exc))
        return 2
    run_ab(
        maps=args.maps.split(","),
        seeds=range(args.seed, args.seed + args.count),
        a=a,
        b=b,
        opponents=[o for o in args.opponents.split(",") if o],
        search_iterations=args.search_iterations,
        role_iterations=args.role_iterations,
        players=args.players,
        jobs=args.jobs,
        out_path=args.out,
        log=_log,
    )
    return 0


def cmd_serve(args) -> int:
    """Server luyện tập tại chỗ: đội tự nối bot vào như server thi đấu thật."""
    if args.web:
        return cmd_serve_web(args)
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


def cmd_games(args) -> int:
    """Liệt kê trận và câu mà đội thấy trên server thi, kèm trạng thái hiện tại."""
    from .client.net import JsonHttp
    from .client.tokens import TokenError, pick_tokens
    from .client.web_api import GameStatus, WebApi
    from .client.web_games import ResolveError, describe_status, fetch_questions, format_table

    try:
        [tok] = pick_tokens(args.env, [args.token_env] if args.token_env else [], args.live)
    except TokenError as exc:
        _log(str(exc))
        return 2
    for warning in tok.warnings:
        _log("CẢNH BÁO: " + warning)
    http = JsonHttp(args.url, timeout=args.timeout, log=_log, secrets=lambda: (tok.value,))
    api = WebApi(http, lambda: tok.value)
    try:
        refs = fetch_questions(api, log=_log)
    except ResolveError as exc:
        _log(str(exc))
        return 1
    statuses = {}
    for ref in refs:
        resp = api.state(ref.game_id)
        if resp.status == 200 and isinstance(resp.payload, dict):
            statuses[ref.game_id] = describe_status(GameStatus.from_json(resp.payload, tok.team_id))
        else:
            statuses[ref.game_id] = f"HTTP {resp.status}"
    print(f"Đội {tok.describe()} — token {tok.var}")
    print(format_table(refs, statuses))
    return 0


def cmd_web(args) -> int:
    """Chơi trên server thi hexudon.hairbui76.id.vn: một câu, nhiều đội, hoặc cả trận."""
    from .client import web_runner
    from .client.tokens import TokenError, pick_tokens

    try:
        tokens = pick_tokens(args.env, args.token_env, args.live)
    except TokenError as exc:
        _log(str(exc))
        return 2
    for tok in tokens:
        for warning in tok.warnings:
            _log("CẢNH BÁO: " + warning)
    if args.events and (args.live or args.follow or len(tokens) > 1):
        _log("--events chỉ dùng khi chơi một câu với một token test, không dùng khi thi thật (--live)")
        return 2
    if args.follow:
        return web_runner.follow(args, tokens, log=_log)
    if len(tokens) > 1:
        return web_runner.play_many(args, tokens, log=_log)
    try:
        return web_runner.play_one(args, tokens[0], log=_log)
    except KeyboardInterrupt:
        _log("dừng theo yêu cầu (Ctrl-C)")
        return 130


def cmd_web_bench(args) -> int:
    """Chạy loạt câu practice trên server thi và so điểm với các đội khác."""
    from .client import web_runner
    from .client.tokens import TokenError, pick_tokens

    try:
        [tok] = pick_tokens(args.env, [args.token_env] if args.token_env else [], live=False)
    except TokenError as exc:
        _log(str(exc))
        return 2
    for warning in tok.warnings:
        _log("CẢNH BÁO: " + warning)
    return web_runner.bench(args, tok, log=_log)


def cmd_serve_web(args) -> int:
    """Server giả nói giao thức web: tập dượt ngày thi trên máy, không cần mạng."""
    import os
    import threading

    from .offline.web_server import Faults, WebWorld, serve_web

    config = _load_config(args.map) if args.map else None
    if config is None:
        from .offline.mapgen import generate
        config = generate(seed=args.seed, size=args.size)
    if args.day_seconds:
        config.day_seconds = [args.day_seconds] * config.n_days
        config.__post_init__()
    faults_on = {f.strip() for f in (args.faults or "").split(",") if f.strip()}
    faults = Faults(rate_limit_every=7 if "429" in faults_on else 0,
                    error_every=11 if "5xx" in faults_on else 0,
                    trickle_seconds=0.02 if "trickle" in faults_on else 0.0)
    world = WebWorld([(77, "DAU 1"), (78, "DAU 2")], faults=faults)
    for i in range(args.questions):
        world.add_question(f"Q{i + 1:02d}", config, 1, "Local Contest",
                           selection_seconds=args.selection_seconds,
                           pre_window=args.pre_window + i * args.stagger,
                           auto_reset=args.auto_reset or None)
    for i in range(2):
        world.add_question(f"Q{i + 1:02d}", config, 2, "Local Practice", practice=True,
                           selection_seconds=args.selection_seconds)
    env_path = os.path.join("experiments", "local.env")

    def write_env() -> dict:
        tokens = {team_id: world.issue_token(team_id) for team_id in (77, 78)}
        os.makedirs("experiments", exist_ok=True)
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write(f"TOKEN={tokens[77]}\nTEST_DAU_01={tokens[77]}\nTEST_DAU_02={tokens[78]}\n")
        return tokens

    try:
        httpd, _, url = serve_web(world, host=args.host, port=args.port)
    except OSError as exc:
        # phải mở cổng TRƯỚC khi ghi token: ghi trước từng đè token của server giả
        # đang chạy trên cổng này, làm bot của nó bị 401 giữa trận
        _log(f"không mở được cổng {args.port} ({exc}); có server giả khác đang chạy? "
             f"giữ nguyên {env_path}")
        return 2
    first_tokens = write_env()
    print(f"server giả (giao thức web): {url} — token ở {env_path}")
    print(f"map {config.width}x{config.height} | {config.n_agents} xe | {config.n_days} ngày "
          f"| {config.seconds_of_day(0):.0f}s mỗi ngày | trận 1 contest ({args.questions} câu), trận 2 practice")
    print(f"  python -m hexudon games --url {url} --env {env_path}")
    print(f"  python -m hexudon web --url {url} --env {env_path} --match 1 --follow "
          f"--token-env TEST_DAU_01 --token-env TEST_DAU_02")
    print(f"  python -m hexudon web-bench --url {url} --env {env_path} --match 2")
    sys.stdout.flush()      # ghi ra file thì stdout bị đệm; lệnh mẫu phải hiện ngay
    if "401" in faults_on:
        def expire_later():
            time.sleep(args.revoke_after)
            for token in first_tokens.values():
                world.revoke(token)
            write_env()
            _log(f"đã thu hồi token cũ và ghi token mới vào {env_path}: bot phải tự đọc lại và chơi tiếp")
        threading.Thread(target=expire_later, daemon=True).start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


def _raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _install_exit_signals() -> None:
    """SIGTERM (kill mặc định), SIGHUP (đóng terminal) và SIGBREAK (Ctrl-Break trên
    Windows) cũng phải dừng web như Ctrl-C, để nhánh `finally: app.shutdown()` chạy
    và bot web không bị bỏ mồ côi."""
    for name in EXIT_SIGNALS:
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _raise_keyboard_interrupt)


def cmd_viz(args) -> int:
    """Web trực quan hoá trên 127.0.0.1: tạo trận, xem bot suy luận, tua lại."""
    import webbrowser

    from .viz.server import VizApp, serve_viz

    app = VizApp(log=_log)
    try:
        httpd, url = serve_viz(port=args.port, app=app)
    except OSError as exc:
        _log(f"không mở được cổng {args.port}: {exc}")
        return 2
    _install_exit_signals()
    _log(f"web trực quan hoá: {url} — Ctrl-C hoặc đóng terminal để tắt; các bot web đang chạy sẽ dừng theo")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        httpd.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hexudon", description="Bot HEXUDON — Procon Việt Nam 2026")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--search-seconds", type=float, default=3.0, help="ngân sách tìm kiếm mỗi ngày")
        p.add_argument("--role-seconds", type=float, default=3.0,
                       help="ngân sách chọn vai trò (tự rút lại theo startsAt)")
        p.add_argument("--net-margin", type=float, default=1.5, help="lề thời gian chừa cho mạng")
        p.add_argument("--seed", type=int, default=0)

    def web_common(p):
        p.add_argument("--url", default=DEFAULT_URL, help="server thi (mặc định %(default)s)")
        p.add_argument("--env", default=".env", help="file chứa token (mặc định .env)")
        p.add_argument("--live", action="store_true", help="THI THẬT: dùng TOKEN trong .env")
        p.add_argument("--timeout", type=float, default=5.0,
                       help="hạn cứng mỗi cuộc gọi HTTP, tính cả lúc server trả nhỏ giọt")

    def web_timing(p):
        p.add_argument("--search-seconds", type=_seconds_or_auto, default=None,
                       help="ngân sách tìm kiếm mỗi ngày; mặc định auto = theo hạn và độ dài ngày của server")
        p.add_argument("--role-seconds", type=_seconds_or_auto, default=None,
                       help="ngân sách chọn vai trò; mặc định auto = theo cửa sổ chọn xe của server")
        p.add_argument("--net-margin", type=float, default=2.0, help="lề thời gian chừa cho mạng")
        p.add_argument("--poll-seconds", type=float, default=0.5, help="nhịp hỏi /state")
        p.add_argument("--give-up-seconds", type=float, default=120.0,
                       help="mất liên lạc quá lâu thì thoát thay vì quay vô hạn")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--out-dir", default="replays", help="thư mục ghi replay và tổng kết")

    p = sub.add_parser("play", help="thi đấu qua HTTP")
    p.add_argument("--url", required=True, help="ví dụ http://10.0.0.5:8099")
    p.add_argument("--match", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--replay", default=None, help="đường dẫn ghi replay JSONL")
    p.add_argument("--timeout", type=float, default=2.0,
                   help="hạn cứng mỗi cuộc gọi HTTP, tính cả lúc server trả nhỏ giọt")
    p.add_argument("--poll-seconds", type=float, default=0.25,
                   help="nhịp hỏi /state; dưới 0.2 s server có thể trả HTTP 429")
    p.add_argument("--give-up-seconds", type=float, default=120.0,
                   help="mất liên lạc quá lâu thì thoát thay vì quay vô hạn")
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

    p = sub.add_parser("arena", help="trận offline n bot, ghi sự kiện cho web trực quan hoá")
    p.add_argument("--map", default="sample_16",
                   help="sample_16 | sample_24 | sample_32 | random<cỡ> | đường dẫn JSON")
    p.add_argument("--bot", action="append", default=[],
                   help="loại[:giây], ví dụ search:3, greedy, sample, wait; lặp lại cho mỗi bot")
    p.add_argument("--days", type=int, default=0, help="chỉ chơi N ngày đầu (0 = đủ)")
    p.add_argument("--role-seconds", type=float, default=3.0, help="ngân sách chọn vai trò của bot search")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--events", required=True, help="file JSONL ghi sự kiện")
    p.set_defaults(func=cmd_arena)

    def iteration_budget(p, search_default, role_default):
        p.add_argument("--search-iterations", type=int, default=search_default,
                       help="ngân sách tìm kiếm theo SỐ VÒNG ALNS mỗi ngày thay vì giây: tất định, "
                            "chạy song song không làm bot yếu đi")
        p.add_argument("--role-iterations", type=int, default=role_default,
                       help="chọn vai trò tất định: vòng 2 chạy ALNS ngần ấy vòng mỗi ngày")
        p.add_argument("--jobs", type=int, default=1,
                       help="số trận chạy song song (mỗi trận một lõi); nên đi cùng --search-iterations")

    p = sub.add_parser("bench", help="benchmark nhiều map và seed")
    p.add_argument("--maps", default="sample_16,sample_24,sample_32,random16,random24")
    p.add_argument("--count", type=int, default=5, help="số seed")
    p.add_argument("--opponents", default="greedy,sample,sample")
    p.add_argument("--out", default=None, help="ghi báo cáo JSON")
    common(p)
    iteration_budget(p, None, None)
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("ab", help="so hai bộ trọng số của bot trong cùng trận nhiều đội")
    p.add_argument("--a", default="", help="trọng số của A, ví dụ fuel_bonus=0.2 (trống = mặc định)")
    p.add_argument("--b", default="", help="trọng số của B, ví dụ fuel_bonus=2,near_chain=0.4")
    p.add_argument("--maps", default="sample_16,sample_24,sample_32,random16,random20,random24,random28,random32",
                   help="tên map (sample_*, random<cỡ>) hoặc đường dẫn JSON, cách nhau bằng dấu phẩy")
    p.add_argument("--count", type=int, default=6, help="số seed mỗi map")
    p.add_argument("--seed", type=int, default=0, help="seed đầu tiên")
    p.add_argument("--opponents", default="search,search,greedy,sample",
                   help="đối thủ cùng trận: search (bot ta, trọng số mặc định), greedy, sample, wait")
    p.add_argument("--players", type=int, default=0,
                   help="mẫu số khi tính lưu lượng; mặc định bằng số đội trong trận")
    p.add_argument("--out", default=None, help="ghi báo cáo JSON")
    iteration_budget(p, 1000, 100)
    p.set_defaults(func=cmd_ab)

    p = sub.add_parser("serve", help="mở server luyện tập tại chỗ")
    p.add_argument("--map", default=None)
    p.add_argument("--size", type=int, default=16)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--teams", type=int, default=1, help="số đội nối từ ngoài vào")
    p.add_argument("--bots", default="greedy,sample", help="đối thủ nội bộ")
    p.add_argument("--day-seconds", type=float, default=0, help="ghi đè hạn phản hồi mỗi ngày")
    p.add_argument("--web", action="store_true", help="nói giao thức của server thi hexudon.hairbui76.id.vn")
    p.add_argument("--questions", type=int, default=2, help="--web: số câu contest")
    p.add_argument("--pre-window", type=float, default=20.0, help="--web: số giây trước khi cửa sổ chọn xe mở")
    p.add_argument("--stagger", type=float, default=45.0, help="--web: các câu contest mở cách nhau ngần này giây")
    p.add_argument("--selection-seconds", type=float, default=30.0, help="--web: độ dài cửa sổ chọn xe")
    p.add_argument("--auto-reset", type=float, default=0.0,
                   help="--web: câu contest tự chạy lại sau ngần này giây (0 = không)")
    p.add_argument("--faults", default="", help="--web: bật lỗi, ví dụ 429,5xx,401,trickle")
    p.add_argument("--revoke-after", type=float, default=60.0,
                   help="--web --faults 401: thu hồi token sau ngần này giây")
    common(p)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("roles", help="chạy riêng bước chọn vai trò")
    p.add_argument("--map", required=True)
    common(p)
    p.set_defaults(func=cmd_roles)

    p = sub.add_parser("games", help="liệt kê trận và câu trên server thi")
    web_common(p)
    p.add_argument("--token-env", default=None, help="tên biến token trong .env (mặc định TEST_DAU_01)")
    p.set_defaults(func=cmd_games)

    p = sub.add_parser("web", help="chơi trên server thi hexudon.hairbui76.id.vn")
    web_common(p)
    web_timing(p)
    p.add_argument("--token-env", action="append", default=[],
                   help="tên biến token trong .env (mặc định TEST_DAU_01); lặp lại để chạy nhiều đội")
    p.add_argument("--follow", action="store_true",
                   help="theo dõi cả trận: mỗi câu mở ra là có một bot cho mỗi đội")
    p.add_argument("--game", default=None, help="ID câu: chơi thẳng, không cần danh sách trận")
    p.add_argument("--practice", action="store_true", help="dùng cùng --game khi đó là câu practice")
    p.add_argument("--match", default=None, help="ID trận (số) hoặc tên trận")
    p.add_argument("--question", default=None, help="tên câu (Q01) hoặc ID câu")
    p.add_argument("--keep", action="store_true",
                   help="practice: không reset sau khi chơi, để xem trên website")
    p.add_argument("--events", default=None,
                   help="ghi sự kiện suy luận ra file JSONL (web trực quan hoá); chỉ khi chơi một câu, một token")
    p.add_argument("--summary", default=None, help=argparse.SUPPRESS)
    p.add_argument("--label", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("web-bench", help="chạy loạt câu practice trên server thi, so điểm với đội khác")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--env", default=".env")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--token-env", default=None, help="tên biến token trong .env (mặc định TEST_DAU_01)")
    p.add_argument("--match", required=True, help="ID hoặc tên trận practice")
    p.add_argument("--questions", default="all", help="all hoặc danh sách như Q01,Q03")
    p.add_argument("--jobs", type=int, default=1, help="số câu chạy song song (nên ≤ số lõi CPU − 1)")
    p.add_argument("--keep", action="store_true", help="không reset practice sau khi chơi")
    p.add_argument("--out", default=None, help="file JSON báo cáo (mặc định experiments/web-bench-<giờ>.json)")
    web_timing(p)
    p.set_defaults(func=cmd_web_bench, live=False)

    p = sub.add_parser("viz", help="web trực quan hoá: tạo trận, xem bot suy luận, tua lại")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="mở trình duyệt")
    p.set_defaults(func=cmd_viz)

    return parser


def _utf8_streams() -> None:
    """In tiếng Việt không vỡ khi stdout/stderr bị chuyển hướng ra file hay pipe.

    Trên Windows, stream bị chuyển hướng dùng bảng mã ANSI (cp1252) nên print tiếng
    Việt ném UnicodeEncodeError; console hay chế độ PYTHONUTF8 thì đã là UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if reconfigure is not None and encoding != "utf8":
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_streams()
    # Windows: nút Dừng của web gửi Ctrl-Break cho bot con; coi nó như Ctrl-C
    install_break_as_interrupt()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
