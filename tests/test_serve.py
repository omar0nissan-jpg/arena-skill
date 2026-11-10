"""Tests for skills/arena/serve.py, the read-only dashboard. Standard library only.

    python3 -m unittest discover -s tests -v

Starts the real server on a free port in a thread and talks to it over HTTP.
"""
import http.client
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(os.path.dirname(HERE), "skills", "arena")
sys.path.insert(0, SKILL)

import bracket as B  # noqa: E402
import serve as S  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class Server(unittest.TestCase):
    """One arena run on disk (16 agents, round 1 judged, one solution written), one live server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="arena-serve-")
        cls.cwd = cls.tmp
        cls.run_dir = os.path.join(cls.tmp, B.ROOT, "run1")
        rc = B.main(["init", "--quick", "--seed", "5", "--task", "Name the boat.", "--dir", cls.run_dir])
        assert rc == 0
        write(os.path.join(cls.tmp, B.ROOT, B.LATEST), cls.run_dir + "\n")
        write(B.spawn_out(cls.run_dir, "a001"), "Salty <script>alert(1)</script>")
        with open(os.path.join(cls.run_dir, B.STATE_FILE), encoding="utf-8") as fh:
            st = json.load(fh)
        rng = random.Random(1)
        for m in st["rounds"][0]["matches"]:
            B.record(st, m["id"], rng.choice([m["a"], m["b"]]), reason="fake", scores={m["a"]: 70, m["b"]: 60},
                     source="judge")
        B.advance(st)
        B.save_state(st)
        write(os.path.join(cls.tmp, "secret.txt"), "outside the run")
        cls.srv = S.make_server("127.0.0.1", 0, cwd=cls.cwd)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, host=None, method="GET"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host} if host else {}
        conn.request(method, path, headers=headers)
        r = conn.getresponse()
        body = r.read().decode("utf-8")
        conn.close()
        return r.status, dict(r.getheaders()), body

    def test_dashboard_page(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("<title>Arena Dashboard</title>", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_state_follows_latest(self):
        status, _, body = self.get("/api/state")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["agents_n"], 16)
        self.assertEqual(d["alive"], 8)
        self.assertEqual(d["eliminated"], 8)
        self.assertEqual(len(d["rounds"]), 2)
        self.assertEqual([u["alive"] for u in d["upcoming"]], [4, 2])
        self.assertEqual(d["next"]["phase"], "spawn")
        self.assertEqual(d["next"]["left"], 15, "one of 16 spawn solutions is written")
        m = d["rounds"][0]["matches"][0]
        self.assertTrue(m["winner"])
        self.assertEqual([f["label"] for f in m["files"]][0], "verdict")
        self.assertFalse(any(f["path"].startswith("/") for f in m["files"]), "paths are relative to the run")
        self.assertEqual(d["agents"]["a001"]["solution"], "r0/a001.md")
        self.assertTrue(d["agents"]["a001"]["has_solution"])
        self.assertIsNone(d["winner"])

    def test_file_inside_the_run(self):
        status, headers, body = self.get("/api/file?path=r0/a001.md")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers["Content-Type"], "served as text, never as html")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("<script>", body)
        status, _, body = self.get("/api/file?path=task.md")
        self.assertEqual((status, body.strip()), (200, "Name the boat."))

    def test_file_outside_the_run_is_refused(self):
        for bad in ("../../secret.txt", "../run1/../../secret.txt", os.path.join(self.tmp, "secret.txt"),
                    "%2e%2e/%2e%2e/secret.txt", "", ".", "r0", "nope.md"):
            status, _, body = self.get("/api/file?path=" + bad)
            self.assertEqual(status, 404, bad)
            self.assertNotIn("outside the run", body)

    def test_symlink_out_of_the_run_is_refused(self):
        link = os.path.join(self.run_dir, "sneaky.md")
        try:
            os.symlink(os.path.join(self.tmp, "secret.txt"), link)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        try:
            self.assertEqual(self.get("/api/file?path=sneaky.md")[0], 404)
        finally:
            os.remove(link)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.get("/api/state", host="evil.example:%d" % self.port)[0], 403)
        self.assertEqual(self.get("/api/state", host="localhost:%d" % self.port)[0], 200)
        self.assertEqual(self.get("/api/state", host="[::1]:%d" % self.port)[0], 200)

    def test_read_only(self):
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.get("/api/state", method=method)[0], 405)

    def test_runs_and_health_and_404(self):
        status, _, body = self.get("/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in json.loads(body)["runs"]], ["run1"])
        self.assertEqual(self.get("/healthz")[:1], (200,))
        self.assertEqual(self.get("/nope")[0], 404)

    def test_head(self):
        status, headers, body = self.get("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, "")
        self.assertGreater(int(headers["Content-Length"]), 1000)


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arena-snap-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finished_run_has_a_winner(self):
        st = B.new_state(7, 3, B.load_strategies(), self.tmp)
        rng = random.Random(3)
        while not st["champion"]:
            for m in B.current_round(st)["matches"]:
                B.record(st, m["id"], rng.choice([m["a"], m["b"]]))
            B.advance(st)
        snap = S.snapshot(st)
        self.assertEqual(snap["alive"], 1)
        self.assertEqual(snap["upcoming"], [])
        self.assertEqual(snap["winner"]["champion"], st["champion"])
        self.assertFalse(os.path.isabs(snap["winner"]["solution"]))
        json.dumps(snap)

    def test_no_run_is_an_empty_state(self):
        srv = S.make_server("127.0.0.1", 0, cwd=self.tmp)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/state" % srv.server_address[1], timeout=5) as r:
                self.assertTrue(json.loads(r.read())["empty"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_safe_path(self):
        write(os.path.join(self.tmp, "run", "a.md"), "x")
        run = os.path.join(self.tmp, "run")
        self.assertTrue(S.safe_path(run, "a.md"))
        self.assertIsNone(S.safe_path(run, "../run/../a.md"))
        self.assertIsNone(S.safe_path(run, "a.md\x00"))


class Cli(unittest.TestCase):
    def test_serve_subcommand_starts_and_answers(self):
        tmp = tempfile.mkdtemp(prefix="arena-cli-")
        proc = subprocess.Popen([sys.executable, os.path.join(SKILL, "bracket.py"), "serve", "--port", "0"],
                                cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertIn("arena dashboard on http://127.0.0.1:", line)
            url = line.split()[3]
            for _ in range(50):
                try:
                    with urllib.request.urlopen(url + "healthz", timeout=2) as r:
                        self.assertEqual(r.read(), b"ok\n")
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("server never answered")
        finally:
            proc.terminate()
            proc.communicate(timeout=10)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_serve_rejects_a_dir_without_a_run(self):
        r = subprocess.run([sys.executable, os.path.join(SKILL, "serve.py"), "--dir", tempfile.gettempdir()],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no arena.json", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
