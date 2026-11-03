"""The HTTP server: the two pages, their files, and a small JSON API (standard library only).

  /                         live drops (or straight to the only one)
  /d/<drop>                 the buyer's drop page
  /maker#key=...            the maker's view (the key stays in the fragment, so it is never sent in a URL or a referrer)

  GET  /api/drops                       published drops
  GET  /api/drops/<drop>                one drop, public
  GET  /api/cards                       the test cards a buyer can pick
  POST /api/drops/<drop>/join           {"name", "card"}  -> a hold at the starting price, and this buyer's token
  POST /api/drops/<drop>/me             {"buyer", "token"}
  POST /api/drops/<drop>/leave          {"buyer", "token"}  -> the hold is released
  GET  /api/maker/drops/<drop>          header X-Maker-Key
  POST /api/maker/drops/<drop>/verify   header X-Maker-Key: every hold and capture read back from PayPal
  POST /api/maker/drops/<drop>/tieout   header X-Maker-Key: the balance proof

There is no route that settles, excludes or binds. Those move money, and only a person approves them, through Togi.
"""
from __future__ import annotations

import hmac
import json
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import Api, ApiError

STATIC = Path(__file__).with_name("static")
FILES = {"app.css": "text/css", "app.js": "text/javascript", "drop.js": "text/javascript", "maker.js": "text/javascript",
         "index.js": "text/javascript", "mark.svg": "image/svg+xml"}
PAGES = {"drop": "drop.html", "maker": "maker.html", "index": "index.html"}
DROP_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BODY = 4096
JOINS_PER_MINUTE = 12          # per client address: holds are real sandbox calls, so a page cannot be used to flood them
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")


class RateLimit:
    def __init__(self, per_minute: int):
        self.per_minute, self.hits, self.lock = per_minute, {}, threading.Lock()

    def allow(self, who: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault(who, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def make_handler(api: Api, maker_key: str, quiet: bool = True):
    joins = RateLimit(JOINS_PER_MINUTE)

    class Handler(BaseHTTPRequestHandler):
        server_version = "CrowdPrice"
        sys_version = ""

        def log_message(self, format, *args):   # noqa: A002 - polling would flood the log: keep POSTs, errors, warnings
            is_request = format.startswith('"')
            failed = is_request and len(args) > 1 and str(args[1])[:1] in "45"
            if not quiet or not is_request or failed or getattr(self, "command", None) == "POST":
                super().log_message(format, *args)

        # ---- responses
        def _headers(self, status: int, ctype: str, length: int):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()

        def _json(self, status: int, payload):
            body = json.dumps(payload, separators=(",", ":"), default=str).encode()
            self._headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _error(self, e: ApiError):
            self._json(e.status, {"ok": False, "error": {"code": e.code, "message": e.message, **e.extra}})

        def _file(self, name: str, ctype: str):
            body = (STATIC / name).read_bytes()
            self._headers(200, ctype, len(body))
            self.wfile.write(body)

        def _not_found(self):
            self._error(ApiError(404, "not_found", "Nothing here."))

        # ---- requests
        def _body(self) -> dict:
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                raise ApiError(415, "json_only", "Send JSON.")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                raise ApiError(413, "body_size", "That request is too large or empty.")
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                raise ApiError(400, "bad_json", "That is not valid JSON.") from None
            if not isinstance(data, dict):
                raise ApiError(400, "bad_json", "Send a JSON object.")
            return data

        def _maker(self):
            got = self.headers.get("X-Maker-Key") or ""
            if not hmac.compare_digest(got.encode(), maker_key.encode()):
                raise ApiError(401, "maker_key", "The maker key is missing or wrong.")

        def _parts(self) -> list[str]:
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            return [p for p in path.split("/") if p]

        def _drop_id(self, s: str) -> str:
            if not DROP_ID.match(s):
                raise ApiError(404, "drop_not_found", "This drop does not exist.")
            return s

        def do_GET(self):
            p = self._parts()
            try:
                if not p:
                    return self._file(PAGES["index"], "text/html; charset=utf-8")
                if p == ["maker"]:
                    return self._file(PAGES["maker"], "text/html; charset=utf-8")
                if len(p) == 2 and p[0] == "d":
                    self._drop_id(p[1])
                    return self._file(PAGES["drop"], "text/html; charset=utf-8")
                if len(p) == 2 and p[0] == "static" and p[1] in FILES:
                    return self._file(p[1], FILES[p[1]])
                if p == ["api", "drops"]:
                    return self._json(200, {"ok": True, "data": api.drops()})
                if p == ["api", "cards"]:
                    return self._json(200, {"ok": True, "data": api.cards()})
                if len(p) == 3 and p[:2] == ["api", "drops"]:
                    return self._json(200, {"ok": True, "data": api.drop(self._drop_id(p[2]))})
                if len(p) == 4 and p[:3] == ["api", "maker", "drops"]:
                    self._maker()
                    return self._json(200, {"ok": True, "data": api.maker(self._drop_id(p[3]))})
                return self._not_found()
            except ApiError as e:
                return self._error(e)
            except Exception:  # noqa: BLE001 - never show a trace to a browser
                self.log_error("internal error on GET %s", self.path)
                return self._error(ApiError(500, "internal", "Something went wrong on our side."))

        def do_POST(self):
            p = self._parts()
            try:
                if len(p) == 4 and p[:2] == ["api", "drops"]:
                    drop, action = self._drop_id(p[2]), p[3]
                    body = self._body()
                    if action == "join":
                        if not joins.allow(self.client_address[0]):
                            raise ApiError(429, "slow_down", "Too many joins from here. Wait a minute.")
                        return self._json(200, {"ok": True, "data": api.join(drop, body.get("name"), body.get("card"))})
                    if action == "me":
                        return self._json(200, {"ok": True, "data": api.me(drop, body.get("buyer"), body.get("token"))})
                    if action == "leave":
                        return self._json(200, {"ok": True, "data": api.leave(drop, body.get("buyer"), body.get("token"))})
                if len(p) == 5 and p[:3] == ["api", "maker", "drops"] and p[4] in ("verify", "tieout"):
                    self._maker()
                    drop = self._drop_id(p[3])
                    return self._json(200, {"ok": True, "data": api.verify(drop) if p[4] == "verify" else api.tieout(drop)})
                return self._not_found()
            except ApiError as e:
                return self._error(e)
            except Exception:  # noqa: BLE001
                self.log_error("internal error on POST %s", self.path)
                return self._error(ApiError(500, "internal", "Something went wrong on our side."))

    return Handler


def serve(api: Api, maker_key: str, host: str = "127.0.0.1", port: int = 8780, quiet: bool = True) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(api, maker_key, quiet))
    httpd.daemon_threads = True
    return httpd
