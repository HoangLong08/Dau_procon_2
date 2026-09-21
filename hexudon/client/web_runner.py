"""Chạy bot trên server thi hexudon.hairbui76.id.vn.

play_one chơi trọn một câu trong tiến trình hiện tại; phần điều phối nhiều tiến
trình (--follow, nhiều đội, web-bench) nằm ở cuối file.
"""

import os
import time
from typing import Callable, List, Tuple

from .net import JsonHttp
from .session import MatchSession, ReplayWriter
from .tokens import TeamToken
from .web_api import FINISHED, GameStatus, WebApi
from .web_games import CONTEST, PRACTICE, GameRef, ResolveError, describe_status, format_table, resolve
from .web_transport import WebTransport

Log = Callable[[str], None]


def slug(text: str) -> str:
    """Tên file an toàn từ nhãn trận/câu."""
    cleaned = "".join(c if c.isalnum() else "-" for c in text)
    return "-".join(part for part in cleaned.split("-") if part) or "x"


def make_api(url: str, tok: TeamToken, timeout: float, log: Log, replay=None) -> WebApi:
    http = JsonHttp(url, timeout=timeout, log=log, replay=replay, secrets=lambda: (tok.value,))
    return WebApi(http, lambda: tok.value)


def run_paths(args, ref: GameRef, tok: TeamToken) -> Tuple[str, str]:
    """(replay JSONL, file tổng kết) cho một lần chơi; tiến trình con nhận sẵn qua --summary."""
    if args.summary:
        base = args.summary[:-len("-summary.json")] if args.summary.endswith("-summary.json") \
            else args.summary
    else:
        kind = "live" if args.live else ref.mode
        name = f"m{ref.match_id}-{ref.question}" if ref.match_id is not None \
            else (ref.question or ref.game_id[:8])
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(args.out_dir, f"{kind}-{slug(name)}-t{tok.team_id}-{stamp}")
    return base + ".jsonl", base + "-summary.json"


def pick_one(api: WebApi, refs: List[GameRef], team_id) -> GameRef:
    """--match không kèm --question: chỉ chơi khi trận còn đúng một câu contest chưa xong."""
    if len(refs) == 1:
        return refs[0]
    if any(r.mode == PRACTICE for r in refs):
        raise ResolveError("trận practice cần --question (hoặc chạy cả trận bằng web-bench):\n"
                           + format_table(refs))
    open_refs, statuses = [], {}
    for ref in refs:
        resp = api.state(ref.game_id)
        if resp.status == 200 and isinstance(resp.payload, dict):
            st = GameStatus.from_json(resp.payload, team_id)
            statuses[ref.game_id] = describe_status(st)
            if st.status != FINISHED:
                open_refs.append(ref)
    if len(open_refs) == 1:
        return open_refs[0]
    raise ResolveError(f"trận có {len(open_refs)} câu chưa kết thúc; chọn --question hoặc dùng --follow:\n"
                       + format_table(refs, statuses))


def banner(tok: TeamToken, ref: GameRef, log: Log) -> None:
    expires = time.strftime("%H:%M %d/%m", time.localtime(tok.expires_at)) if tok.expires_at else "?"
    log("=" * 64)
    log(f"THI THẬT — đội {tok.describe()} — {ref.label()} — token hết hạn {expires}")
    log("=" * 64)


def play_one(args, tok: TeamToken, log: Log) -> int:
    """Chơi trọn một câu trong tiến trình này; trả mã thoát."""
    lookup = make_api(args.url, tok, args.timeout, log)
    try:
        refs = resolve(lookup, match=args.match, question=args.question, game=args.game,
                       practice=args.practice, label=args.label, log=log)
        ref = pick_one(lookup, refs, tok.team_id)
    except ResolveError as exc:
        log(str(exc))
        return 1
    if args.live and ref.mode != CONTEST:
        log(f"--live chỉ chơi trận contest; {ref.label()} là {ref.mode}")
        return 2
    if args.live:
        banner(tok, ref, log)
    replay_path, summary_path = run_paths(args, ref, tok)
    replay = ReplayWriter(replay_path)
    try:
        api = make_api(args.url, tok, args.timeout, log, replay)
        transport = WebTransport(api, ref, tok.team_id, keep=args.keep, live=args.live,
                                 poll_seconds=args.poll_seconds, give_up_seconds=args.give_up_seconds,
                                 token_reload=tok.reload, token_var=tok.var, log=log, replay=replay)
        session = MatchSession(transport, search_seconds=args.search_seconds,
                               net_margin=args.net_margin, role_seconds=args.role_seconds,
                               seed=args.seed, log=log, replay=replay)
        session.run()
        predicted = list(session.strategist.score_tuple()) if session.strategist else None
        summary = transport.finish(summary_path, extra={
            "team_name": tok.team_name, "token_var": tok.var,
            "predicted": predicted, "roles": transport.kinds})
    finally:
        replay.close()
    score = summary.get("score")
    if score:
        log(f"KẾT QUẢ {ref.label()}: L={score['L']} D={score['D']} Q={score['Q']}, "
            f"hạng {score['rank']}/{score['teams']} — tổng kết ở {summary_path}")
    else:
        log(f"xong {ref.label()}; server chưa trả điểm — tổng kết ở {summary_path}")
    return 0
