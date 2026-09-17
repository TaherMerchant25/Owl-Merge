#!/usr/bin/env python3
"""Web front end for kg_engine: ask a question in the browser, get evidence paths and an answer.

usage: .venv/bin/python web_app.py [--host 127.0.0.1] [--port 8780]
       .venv/bin/python web_app.py selfcheck
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import kg_engine as E

PAGE = Path(__file__).resolve().parent / "web" / "index.html"
MAX_BODY = 8_000
KGS = {}


def parse_request(raw):
    """Validate an /api/ask body. Returns clean arguments or raises ValueError with a message for the user."""
    try:
        req = json.loads(raw or b"{}")
    except ValueError:
        raise ValueError("body must be JSON")
    if not isinstance(req, dict):
        raise ValueError("body must be a JSON object")
    if req.get("kg") not in E.OWL_FILES:
        raise ValueError(f"kg must be one of {sorted(E.OWL_FILES)}")
    question = req.get("question")
    if not isinstance(question, str) or not 0 < len(question.strip()) <= 500:
        raise ValueError("question must be 1-500 characters")

    def number(name, default, lo, hi, cast):
        v = req.get(name)
        if v in (None, ""):
            return default
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError(f"{name} must be a number")
        try:
            return min(hi, max(lo, cast(v)))
        except ValueError:
            raise ValueError(f"{name} must be a number")

    def flag(name, default):
        v = req.get(name, default)
        if not isinstance(v, bool):
            raise ValueError(f"{name} must be true or false")
        return v

    entities = req.get("entities") or []
    if not isinstance(entities, list) or len(entities) > 4 or any(not isinstance(e, str) or len(e) > 120 for e in entities):
        raise ValueError("entities must be up to 4 names of at most 120 characters")
    return {"kg": req["kg"], "question": question.strip(),
            "min_conf": number("min_conf", 0.9, 0.0, 1.0, float),
            "allow_unscored": flag("allow_unscored", False),
            "hops": number("hops", None, 1, 4, int),
            "max_paths": number("max_paths", 25, 1, 50, int),
            "llm": flag("llm", True),
            "entities": [e.strip() for e in entities if e.strip()]}


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/health":
            return self.send(200, {"kgs": {k: len(kg.labels()) for k, kg in KGS.items()},
                                   "llm": E.LLM_MODEL})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/ask":
            return self.send(404, {"error": "not found"})
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.send(400, {"error": "bad Content-Length"})
        if size > MAX_BODY:
            return self.send(413, {"error": "request too large"})
        try:
            args = parse_request(self.rfile.read(size))
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        if args["kg"] not in KGS:
            return self.send(503, {"error": f"store '{args['kg']}' is not loaded; run kg_engine.py load {args['kg']}"})
        kg = KGS[args["kg"]].fork(min_conf=args["min_conf"], allow_unscored=args["allow_unscored"])
        try:
            r = kg.ask(args["question"], args["entities"], args["hops"], args["max_paths"], llm=args["llm"])
        except Exception as e:
            return self.send(500, {"error": f"engine error: {e}"})
        r.update(kg=args["kg"], sparql=kg.log)
        self.send(422 if "error" in r else 200, r)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {fmt % args}\n")


def selfcheck():
    ok = parse_request(b'{"kg": "full", "question": " Is BRCA2 RAD51? ", "hops": 99, "min_conf": -1}')
    assert ok["question"] == "Is BRCA2 RAD51?" and ok["hops"] == 4 and ok["min_conf"] == 0.0 and ok["llm"] is True, ok
    for bad in (b"nope", b"[1]", b'{"kg": "other", "question": "x"}', b'{"kg": "full", "question": "  "}',
                b'{"kg": "full", "question": "x", "llm": "false"}', b'{"kg": "full", "question": "x", "hops": "two"}',
                b'{"kg": "full", "question": "x", "hops": true}', b'{"kg": "full", "question": "x", "entities": "BRCA2"}'):
        try:
            parse_request(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    print("selfcheck ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["serve", "selfcheck"], default="serve")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to expose on the network")
    ap.add_argument("--port", type=int, default=8780, help="default 8780 (8765 is taken by airbench on this machine)")
    a = ap.parse_args()
    if a.cmd == "selfcheck":
        return selfcheck()
    try:                                   # bind before the ~10 s store load, so a taken port fails fast
        server = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:
        sys.exit(f"cannot listen on {a.host}:{a.port} ({e.strerror}); another program is using that port. "
                 f"Try: .venv/bin/python web_app.py --port {a.port + 1}")
    for name in E.OWL_FILES:
        if (E.STORES / name).exists():
            t = time.time()
            KGS[name] = E.KG.open(name)
            KGS[name].labels()
            KGS[name].token_index()
            print(f"loaded {name}: {len(KGS[name].labels()):,} labelled nodes in {time.time() - t:.1f}s", flush=True)
    if not KGS:
        sys.exit("no stores found; run: kg_engine.py load simple && kg_engine.py load full")
    print(f"serving on http://{a.host}:{a.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
