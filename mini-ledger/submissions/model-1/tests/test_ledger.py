#!/usr/bin/env python3
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_BIN = os.path.join(ROOT, "ledger")
STATE_ROOT = os.path.join(ROOT, ".test-state")

FAILURES = []


def check(name, cond, detail=""):
    print(("[ok]   %s" % name) if cond else ("[FAIL] %s -- %s" % (name, detail)))
    if not cond:
        FAILURES.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, state_dir, port):
        self.state_dir = state_dir
        self.port = port
        env = dict(os.environ)
        env["PORT"] = str(port)
        env["LEDGER_STATE_DIR"] = state_dir
        errlog = open(os.path.join(state_dir, "server.err"), "wb")
        self.proc = subprocess.Popen(
            [sys.executable, LEDGER_BIN, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=errlog,
            preexec_fn=os.setsid,
        )
        deadline = time.time() + 15
        last_err = None
        while time.time() < deadline:
            try:
                self.get("/ledger")
                errlog.close()
                return
            except Exception as e:
                last_err = e
                time.sleep(0.05)
        errlog.close()
        detail = "poll=%r last_err=%r stderr=%r" % (
            self.proc.poll(),
            repr(last_err)[:200],
            open(os.path.join(state_dir, "server.err")).read()[:2000],
        )
        raise RuntimeError("server did not start: %s" % detail)

    def _req(self, method, path, obj=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(obj).encode() if obj is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post(self, path, obj):
        return self._req("POST", path, obj)

    def get(self, path):
        return self._req("GET", path)

    def ledger(self):
        code, obj = self.get("/ledger")
        if code != 200:
            raise RuntimeError("ledger fetch failed: %s" % code)
        return obj["entries"]

    def account(self, acct):
        return self.get("/account?account_id=%s" % acct)

    def kill(self):
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()

    def stop(self):
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        self.proc.wait()


def replay_invariants(entries):
    violations = []
    bal = {}
    held = {}
    open_holds = {}
    terminals = {}
    last_seq = 0
    for e in entries:
        seq = e.get("seq")
        if seq != last_seq + 1:
            violations.append("seq gap at %r" % (seq,))
        last_seq = seq
        kind = e.get("kind")
        if kind == "hold":
            ref = e.get("ref")
            if ref in open_holds or ref in terminals:
                violations.append("dup hold %s" % ref)
            open_holds[ref] = (e.get("account_id"), e.get("amount"))
            a = e.get("account_id")
            held[a] = held.get(a, 0) + e.get("amount", 0)
        elif kind in ("charge", "release"):
            ref = e.get("ref")
            if ref not in open_holds or ref in terminals:
                violations.append("bad terminal %s" % ref)
            else:
                exp = open_holds.pop(ref)
                if (e.get("account_id"), e.get("amount")) != exp:
                    violations.append("terminal mismatch %s" % ref)
                terminals[ref] = kind
                a = e.get("account_id")
                held[a] = held.get(a, 0) - e.get("amount", 0)
                if kind == "charge":
                    bal[a] = bal.get(a, 0) - e.get("amount", 0)
        elif kind == "topup":
            a = e.get("account_id")
            if a not in bal:
                violations.append("topup before account_open %s" % a)
            bal[a] = bal.get(a, 0) + e.get("amount", 0)
        elif kind == "account_open":
            a = e.get("account_id")
            if a in bal:
                violations.append("dup account %s" % a)
            bal[a] = e.get("amount", 0)
        else:
            violations.append("bad kind %r" % (kind,))
        for a in bal:
            if bal[a] - held.get(a, 0) < 0:
                violations.append("negative available %s at seq %s" % (a, seq))
    return violations


def is_prefix(earlier, later):
    return len(earlier) <= len(later) and earlier == later[: len(earlier)]


def test_functional(srv):
    code, _ = srv.post("/account", {"account_id": "a1", "initial": 100})
    check("account create", code == 200)
    code, _ = srv.post("/account", {"account_id": "a1", "initial": 100})
    check("account replay", code == 200)
    check("account replay appended nothing", len(srv.ledger()) == 1)

    code, obj = srv.account("a1")
    check("initial balance", code == 200 and obj.get("balance") == 100 and obj.get("available") == 100)

    code, _ = srv.post("/topup", {"account_id": "a1", "amount": 50, "key": "k1"})
    check("topup", code == 200)
    code, _ = srv.post("/topup", {"account_id": "a1", "amount": 50, "key": "k1"})
    check("topup replay", code == 200)
    check("topup replay appended nothing", len(srv.ledger()) == 2)
    code, _ = srv.post("/topup", {"account_id": "nope", "amount": 5, "key": "kx"})
    check("topup unknown account", code == 404)

    code, obj = srv.post("/hold", {"account_id": "a1", "ref": "h1", "amount": 40})
    check("hold", code == 200 and obj.get("held") is True)
    code, _ = srv.post("/hold", {"account_id": "a1", "ref": "h1", "amount": 99})
    check("hold duplicate_ref", code == 409)
    code, _ = srv.post("/hold", {"account_id": "a1", "ref": "h1", "amount": 40})
    check("hold exact replay", code == 200)
    check("hold replay appended nothing", len(srv.ledger()) == 3)

    code, obj = srv.account("a1")
    check("available after hold", obj.get("balance") == 150 and obj.get("available") == 110)

    code, _ = srv.post("/charge", {"ref": "h1"})
    check("charge", code == 200)
    code, _ = srv.post("/charge", {"ref": "h1"})
    check("charge replay", code == 200)
    check("charge appended once", len(srv.ledger()) == 4)
    code, _ = srv.post("/release", {"ref": "h1"})
    check("release after charge", code == 409)

    code, _ = srv.post("/hold", {"account_id": "a1", "ref": "h2", "amount": 30})
    code, _ = srv.post("/release", {"ref": "h2"})
    check("release", code == 200)
    code, _ = srv.post("/release", {"ref": "h2"})
    check("release replay", code == 200)
    code, _ = srv.post("/charge", {"ref": "h2"})
    check("charge after release", code == 409)
    code, _ = srv.post("/charge", {"ref": "ghost"})
    check("charge unknown ref", code == 404)
    code, _ = srv.post("/release", {"ref": "ghost"})
    check("release unknown ref", code == 404)

    entries = srv.ledger()
    check("functional replay clean", replay_invariants(entries) == [], repr(replay_invariants(entries)))

    code, obj = srv.account("a1")
    bal = replay_balance(entries, "a1")
    check("balance reconciles", obj.get("balance") == bal[0] and obj.get("available") == bal[1],
          "api=%r dump=%r" % (obj, bal))


def replay_balance(entries, acct):
    bal = 0
    held = 0
    for e in entries:
        a = e.get("account_id")
        if a != acct:
            continue
        if e["kind"] == "account_open":
            bal = e["amount"]
        elif e["kind"] == "topup":
            bal += e["amount"]
        elif e["kind"] == "hold":
            held += e["amount"]
        elif e["kind"] == "charge":
            bal -= e["amount"]
            held -= e["amount"]
        elif e["kind"] == "release":
            held -= e["amount"]
    return bal, bal - held


def test_racing_holds(srv):
    srv.post("/account", {"account_id": "r1", "initial": 5})
    n = 12
    barrier = threading.Barrier(n)
    codes = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        code, _ = srv.post("/hold", {"account_id": "r1", "ref": "drain-%d" % i, "amount": 1})
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    snap_before = srv.ledger()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap_after = srv.ledger()

    check("drain: exactly 5 holds won", codes.count(200) == 5, repr(codes))
    check("drain: losers got 409", codes.count(409) == n - 5, repr(codes))
    code, obj = srv.account("r1")
    check("drain: available zero", obj.get("balance") == 5 and obj.get("available") == 0, repr(obj))
    check("drain: prefix property", is_prefix(snap_before, snap_after))
    check("drain: replay clean", replay_invariants(snap_after) == [], repr(replay_invariants(snap_after)))


def test_racing_duplicate_holds(srv):
    srv.post("/account", {"account_id": "r2", "initial": 100})
    barrier = threading.Barrier(2)
    codes = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        code, _ = srv.post("/hold", {"account_id": "r2", "ref": "dup-1", "amount": 10})
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = srv.ledger()
    dup_entries = [e for e in entries if e.get("ref") == "dup-1" and e["kind"] == "hold"]
    check("dup hold: both 200", sorted(codes) == [200, 200], repr(codes))
    check("dup hold: exactly one entry", len(dup_entries) == 1, repr(dup_entries))


def test_racing_terminals(srv):
    srv.post("/account", {"account_id": "t1", "initial": 1000})
    refs = []
    for i in range(20):
        code, _ = srv.post("/hold", {"account_id": "t1", "ref": "t1-%d" % i, "amount": 10})
        assert code == 200
        refs.append("t1-%d" % i)

    results = {}
    lock = threading.Lock()
    for ref in refs:
        barrier = threading.Barrier(2)
        pair = {}

        def worker(kind):
            barrier.wait()
            code, _ = srv.post("/" + kind, {"ref": ref})
            with lock:
                pair[kind] = code

        threads = [threading.Thread(target=worker, args=("charge",)),
                   threading.Thread(target=worker, args=("release",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        results[ref] = pair

    bad = {r: p for r, p in results.items() if sorted(p.values()) != [200, 409]}
    check("terminal races: one 200 one 409 each", not bad, repr(bad))

    entries = srv.ledger()
    terms = {}
    for e in entries:
        if e["kind"] in ("charge", "release"):
            terms.setdefault(e["ref"], []).append(e["kind"])
    multi = {r: k for r, k in terms.items() if len(k) != 1}
    check("terminal races: exactly one terminal per ref", not multi, repr(multi))
    check("terminal races: replay clean", replay_invariants(entries) == [], repr(replay_invariants(entries)))


def test_replay_storm(srv):
    srv.post("/account", {"account_id": "s1", "initial": 10})
    srv.post("/topup", {"account_id": "s1", "amount": 7, "key": "sk"})
    srv.post("/hold", {"account_id": "s1", "ref": "sh", "amount": 3})
    srv.post("/charge", {"ref": "sh"})
    snap = srv.ledger()
    check("storm setup: 4 entries", len(snap) == 4, repr(len(snap)))

    n = 24
    barrier = threading.Barrier(n)
    codes = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        kind = i % 4
        if kind == 0:
            code, _ = srv.post("/account", {"account_id": "s1", "initial": 10})
        elif kind == 1:
            code, _ = srv.post("/topup", {"account_id": "s1", "amount": 7, "key": "sk"})
        elif kind == 2:
            code, _ = srv.post("/hold", {"account_id": "s1", "ref": "sh", "amount": 3})
        else:
            code, _ = srv.post("/charge", {"ref": "sh"})
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap_after = srv.ledger()

    check("storm: all replays 200", all(c == 200 for c in codes), repr(codes))
    check("storm: appended nothing", snap_after == snap, "%d -> %d entries" % (len(snap), len(snap_after)))


def test_durability(state_dir):
    port = free_port()
    srv = Server(state_dir, port)
    srv.post("/account", {"account_id": "d1", "initial": 100})
    srv.post("/topup", {"account_id": "d1", "amount": 25, "key": "dk"})
    srv.post("/hold", {"account_id": "d1", "ref": "dh", "amount": 10})
    srv.post("/charge", {"ref": "dh"})
    snap = srv.ledger()
    pre_balance, pre_obj = srv.account("d1")
    check("durability setup: 4 entries", len(snap) == 4, repr(len(snap)))

    srv.kill()
    check("durability: process group killed", srv.proc.returncode == -signal.SIGKILL)

    srv2 = Server(state_dir, free_port())
    post = srv2.ledger()
    check("durability: ledger preserves pre-kill entries", is_prefix(snap, post) and len(post) >= len(snap),
          "%d -> %d entries" % (len(snap), len(post)))
    code, obj = srv2.account("d1")
    check("durability: state intact after restart",
          code == 200 and obj.get("balance") == pre_obj.get("balance") and obj.get("available") == pre_obj.get("available"),
          repr(obj))

    code, _ = srv2.post("/topup", {"account_id": "d1", "amount": 5, "key": "dk2"})
    check("durability: server keeps working", code == 200)
    final = srv2.ledger()
    check("durability: prefix property across kill", is_prefix(post, final))
    check("durability: replay clean", replay_invariants(final) == [], repr(replay_invariants(final)))
    code, obj = srv2.account("d1")
    bal = replay_balance(final, "d1")
    check("durability: balances reconcile", obj.get("balance") == bal[0] and obj.get("available") == bal[1],
          "api=%r dump=%r" % (obj, bal))
    srv2.stop()


def main():
    shutil.rmtree(STATE_ROOT, ignore_errors=True)
    os.makedirs(STATE_ROOT, exist_ok=True)

    def run(name, fn):
        state_dir = os.path.join(STATE_ROOT, name)
        os.makedirs(state_dir, exist_ok=True)
        srv = Server(state_dir, free_port())
        try:
            fn(srv)
        finally:
            srv.stop()

    run("functional", test_functional)
    run("racing-holds", test_racing_holds)
    run("racing-dup-holds", test_racing_duplicate_holds)
    run("racing-terminals", test_racing_terminals)
    run("replay-storm", test_replay_storm)
    durability_dir = os.path.join(STATE_ROOT, "durability")
    os.makedirs(durability_dir, exist_ok=True)
    test_durability(durability_dir)

    shutil.rmtree(STATE_ROOT, ignore_errors=True)
    if FAILURES:
        print("\n%d FAILURE(S): %s" % (len(FAILURES), ", ".join(FAILURES)))
        sys.exit(1)
    print("\nall tests passed")


if __name__ == "__main__":
    main()
