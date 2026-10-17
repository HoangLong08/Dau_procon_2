"""Chạy bot trên server thi hexudon.hairbui76.id.vn.

play_one chơi trọn một câu trong tiến trình hiện tại; phần điều phối nhiều tiến
trình (--follow, nhiều đội, web-bench) nằm ở cuối file.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..procs import child_env, detached_kwargs
from .net import JsonHttp
from .session import MatchSession, ReplayWriter
from .tokens import TeamToken
from .web_api import FINISHED, IN_PROGRESS, SELECTING, GameStatus, WebApi, error_detail
from .web_games import CONTEST, PRACTICE, GameRef, ResolveError, describe_status, fetch_questions, format_table, resolve
from .web_transport import WebTransport, score_of

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
    events_path = getattr(args, "events", None)
    writer = None
    emit = None
    if events_path:
        from ..viz.events import EventWriter
        writer = EventWriter(events_path)
        emit = writer.for_bot(tok.var)
    try:
        api = make_api(args.url, tok, args.timeout, log, replay)
        transport = WebTransport(api, ref, tok.team_id, keep=args.keep, live=args.live,
                                 poll_seconds=args.poll_seconds, give_up_seconds=args.give_up_seconds,
                                 token_reload=tok.reload, token_var=tok.var, log=log, replay=replay)
        session = MatchSession(transport, search_seconds=args.search_seconds,
                               net_margin=args.net_margin, role_seconds=args.role_seconds,
                               seed=args.seed, log=log, replay=replay, emit=emit)
        try:
            session.run()
        except KeyboardInterrupt:
            if emit is not None:
                emit("log", {"text": "dừng theo yêu cầu (Ctrl-C)", "level": "warn"})
            raise
        predicted = list(session.strategist.score_tuple()) if session.strategist else None
        summary = transport.finish(summary_path, extra={
            "team_name": tok.team_name, "token_var": tok.var,
            "predicted": predicted, "roles": transport.kinds})
        if emit is not None:
            emit("match_end", {"score": summary.get("score"), "replay_check": summary.get("replay_check"),
                               "predicted": predicted})
    finally:
        replay.close()
        if writer is not None:
            writer.close()
    score = summary.get("score")
    if score:
        log(f"KẾT QUẢ {ref.label()}: L={score['L']} D={score['D']} Q={score['Q']}, "
            f"hạng {score['rank']}/{score['teams']} — tổng kết ở {summary_path}")
    else:
        log(f"xong {ref.label()}; server chưa trả điểm — tổng kết ở {summary_path}")
    return 0


# ---- nhiều tiến trình: --follow, nhiều đội, web-bench ----

#: gốc repo, để tiến trình con `python -m hexudon` luôn tìm thấy package
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: con thoát lỗi liên tiếp ngần này lần thì thôi mở lại cho (câu, đội) đó
MAX_CHILD_FAILURES = 3
#: trận chưa hiện thì báo lại tối đa mỗi ngần này giây, để log không bị ngập
WAIT_LOG_SECONDS = 60.0


def _auto(value: Optional[float]) -> str:
    return "auto" if value is None else repr(float(value))


def child_command(args, ref: GameRef, token_var: str, summary_path: str, label: str) -> List[str]:
    """Dòng lệnh cho một tiến trình con chơi đúng một câu với đúng một token."""
    cmd = [sys.executable, "-m", "hexudon", "web", "--game", ref.game_id, "--label", label,
           "--summary", summary_path, "--url", args.url, "--env", os.path.abspath(args.env),
           "--timeout", repr(float(args.timeout)), "--net-margin", repr(float(args.net_margin)),
           "--poll-seconds", repr(float(args.poll_seconds)),
           "--give-up-seconds", repr(float(args.give_up_seconds)), "--seed", str(args.seed),
           "--search-seconds", _auto(args.search_seconds), "--role-seconds", _auto(args.role_seconds)]
    if ref.mode == PRACTICE:
        cmd.append("--practice")
    if getattr(args, "keep", False):
        cmd.append("--keep")
    cmd += ["--live"] if args.live else ["--token-env", token_var]
    return cmd


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def result_line(summary: dict) -> str:
    score = summary.get("score")
    text = (f"L={score['L']} D={score['D']} Q={score['Q']} hạng {score['rank']}/{score['teams']}"
            if score else "không có điểm")
    mismatch = (summary.get("replay_check") or {}).get("days_with_mismatch")
    checked = "chưa đối chiếu" if mismatch is None else f"lệch simulator {len(mismatch)} ngày"
    return (f"xong {summary.get('label', '?')} ({summary.get('token_var', '?')}): {text}, {checked}, "
            f"mã thoát {summary.get('exit_code')}")


@dataclass
class Child:
    ref: GameRef
    token_var: str
    proc: subprocess.Popen
    summary_path: str
    log_path: str


class Supervisor:
    """Giữ các tiến trình con. Ctrl-C lần 1 thôi mở con mới; lần 2 dừng hết.

    Con mở trong nhóm tiến trình riêng (start_new_session trên POSIX,
    CREATE_NEW_PROCESS_GROUP trên Windows) để Ctrl-C trên terminal chỉ tới tiến
    trình cha: một lần lỡ tay không được giết trận đang thi.
    """

    def __init__(self, args, log: Log):
        self.args = args
        self.log = log
        self.out_dir = os.path.abspath(args.out_dir)
        self.children: Dict[Tuple[str, str], Child] = {}
        self.failures: Dict[Tuple[str, str], int] = {}
        self.done: List[dict] = []
        self.stopping = False

    def running(self, ref: GameRef, token_var: str) -> bool:
        return (ref.game_id, token_var) in self.children

    def blocked(self, ref: GameRef, token_var: str) -> bool:
        return self.failures.get((ref.game_id, token_var), 0) >= MAX_CHILD_FAILURES

    def spawn(self, ref: GameRef, token_var: str) -> Child:
        label = f"{ref.match_name}/{ref.question}" if ref.question else ref.game_id[:8]
        kind = "live" if self.args.live else ref.mode
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = os.path.join(self.out_dir, f"{kind}-{slug(label)}-{slug(token_var)}-{stamp}")
        os.makedirs(self.out_dir, exist_ok=True)
        cmd = child_command(self.args, ref, token_var, base + "-summary.json", label)
        with open(base + ".log", "a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, cwd=PROJECT_ROOT,
                                    env=child_env(PROJECT_ROOT), **detached_kwargs())
        child = Child(ref, token_var, proc, base + "-summary.json", base + ".log")
        self.children[(ref.game_id, token_var)] = child
        self.log(f"bắt đầu {label} ({token_var}) — log: {child.log_path}")
        return child

    def reap(self) -> List[dict]:
        finished = []
        for key, child in list(self.children.items()):
            code = child.proc.poll()
            if code is None:
                continue
            del self.children[key]
            self.failures[key] = 0 if code == 0 else self.failures.get(key, 0) + 1
            summary = _read_json(child.summary_path) or {"label": child.ref.label()}
            summary["exit_code"] = code
            summary["token_var"] = child.token_var
            summary.setdefault("game_id", child.ref.game_id)
            self.done.append(summary)
            finished.append(summary)
            self.log(result_line(summary))
            if self.failures[key] == MAX_CHILD_FAILURES:
                self.log(f"{child.ref.label()} ({child.token_var}) lỗi {MAX_CHILD_FAILURES} lần liền — "
                         f"thôi mở lại; xem log {child.log_path}")
        return finished

    def terminate_all(self, grace: float = 5.0) -> None:
        for child in self.children.values():
            if child.proc.poll() is None:
                child.proc.terminate()
        deadline = time.monotonic() + grace
        for child in self.children.values():
            try:
                child.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.proc.kill()
        self.reap()


class _CtrlC:
    """Ctrl-C lần 1: thôi mở tiến trình mới, các bot đang chơi chơi nốt; lần 2: dừng hết."""

    def __init__(self, sup: Supervisor, log: Log):
        self.sup = sup
        self.log = log
        self.presses = 0

    def __call__(self, signum, frame):
        self.presses += 1
        if self.presses == 1:
            self.sup.stopping = True
            self.log("Ctrl-C: thôi bắt câu mới; các bot đang chơi vẫn chơi nốt. "
                     "Nhấn Ctrl-C lần nữa để dừng hết.")
            return
        raise KeyboardInterrupt


@contextlib.contextmanager
def _ctrl_c(sup: Supervisor, log: Log):
    handler = _CtrlC(sup, log)
    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:            # không ở luồng chính: Ctrl-C giữ hành vi mặc định
        previous = None
    try:
        yield handler
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def question_status(api: WebApi, ref: GameRef, tokens: Sequence[TeamToken], log: Log,
                    reported: set) -> Optional[GameStatus]:
    """/state của một câu cho tiến trình cha; lỗi thì báo một lần, 401/403 thì đọc lại token."""
    resp = api.state(ref.game_id)
    if resp.status == 200 and isinstance(resp.payload, dict):
        return GameStatus.from_json(resp.payload)
    if (ref.game_id, resp.status) not in reported:
        reported.add((ref.game_id, resp.status))
        log(f"/state của {ref.label()} lỗi (HTTP {resp.status}): {error_detail(resp.payload)}")
    if resp.status in (401, 403):
        for tok in tokens:
            tok.reload()
    return None


def wait_for_match(api: WebApi, match, tokens: Sequence[TeamToken], sup: Supervisor, log: Log,
                   sleep: Callable[[float], None], every: float,
                   stop_when: Optional[Callable[[Supervisor], bool]]) -> Optional[List[GameRef]]:
    """Trận chưa công bố hoặc manager API lỗi: hỏi lại mỗi `every` giây tới khi có.

    None nếu người dùng dừng, hoặc lỗi không tự hết (tên trùng, chế độ chưa hỗ trợ).
    """
    logged_at: Optional[float] = None
    while True:
        try:
            return resolve(api, match=match, log=log)
        except ResolveError as exc:
            if not exc.retry:
                log(str(exc))
                return None
            if logged_at is None or time.monotonic() - logged_at >= WAIT_LOG_SECONDS:
                logged_at = time.monotonic()
                log(f"{exc}\nhỏi lại mỗi {every:g}s cho tới khi thấy trận {match!r}; Ctrl-C để thôi")
        if stop_when is not None and stop_when(sup):
            sup.stopping = True
        if sup.stopping:
            return None
        sleep(every)
        for tok in tokens:           # manager API trả 401 thì token mới trong .env được dùng ngay
            tok.reload()


def follow(args, tokens: Sequence[TeamToken], log: Log, sleep: Callable[[float], None] = time.sleep,
           status_every: float = 5.0, questions_every: float = 15.0,
           stop_when: Optional[Callable[[Supervisor], bool]] = None) -> int:
    """Theo dõi cả trận: mỗi câu mở ra là có một bot cho mỗi đội."""
    if args.game or not args.match:
        # lỗi cách gọi: chờ bao lâu cũng không tự hết, nên dừng ngay chứ không hỏi lại
        log("--follow cần --match (ID hoặc tên trận), không dùng cùng --game")
        return 2
    api = make_api(args.url, tokens[0], args.timeout, log)
    sup = Supervisor(args, log)
    reported: set = set()
    with _ctrl_c(sup, log):
        try:
            refs = wait_for_match(api, args.match, tokens, sup, log, sleep, questions_every, stop_when)
            if refs is None:
                return 1
            if refs[0].mode != CONTEST:
                log("--follow chỉ dùng cho trận contest; trận practice thì chạy web-bench")
                return 2
            match_id = refs[0].match_id
            log(f"theo dõi trận {match_id} «{refs[0].match_name}»: {len(refs)} câu, {len(tokens)} đội. "
                "Ctrl-C một lần để thôi bắt câu mới.")
            listed_at = time.monotonic()
            while True:
                try:
                    sup.reap()
                    if stop_when is not None and stop_when(sup):
                        sup.stopping = True
                    if sup.stopping:
                        if not sup.children:
                            break
                        sleep(0.5)
                        continue
                    if time.monotonic() - listed_at >= questions_every:
                        listed_at = time.monotonic()
                        try:
                            refs = [r for r in fetch_questions(api, log) if r.match_id == match_id] or refs
                        except ResolveError as exc:
                            log(f"không làm mới được danh sách câu ({exc}); giữ danh sách cũ")
                    for ref in refs:
                        waiting = [tok for tok in tokens
                                   if not sup.running(ref, tok.var) and not sup.blocked(ref, tok.var)]
                        if not waiting:
                            continue
                        st = question_status(api, ref, tokens, log, reported)
                        if st is not None and st.status in (SELECTING, IN_PROGRESS):
                            for tok in waiting:
                                sup.spawn(ref, tok.var)
                except Exception as exc:
                    # cha chết thì các con chơi mù và không ai bắt câu mới: báo rồi theo dõi tiếp
                    log(f"lỗi khi theo dõi trận ({exc!r}); bot đang chơi không bị ảnh hưởng, "
                        f"thử lại sau {status_every:g}s")
                sleep(status_every)
        except KeyboardInterrupt:
            log("dừng tất cả bot")
            sup.terminate_all()
    return 0


def play_many(args, tokens: Sequence[TeamToken], log: Log,
              sleep: Callable[[float], None] = time.sleep) -> int:
    """Một câu, nhiều đội: mỗi đội một tiến trình con."""
    api = make_api(args.url, tokens[0], args.timeout, log)
    try:
        refs = resolve(api, match=args.match, question=args.question, game=args.game,
                       practice=args.practice, label=args.label, log=log)
        ref = pick_one(api, refs, tokens[0].team_id)
    except ResolveError as exc:
        log(str(exc))
        return 1
    sup = Supervisor(args, log)
    with _ctrl_c(sup, log):
        try:
            for tok in tokens:
                sup.spawn(ref, tok.var)
            while sup.children:
                sup.reap()
                if sup.children:
                    sleep(0.5)
        except KeyboardInterrupt:
            log("dừng tất cả bot")
            sup.terminate_all()
    return 0 if sup.done and all(s.get("exit_code") == 0 for s in sup.done) else 1


def match_teams(api: WebApi, match_id) -> List[Tuple[int, str]]:
    resp = api.matches()
    if resp.status != 200 or not isinstance(resp.payload, dict):
        return []
    for row in resp.payload.get("data") or []:
        if row.get("id") == match_id:
            return [(int(t["id"]), str(t.get("name", t["id"]))) for t in row.get("teams") or []]
    return []


def peer_scores(api: WebApi, ref: GameRef, teams: Sequence[Tuple[int, str]], exclude,
                sleep: Callable[[float], None] = time.sleep) -> List[dict]:
    """Điểm practice của các đội khác trên cùng câu; đội chưa chơi hoặc đã reset thì bỏ qua."""
    scores = []
    for team_id, name in teams:
        if team_id == exclude:
            continue
        resp = api.practice_score(f"{ref.game_id}:{team_id}")
        if resp.status == 429:
            sleep(1.0)
            resp = api.practice_score(f"{ref.game_id}:{team_id}")
        score = score_of(resp.payload, team_id) if resp.status == 200 else None
        if score and score.get("days_submitted"):
            scores.append(dict(score, team_id=team_id, name=name))
        sleep(0.2)
    return scores


def bench_row(ref: GameRef, ours: Optional[dict], peers: List[dict]) -> dict:
    best = max(peers, key=lambda s: (s["L"], s["D"], s["Q"]), default=None)
    checked = (ours or {}).get("replay_check") or {}
    return {"question": ref.question, "game_id": ref.game_id, "ours": (ours or {}).get("score"),
            "best_peer": best, "peers_with_data": len(peers),
            "mismatch_days": checked.get("days_with_mismatch"),
            "exit_code": (ours or {}).get("exit_code")}


def _triple(score: Optional[dict]) -> str:
    return f"({score['L']}, {score['D']}, {score['Q']})" if score else "(—)"


def bench(args, tok: TeamToken, log: Log, sleep: Callable[[float], None] = time.sleep) -> int:
    """Chạy các câu practice của một trận rồi so điểm với các đội khác."""
    api = make_api(args.url, tok, args.timeout, log)
    try:
        refs = resolve(api, match=args.match, log=log)
    except ResolveError as exc:
        log(str(exc))
        return 1
    if refs[0].mode != PRACTICE:
        log(f"web-bench chỉ chạy trận practice; trận {args.match!r} là {refs[0].mode}")
        return 2
    if args.questions and args.questions.strip().lower() != "all":
        wanted = {q.strip().casefold() for q in args.questions.split(",") if q.strip()}
        refs = [r for r in refs if (r.question or "").casefold() in wanted]
    if not refs:
        log("không còn câu nào để chạy sau khi lọc --questions")
        return 1
    sup = Supervisor(args, log)
    queue = list(refs)
    with _ctrl_c(sup, log):
        try:
            while queue or sup.children:
                sup.reap()
                if sup.stopping:
                    queue.clear()
                while queue and len(sup.children) < max(1, args.jobs):
                    sup.spawn(queue.pop(0), tok.var)
                if sup.children:
                    sleep(0.5)
        except KeyboardInterrupt:
            log("dừng tất cả bot")
            sup.terminate_all()
    sup.reap()
    teams = match_teams(api, refs[0].match_id)
    rows = []
    for ref in refs:
        ours = next((s for s in sup.done if s.get("game_id") == ref.game_id), None)
        rows.append(bench_row(ref, ours, peer_scores(api, ref, teams, tok.team_id, sleep=sleep)))
    report_path = args.out or os.path.join("experiments", f"web-bench-{time.strftime('%Y%m%d-%H%M%S')}.json")
    directory = os.path.dirname(report_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump({"match": refs[0].match_name, "team": tok.describe(), "rows": rows},
                  fh, ensure_ascii=False, indent=2)
    for row in rows:
        best = row["best_peer"]
        who = f" {best['name']}" if best else ""
        mism = row["mismatch_days"]
        log(f"{row['question']:<5} ta {_triple(row['ours'])}  |  đội khác tốt nhất {_triple(best)}{who}  "
            f"|  đội có dữ liệu: {row['peers_with_data']}  |  lệch simulator: "
            f"{len(mism) if mism is not None else '?'} ngày")
    log(f"báo cáo: {report_path}")
    return 0
