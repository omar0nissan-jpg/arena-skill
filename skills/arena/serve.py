#!/usr/bin/env python3
"""serve.py: a live, read-only dashboard for an /arena run, on your own machine.

    python3 serve.py                       # follows .arena/LATEST, http://127.0.0.1:8765
    python3 serve.py --dir .arena/<run>    # one fixed run
    python3 serve.py --port 0 --open       # any free port, and open the browser
    python3 bracket.py serve ...           # the same thing

It reads arena.json and the files next to it, and writes nothing. The page polls
every two seconds, so you can watch a 100-agent run play out while Claude drives it.
It listens on 127.0.0.1 only unless you pass --host, and it refuses requests whose
Host header is not local, so other sites cannot read your run through your browser.

Python 3.8+, standard library only.
"""
import argparse
import json
import os
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bracket as B  # noqa: E402

DASHBOARD_PATH = os.path.join(HERE, "dashboard.html")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_FILE_BYTES = 512 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


# ---------------------------------------------------------------- run lookup

def resolve_run_dir(fixed_dir=None, cwd="."):
    """The run to show: the fixed --dir, or whatever .arena/LATEST points at right now
    (read on every request, so a new init shows up without a restart)."""
    if fixed_dir:
        return os.path.abspath(fixed_dir)
    latest = os.path.join(cwd, B.ROOT, B.LATEST)
    if os.path.isfile(latest):
        with open(latest, encoding="utf-8") as fh:
            path = fh.read().strip()
        return path or None
    return None


def list_runs(cwd="."):
    root = os.path.join(cwd, B.ROOT)
    if not os.path.isdir(root):
        return []
    runs = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isfile(os.path.join(d, B.STATE_FILE)):
            runs.append({"name": name, "dir": os.path.abspath(d)})
    return runs


def load_run(run_dir):
    path = os.path.join(run_dir, B.STATE_FILE)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh), os.path.getmtime(path)


# ---------------------------------------------------------------- snapshot

def _rel(run_dir, path):
    return os.path.relpath(path, run_dir).replace(os.sep, "/")


def _file_entry(run_dir, label, path):
    return {"label": label, "path": _rel(run_dir, path), "exists": B._has_output(path)}


def match_files(state, rnd, m):
    d = state["dir"]
    a, b, mid = m["a"], m["b"], m["id"]
    return [
        _file_entry(d, "verdict", B.verdict_out(d, rnd, mid)),
        _file_entry(d, "%s solution (revised)" % a, B.revised_out(d, rnd, mid, a)),
        _file_entry(d, "%s solution (revised)" % b, B.revised_out(d, rnd, mid, b)),
        _file_entry(d, "%s attacks %s" % (a, b), B.attack_out(d, rnd, mid, a)),
        _file_entry(d, "%s attacks %s" % (b, a), B.attack_out(d, rnd, mid, b)),
        _file_entry(d, "%s defense" % a, B.defense_out(d, rnd, mid, a)),
        _file_entry(d, "%s defense" % b, B.defense_out(d, rnd, mid, b)),
    ]


def snapshot(state, mtime=None):
    """Everything the dashboard draws, from one arena.json. Pure: reads files, writes none."""
    d = state["dir"]
    alive = B.alive_ids(state)
    try:
        phase, left, jobs = B.next_action(state)
        nxt = {"phase": phase, "left": len(left), "total": len(jobs)}
    except (B.ArenaError, KeyError, TypeError) as e:
        nxt = {"phase": "unknown", "left": 0, "total": 0, "error": str(e)}

    rounds = []
    for rd in state["rounds"]:
        matches = []
        for m in rd["matches"]:
            matches.append({
                "id": m["id"], "a": m["a"], "b": m["b"],
                "winner": m["winner"], "loser": m["loser"], "reason": m.get("reason"),
                "survived": m.get("survived") or [], "scores": m.get("scores"),
                "source": m.get("source"), "files": match_files(state, rd["n"], m),
            })
        recorded = sum(1 for m in rd["matches"] if m["winner"])
        rounds.append({"n": rd["n"], "alive": len(rd["alive"]), "bye": rd["bye"], "closed": rd["closed"],
                       "recorded": recorded, "matches": matches})

    sizes = B.bracket_sizes(state["agents_n"])
    upcoming = [{"n": i + 1, "alive": s} for i, s in enumerate(sizes[:-1]) if i >= len(state["rounds"])]

    agents = {}
    for aid, a in state["agents"].items():
        agents[aid] = {
            "alive": a["alive"], "eliminated_in": a["eliminated_in"], "eliminated_by": a["eliminated_by"],
            "byes": a["byes"], "card": a["card"], "card_line": B._card_line(a["card"]),
            "solution": _rel(d, a["solution"]), "has_solution": B._has_output(a["solution"]),
        }

    files = [_file_entry(d, "task", os.path.join(d, "task.md")),
             _file_entry(d, "rubric", os.path.join(d, "rubric.md"))]
    if state.get("has_baseline"):
        files.append(_file_entry(d, "rejected answer (baseline)", os.path.join(d, "baseline.md")))
    if state.get("final"):
        files.append(_file_entry(d, "final verdict", B.final_out(d)))

    rep = B.winner_report(state)
    if rep:
        rep = dict(rep, solution=_rel(d, rep["solution"]))

    return {
        "dir": d, "name": os.path.basename(d.rstrip(os.sep)), "created": state.get("created"),
        "seed": state["seed"], "agents_n": state["agents_n"], "wave": state["wave"],
        "cards_available": state.get("cards_available"), "has_baseline": state.get("has_baseline", False),
        "alive": len(alive), "eliminated": state["agents_n"] - len(alive),
        "champion": state["champion"], "final": state.get("final"),
        "next": nxt, "plan": B.plan_totals(state["agents_n"], state["wave"]),
        "rounds": rounds, "upcoming": upcoming, "agents": agents, "files": files,
        "winner": rep, "updated": mtime,
    }


def safe_path(run_dir, rel):
    """rel resolved inside run_dir, or None. Blocks .., absolute paths and symlinks out."""
    if not rel or "\x00" in rel:
        return None
    root = os.path.realpath(run_dir)
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, target]) != root or target == root:
        return None
    return target if os.path.isfile(target) else None


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "arena-dashboard"
    sys_version = ""
    fixed_dir = None
    cwd = "."
    check_host = True
    quiet = True

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, obj, status=HTTPStatus.OK):
        self._send(status, json.dumps(obj), "application/json; charset=utf-8")

    def _error(self, status, msg):
        self._json({"error": msg}, status)

    def _host_ok(self):
        if not self.check_host:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name in LOCAL_HOSTS

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "this dashboard only answers on localhost")
        url = urlparse(self.path)
        route = url.path.rstrip("/") or "/"
        try:
            if route in ("/", "/index.html"):
                with open(DASHBOARD_PATH, encoding="utf-8") as fh:
                    return self._send(HTTPStatus.OK, fh.read(), "text/html; charset=utf-8")
            if route == "/healthz":
                return self._send(HTTPStatus.OK, "ok\n", "text/plain; charset=utf-8")
            if route == "/api/state":
                return self.api_state()
            if route == "/api/runs":
                return self._json({"runs": list_runs(self.cwd), "following": self.fixed_dir is None})
            if route == "/api/file":
                return self.api_file(parse_qs(url.query).get("path", [""])[0])
        except (OSError, ValueError) as e:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
        return self._error(HTTPStatus.NOT_FOUND, "no such page")

    def _run_dir(self):
        return resolve_run_dir(self.fixed_dir, self.cwd)

    def api_state(self):
        d = self._run_dir()
        if not d or not os.path.isfile(os.path.join(d, B.STATE_FILE)):
            return self._json({"empty": True, "dir": d,
                               "hint": "No arena yet. Run /arena in Claude Code; this page picks it up."})
        state, mtime = load_run(d)
        return self._json(snapshot(state, mtime))

    def api_file(self, rel):
        d = self._run_dir()
        target = safe_path(d, rel) if d else None
        if target is None:
            return self._error(HTTPStatus.NOT_FOUND, "no such file in this run")
        size = os.path.getsize(target)
        with open(target, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES)
        text = data.decode("utf-8", errors="replace")
        if size > MAX_FILE_BYTES:
            text += "\n\n[cut at %d KB of %d KB]" % (MAX_FILE_BYTES // 1024, size // 1024)
        return self._send(HTTPStatus.OK, text, "text/plain; charset=utf-8")

    def _no_writes(self):
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "read-only")

    do_POST = do_PUT = do_DELETE = do_PATCH = _no_writes


def make_server(host=DEFAULT_HOST, port=DEFAULT_PORT, fixed_dir=None, cwd=".", quiet=True):
    attrs = {"fixed_dir": os.path.abspath(fixed_dir) if fixed_dir else None, "cwd": os.path.abspath(cwd),
             "check_host": host in LOCAL_HOSTS, "quiet": quiet}
    handler = type("ArenaHandler", (Handler,), attrs)
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def build_parser():
    p = argparse.ArgumentParser(prog="serve.py", description="Live read-only dashboard for an /arena run.")
    add_arguments(p)
    return p


def add_arguments(p):
    p.add_argument("--dir", help="one fixed run directory (default: follow .arena/LATEST)")
    p.add_argument("--host", default=DEFAULT_HOST, help="address to listen on (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="port (default %d, 0 picks a free one)"
                   % DEFAULT_PORT)
    p.add_argument("--open", action="store_true", help="open the dashboard in your browser")
    p.add_argument("--verbose", action="store_true", help="log every request")


def run(args):
    if args.dir and not os.path.isfile(os.path.join(args.dir, B.STATE_FILE)):
        print("error: no %s in %s" % (B.STATE_FILE, os.path.abspath(args.dir)), file=sys.stderr)
        return 2
    try:
        srv = make_server(args.host, args.port, args.dir, quiet=not args.verbose)
    except OSError as e:
        print("error: cannot listen on %s:%s (%s). Try --port 0." % (args.host, args.port, e), file=sys.stderr)
        return 2
    host, port = srv.server_address[:2]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = "http://%s:%d/" % ("[%s]" % shown if ":" in shown else shown, port)
    if args.host not in LOCAL_HOSTS:
        print("warning: listening on %s. Anyone who can reach this machine can read the run." % args.host)
    print("arena dashboard on %s  (%s)" % (url, args.dir or "following .arena/LATEST"))
    print("read-only. Ctrl+C to stop.")
    if args.open:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        srv.server_close()
    return 0


def main(argv=None):
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
