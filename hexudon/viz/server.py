"""Web trực quan hoá: phục vụ trang, khởi chạy bot bằng tiến trình con, đọc file sự kiện.

Chỉ nghe 127.0.0.1 vì .env chứa token của đội. Mỗi trận là một thư mục trong
replays/viz/ (meta.json, events-*.jsonl, *.log), nên tắt web rồi mở lại vẫn tua
được. Bot chạy trong tiến trình riêng (start_new_session) để mỗi bot có trọn một
lõi CPU như khi thi, và bot chết không kéo web chết theo.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

from ..client.net import load_env
from ..client.tokens import TokenError, pick_tokens
from ..client.web_api import DEFAULT_URL, GameStatus
from ..client.web_games import (COMPETITIVE_PRACTICE, PRACTICE, GameRef, ResolveError,
                                describe_status, fetch_questions)
from ..client.web_runner import PROJECT_ROOT, make_api, slug
from ..offline.bots import BOT_TYPES
from .arena import BotSpec
from .events import EventWriter, read_events
from .guard import TEST_VAR_RE, GuardError, RunRegistry, allowed_on_real, check_real_game, check_token_var

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8"}
MAPS = ["sample_16", "sample_24", "sample_32", "random8", "random12", "random16", "random24", "random32"]
MAP_RE = re.compile(r"^(sample_(16|24|32)|random([89]|[12][0-9]|3[0-2]))$")
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?-(offline|fake|real)$")
MAX_BOTS = 16
PAGE_LIMIT = 5000
TAIL_LINES = 20
FAKE_PORT = 8099

Log = Callable[[str], None]


def tail(path: str, n: int = TAIL_LINES) -> List[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-n:]
    except OSError:
        return []


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def host_port(url: str) -> Tuple[str, int]:
    parsed = urlparse(url)
    return parsed.hostname or "127.0.0.1", parsed.port or (443 if parsed.scheme == "https" else 80)


def port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def token_vars_in(env_path: str) -> List[str]:
    """Tên các biến TEST_* có trong file token; không bao giờ trả giá trị."""
    return sorted(name for name in load_env(env_path) if TEST_VAR_RE.fullmatch(name))


def online_command(python: str, url: str, env_path: str, ref: GameRef, token_var: str,
                   events_path: str, out_dir: str, keep: bool) -> List[str]:
    """Một tiến trình con chơi đúng một câu với đúng một token TEST_*; không bao giờ có --live."""
    check_token_var(token_var)
    cmd = [python, "-m", "hexudon", "web", "--game", ref.game_id,
           "--label", f"{ref.match_name or 'câu'}/{ref.question or ref.game_id[:8]}",
           "--url", url, "--env", os.path.abspath(env_path), "--token-env", token_var,
           "--events", events_path, "--out-dir", out_dir]
    if ref.mode == PRACTICE:
        cmd.append("--practice")
    if keep:
        cmd.append("--keep")
    return cmd


@dataclass
class Proc:
    label: str
    popen: Optional[subprocess.Popen]
    log_path: str
    ended: bool = False


class Run:
    """Một trận: tiến trình con (nếu còn chạy) và toàn bộ sự kiện đã đọc."""

    def __init__(self, run_id: str, directory: str, meta: dict, procs: Sequence[Proc]):
        self.id = run_id
        self.dir = directory
        self.meta = meta
        self.procs = list(procs)
        self.events: List[dict] = []
        self.offsets: Dict[str, int] = {}
        self.stop_presses = 0
        self.lock = threading.Lock()
        self.server_writer: Optional[EventWriter] = None

    @staticmethod
    def load(run_id: str, directory: str) -> "Run":
        """Trận cũ trên đĩa: đọc hết các file rồi xếp theo giờ."""
        run = Run(run_id, directory, read_json(os.path.join(directory, "meta.json")) or {}, [])
        run.refresh()
        run.events.sort(key=lambda e: (e.get("t", 0), e.get("seq", 0)))
        return run

    @property
    def running(self) -> bool:
        return any(p.popen is not None and not p.ended for p in self.procs)

    def _server_event(self, bot: str, kind: str, data: dict) -> None:
        if self.server_writer is None:
            self.server_writer = EventWriter(os.path.join(self.dir, "events-server.jsonl"))
        self.server_writer.write(bot, kind, data)

    def refresh(self) -> None:
        """Ghi run_end cho tiến trình vừa thoát, rồi đọc thêm sự kiện mới từ mọi file."""
        with self.lock:
            for proc in self.procs:
                if not proc.ended and proc.popen is not None and proc.popen.poll() is not None:
                    proc.ended = True
                    self._server_event(proc.label, "run_end", {"exit_code": proc.popen.returncode,
                                                               "stderr_tail": tail(proc.log_path),
                                                               "stopped": self.stop_presses > 0})
            try:
                names = sorted(n for n in os.listdir(self.dir) if n.startswith("events-") and n.endswith(".jsonl"))
            except OSError:
                names = []
            for name in names:
                new, self.offsets[name] = read_events(os.path.join(self.dir, name), self.offsets.get(name, 0))
                self.events.extend(new)
            if not self.running and self.server_writer is not None:
                self.server_writer.close()
                self.server_writer = None

    def stop(self) -> str:
        """Lần 1: Ctrl-C (bot chơi nốt phần đang dở nếu làm được); lần 2: dừng hẳn; lần 3: giết."""
        with self.lock:
            self.stop_presses += 1
            alive = [p.popen for p in self.procs if p.popen is not None and p.popen.poll() is None]
            for popen in alive:
                if self.stop_presses == 1:
                    popen.send_signal(signal.SIGINT)
                elif self.stop_presses == 2:
                    popen.terminate()
                else:
                    popen.kill()
        if not alive:
            return "trận không còn chạy"
        return ["đã gửi Ctrl-C; bấm Dừng lần nữa để dừng hẳn", "đã dừng hẳn", "đã giết tiến trình"][
            min(self.stop_presses, 3) - 1]


class VizApp:
    """Toàn bộ trạng thái của web: các trận, quy tắc an toàn, server giả do web mở."""

    def __init__(self, root: str = PROJECT_ROOT, runs_dir: Optional[str] = None,
                 targets: Optional[Dict[str, Tuple[str, str]]] = None,
                 python: str = sys.executable, log: Optional[Log] = None):
        self.root = root
        self.runs_dir = runs_dir or os.path.join(root, "replays", "viz")
        self.targets = targets or {
            "fake": (f"http://127.0.0.1:{FAKE_PORT}", os.path.join(root, "experiments", "local.env")),
            "real": (DEFAULT_URL, os.path.join(root, ".env")),
        }
        self.python = python
        self.log = log or (lambda msg: None)
        self.runs: Dict[str, Run] = {}
        self.registry = RunRegistry()
        self.lock = threading.Lock()
        self.fake_proc: Optional[subprocess.Popen] = None

    # ---- đọc ----

    def options(self) -> dict:
        host, port = host_port(self.targets["fake"][0])
        return {
            "maps": MAPS,
            "bot_types": list(BOT_TYPES),
            "cpu": os.cpu_count() or 1,
            "fake": {"url": self.targets["fake"][0], "reachable": port_open(host, port),
                     "started_here": self.fake_proc is not None and self.fake_proc.poll() is None},
            "tokens": {name: token_vars_in(env) for name, (_, env) in self.targets.items()},
        }

    def _reap(self) -> None:
        """Cập nhật run_end cho tiến trình vừa thoát và trả token cho các trận đã xong, để trang
        `runs`/khởi động online không phải chờ ai đó mở trang `events()` của trận đó trước."""
        for run_id, run in list(self.runs.items()):
            if run.procs:
                run.refresh()
                if not run.running:
                    self.registry.release_run(run_id)

    def list_runs(self) -> List[dict]:
        self._reap()
        rows = []
        try:
            names = sorted(os.listdir(self.runs_dir), reverse=True)
        except OSError:
            names = []
        for name in names:
            meta = read_json(os.path.join(self.runs_dir, name, "meta.json"))
            if meta is None:
                continue
            run = self.runs.get(name)
            rows.append({"id": name, "mode": meta.get("mode"), "title": meta.get("title"),
                         "created": meta.get("created"), "running": bool(run and run.running)})
        return rows

    def get_run(self, run_id: str) -> Run:
        if not RUN_ID_RE.fullmatch(run_id or ""):
            raise KeyError(run_id)
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                directory = os.path.join(self.runs_dir, run_id)
                if not os.path.isfile(os.path.join(directory, "meta.json")):
                    raise KeyError(run_id)
                run = Run.load(run_id, directory)
                self.runs[run_id] = run
            return run

    def events(self, run_id: str, since: int) -> dict:
        run = self.get_run(run_id)
        run.refresh()
        running = run.running
        if not running:
            self.registry.release_run(run_id)
        with run.lock:
            since = max(0, since)
            chunk = run.events[since:since + PAGE_LIMIT]
            total = len(run.events)
        nxt = since + len(chunk)
        return {"events": chunk, "next": nxt, "more": nxt < total, "running": running,
                "warnings": self.cpu_warnings() if running else []}

    def questions(self, target: str, token_var: str) -> dict:
        url, env = self._target(target)
        check_token_var(token_var)
        [tok] = pick_tokens(env, [token_var], live=False)
        api = make_api(url, tok, 5.0, self.log)
        rows = []
        for ref in fetch_questions(api, self.log):
            if ref.mode == COMPETITIVE_PRACTICE:
                continue
            allowed = target != "real" or allowed_on_real(ref)
            status = ""
            if allowed:
                resp = api.state(ref.game_id)
                status = (describe_status(GameStatus.from_json(resp.payload, tok.team_id))
                          if resp.status == 200 and isinstance(resp.payload, dict) else f"HTTP {resp.status}")
            rows.append({"game_id": ref.game_id, "match_id": ref.match_id, "match": ref.match_name,
                         "question": ref.question, "mode": ref.mode, "status": status, "allowed": allowed,
                         "why": "" if allowed else "server thật: chỉ Practice và Mock Contest"})
        return {"team": tok.describe(), "warnings": list(tok.warnings), "rows": rows}

    # ---- chạy ----

    def start(self, body: dict) -> dict:
        mode = body.get("mode")
        if mode == "offline":
            return self.start_offline(body)
        if mode in ("fake", "real"):
            return self.start_online(mode, body)
        raise ValueError(f"chế độ không hợp lệ: {mode!r}")

    def start_offline(self, body: dict) -> dict:
        map_name = str(body.get("map") or "sample_16")
        if not MAP_RE.fullmatch(map_name):
            raise ValueError(f"map không hợp lệ: {map_name!r}")
        bots = body.get("bots") or []
        if not 1 <= len(bots) <= MAX_BOTS:
            raise ValueError(f"cần từ 1 tới {MAX_BOTS} bot")
        specs = []
        for bot in bots:
            kind = str(bot.get("type"))
            seconds = bot.get("seconds")
            text = f"{kind}:{float(seconds)}" if kind == "search" and seconds is not None else kind
            BotSpec.parse(text)
            specs.append(text)
        seed = int(body.get("seed") or 0)
        days = max(0, int(body.get("days") or 0))
        run_id, directory = self._new_run("offline")
        events = os.path.join(directory, "events-arena.jsonl")
        cmd = [self.python, "-m", "hexudon", "arena", "--map", map_name, "--events", events,
               "--seed", str(seed), "--days", str(days)]
        for spec in specs:
            cmd += ["--bot", spec]
        title = f"Offline {map_name}: " + ", ".join(specs)
        return self._launch(run_id, directory, "offline", title, body, [("arena", cmd)])

    def start_online(self, target: str, body: dict) -> dict:
        self._reap()
        url, env = self._target(target)
        game_id = str(body.get("game_id") or "")
        token_vars = list(dict.fromkeys(str(t) for t in body.get("tokens") or []))
        if not game_id or not token_vars:
            raise ValueError("cần chọn một câu và ít nhất một token")
        for var in token_vars:
            check_token_var(var)
        tokens = pick_tokens(env, token_vars, live=False)
        api = make_api(url, tokens[0], 5.0, self.log)
        ref = next((r for r in fetch_questions(api, self.log) if r.game_id == game_id), None)
        if ref is None:
            raise LookupError(f"câu {game_id}")
        if ref.mode == COMPETITIVE_PRACTICE:
            raise GuardError("competitive_practice chưa được hỗ trợ")
        if target == "real":
            check_real_game(ref)
        run_id, directory = self._new_run(target)
        try:
            self.registry.claim(target, game_id, token_vars, run_id)
        except GuardError:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        try:
            commands = [(var, online_command(self.python, url, env, ref, var,
                                             os.path.join(directory, f"events-{var}.jsonl"), directory,
                                             bool(body.get("keep"))))
                        for var in token_vars]
            where = "Server thật" if target == "real" else "Server giả"
            title = f"{where}: {ref.match_name}/{ref.question} — {', '.join(token_vars)}"
            request = {"mode": target, "game_id": game_id, "tokens": token_vars, "keep": bool(body.get("keep"))}
            return self._launch(run_id, directory, target, title, request, commands)
        except Exception:
            self.registry.release_run(run_id)
            raise

    def start_fake(self, day_seconds: float) -> dict:
        """Mở `serve --web` làm tiến trình con; không bao giờ mở lên cổng đang có server khác."""
        host, port = host_port(self.targets["fake"][0])
        with self.lock:
            if self.fake_proc is not None and self.fake_proc.poll() is None:
                raise GuardError("server giả do web mở vẫn đang chạy", status=409)
            if port_open(host, port):
                raise GuardError(f"cổng {port} đã có server (có thể là server giả bạn tự mở); dùng luôn server đó",
                                 status=409)
            os.makedirs(self.runs_dir, exist_ok=True)
            log_path = os.path.join(self.runs_dir, "fake-server.log")
            cmd = [self.python, "-m", "hexudon", "serve", "--web", "--port", str(port),
                   "--day-seconds", f"{float(day_seconds):g}"]
            with open(log_path, "a", encoding="utf-8") as fh:
                self.fake_proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=self.root,
                                                  env=self._child_env(), start_new_session=True)
            proc = self.fake_proc
        for _ in range(50):
            if port_open(host, port):
                return {"ok": True, "log": log_path}
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        raise RuntimeError(f"server giả không mở được; xem {log_path}")

    def cpu_warnings(self) -> List[str]:
        busy = sum(1 for run in list(self.runs.values()) for p in run.procs
                   if p.popen is not None and p.popen.poll() is None)
        cores = os.cpu_count() or 1
        if busy > cores - 1:
            return [f"{busy} tiến trình bot đang chạy trên máy {cores} lõi: có thể bị trễ hạn gửi bài"]
        return []

    def shutdown(self, grace: float = 5.0) -> None:
        """Tắt web: dừng mọi tiến trình con do web mở (bot + server giả), rồi giết nếu còn sống
        sau `grace` giây — cùng cách với Supervisor.terminate_all bên client/web_runner.py."""
        popens = [proc.popen for run in list(self.runs.values()) for proc in run.procs
                 if proc.popen is not None]
        if self.fake_proc is not None:
            popens.append(self.fake_proc)
        for popen in popens:
            if popen.poll() is None:
                popen.terminate()
        deadline = time.monotonic() + grace
        for popen in popens:
            if popen.poll() is None:
                try:
                    popen.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    popen.kill()

    # ---- nội bộ ----

    def _target(self, name: str) -> Tuple[str, str]:
        if name not in ("fake", "real"):
            raise ValueError(f"đích không hợp lệ: {name!r} (fake hoặc real)")
        return self.targets[name]

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = self.root + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def _new_run(self, mode: str) -> Tuple[str, str]:
        """Thư mục mới cho một trận; tên theo giờ, thêm số nếu trùng giây."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = 1
        with self.lock:
            while True:
                run_id = f"{stamp}-{mode}" if n == 1 else f"{stamp}-{n}-{mode}"
                directory = os.path.join(self.runs_dir, run_id)
                try:
                    os.makedirs(directory)
                    return run_id, directory
                except FileExistsError:
                    n += 1

    def _launch(self, run_id: str, directory: str, mode: str, title: str, request: dict,
                commands: Sequence[Tuple[str, List[str]]]) -> dict:
        procs = []
        for label, cmd in commands:
            log_path = os.path.join(directory, f"{slug(label)}.log")
            with open(log_path, "a", encoding="utf-8") as fh:
                popen = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=self.root,
                                         env=self._child_env(), start_new_session=True)
            procs.append(Proc(label, popen, log_path))
        meta = {"id": run_id, "mode": mode, "title": title, "created": time.time(), "request": request,
                "procs": [{"label": p.label, "log": os.path.basename(p.log_path)} for p in procs]}
        with open(os.path.join(directory, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        run = Run(run_id, directory, meta, procs)
        with self.lock:
            self.runs[run_id] = run
        self.log(f"bắt đầu {run_id}: {title}")
        return {"id": run_id, "warnings": self.cpu_warnings()}


class _Handler(BaseHTTPRequestHandler):
    app: Optional[VizApp] = None      # gắn bởi serve_viz

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, raw: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _local_host(self) -> bool:
        """Chặn DNS rebinding: Host phải đúng là 127.0.0.1 hoặc localhost với cổng của server."""
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _static(self, name: str) -> None:
        root = os.path.realpath(STATIC_DIR)
        path = os.path.realpath(os.path.join(root, name))
        ctype = STATIC_TYPES.get(os.path.splitext(path)[1])
        if not path.startswith(root + os.sep) or ctype is None or not os.path.isfile(path):
            self._json(404, {"error": "không có trang này"})
            return
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ValueError("thân request không phải JSON") from None
        if not isinstance(body, dict):
            raise ValueError("thân request phải là một object JSON")
        return body

    def _handle(self, fn) -> None:
        try:
            code, payload = 200, fn()
        except GuardError as exc:
            code, payload = exc.status, {"error": str(exc)}
        except (TokenError, ValueError) as exc:
            code, payload = 400, {"error": str(exc)}
        except ResolveError as exc:
            code, payload = 502, {"error": str(exc)}
        except LookupError as exc:
            code, payload = 404, {"error": f"không thấy {exc}"}
        except Exception as exc:      # web không được chết vì một request lạ
            code, payload = 500, {"error": repr(exc)}
        self._json(code, payload)

    def do_GET(self):
        if not self._local_host():
            self._json(403, {"error": "chỉ nhận truy cập qua 127.0.0.1"})
            return
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        app = self.app
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        elif path == "/api/options":
            self._handle(app.options)
        elif path == "/api/runs":
            self._handle(lambda: {"runs": app.list_runs()})
        elif re.match(r"^/api/runs/[^/]+/events$", path):
            run_id = path.split("/")[3]
            self._handle(lambda: app.events(run_id, int(query.get("since") or 0)))
        elif path == "/api/online/questions":
            self._handle(lambda: app.questions(query.get("target", ""), query.get("token", "TEST_DAU_01")))
        else:
            self._json(404, {"error": "không có"})

    def do_POST(self):
        if not self._local_host():
            self._json(403, {"error": "chỉ nhận truy cập qua 127.0.0.1"})
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._json(415, {"error": "cần Content-Type: application/json"})
            return
        path = urlparse(self.path).path
        app = self.app
        if path == "/api/runs":
            self._handle(lambda: app.start(self._body()))
        elif re.match(r"^/api/runs/[^/]+/stop$", path):
            run_id = path.split("/")[3]
            self._handle(lambda: {"message": app.get_run(run_id).stop()})
        elif path == "/api/fake-server":
            self._handle(lambda: app.start_fake(float(self._body().get("day_seconds") or 30)))
        else:
            self._json(404, {"error": "không có"})


def serve_viz(port: int = 8765, app: Optional[VizApp] = None,
              log: Optional[Log] = None) -> Tuple[ThreadingHTTPServer, str]:
    """Tạo web server trên 127.0.0.1 (chưa chạy vòng phục vụ); trả (httpd, url)."""
    app = app or VizApp(log=log)
    handler = type("BoundVizHandler", (_Handler,), {"app": app})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
