"""The HTTP server: the two pages, their files, and a small JSON API (standard library only).

  /                         live drops (or straight to the only one)
  /d/<drop>                 the buyer's drop page
  /maker#key=...            the maker's view (the key stays in the fragment, so it is never sent in a URL or a referrer)

  GET  /api/drops                       published drops
  GET  /api/drops/<drop>                one drop, public
  GET  /api/cards                       the test cards a buyer can pick
  GET  /api/info                        which PayPal this server uses (fake or sandbox)
  GET  /media/<n>                       a product photo named in the maker's details file (only those)
  POST /api/drops/<drop>/join           {"name", "card", "secret"}  -> a hold at the starting price (asked again with the
                                        same secret after a lost reply, the same join comes back)
  POST /api/drops/<drop>/me             {"buyer", "secret"}
  POST /api/drops/<drop>/leave          {"buyer", "secret"}  -> the hold is released
  GET  /api/maker/drops/<drop>          header X-Maker-Key
  POST /api/maker/drops/<drop>/verify   header X-Maker-Key: every hold and capture read back from PayPal
  POST /api/maker/drops/<drop>/tieout   header X-Maker-Key: the balance proof

There is no route that settles, excludes or binds. Those move money, and only a person approves them, through Togi.
"""
from __future__ import annotations

import hmac
import io
import json
import re
import socket
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
JOINS_PER_DROP_MINUTE = 120    # per drop, whoever asks: the ceiling still holds behind a proxy that hides addresses
MAX_CONNECTIONS = 64           # open connections at once; a client that opens many slow ones cannot exhaust the threads
MAX_CONNECTIONS_PER_ADDRESS = 16   # of those, one address's share WHILE ITS REQUEST IS STILL ARRIVING (off behind --trust-proxy)
READ_TIMEOUT = 10              # seconds a connection may sit without sending anything
REQUEST_DEADLINE = 15          # seconds for a whole request to arrive, however slowly (READ_TIMEOUT alone is reset by every byte)
MIN_MAKER_KEY = 16
KEY_FAILS_PER_MINUTE = 20
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")


class RateLimit:
    def __init__(self, per_minute: int):
        self.per_minute, self.hits, self.lock = per_minute, {}, threading.Lock()

    def _q(self, who: str, now: float) -> deque:
        if len(self.hits) > 4096:          # forget everyone whose last minute is over
            self.hits = {k: q for k, q in self.hits.items() if q and now - q[-1] <= 60}
        q = self.hits.setdefault(who, deque())
        while q and now - q[0] > 60:
            q.popleft()
        return q

    def allow(self, who: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self._q(who, now)
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True

    def full(self, who: str) -> bool:
        now = time.monotonic()
        with self.lock:
            return len(self._q(who, now)) >= self.per_minute


class CappedServer(ThreadingHTTPServer):
    """A threading server that turns a connection away (closes it) when MAX_CONNECTIONS are already being served, or when one
    address already has `per_address` requests still ARRIVING (0: no per-address cap). A connection counts toward its address only
    until its request has been read in full (`arrived`): one that is merely waiting for the drop's turn, or for PayPal, is the
    server's own queue and must not shut that address (a whole venue behind one router) out of everything else. Together with the
    request deadline, a few slow clients can neither keep every slot nor keep one for long."""
    daemon_threads = True

    def __init__(self, *a, per_address: int | None = None, **kw):
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._per_address = MAX_CONNECTIONS_PER_ADDRESS if per_address is None else per_address
        self._arriving: dict[int, str] = {}         # id(connection) -> address, while its request is still arriving
        self._open: dict[str, int] = {}             # address -> how many of its connections are in that state
        self._open_lock = threading.Lock()
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        who = client_address[0]
        with self._open_lock:
            full = bool(self._per_address) and self._open.get(who, 0) >= self._per_address
            if full or not self._slots.acquire(blocking=False):
                taken = False
            else:
                taken = True
                self._arriving[id(request)] = who
                self._open[who] = self._open.get(who, 0) + 1
        if not taken:
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.arrived(request)
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.arrived(request)
            self._slots.release()

    def arrived(self, request) -> None:
        """This connection's request has been read in full (or it is over): its address no longer pays for it."""
        with self._open_lock:
            who = self._arriving.pop(id(request), None)
            if who is None:
                return
            left = self._open.get(who, 0) - 1
            if left > 0:
                self._open[who] = left
            else:
                self._open.pop(who, None)


class _Deadline(io.RawIOBase):
    """Reads a connection, but never for longer in all than `seconds`: a client that sends a byte every few seconds is idle for
    no single read, yet it keeps its slot for as long as it likes unless the whole request has a limit."""

    def __init__(self, sock, seconds: float):
        self.sock, self.end = sock, time.monotonic() + seconds

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        left = self.end - time.monotonic()
        if left <= 0:
            raise socket.timeout("the request took too long to arrive")
        self.sock.settimeout(min(READ_TIMEOUT, left))
        return self.sock.recv_into(b)


def make_handler(api: Api, maker_key: str, quiet: bool = True, hosts: set[str] | None = None, trust_proxy: bool = False):
    joins, drop_joins, key_fails = RateLimit(JOINS_PER_MINUTE), RateLimit(JOINS_PER_DROP_MINUTE), RateLimit(KEY_FAILS_PER_MINUTE)
    join_gate = threading.Lock()          # the two join limits are checked and taken together

    def admit_join(who: str, drop: str) -> None:
        """Called by the API once a join is valid and is not a hand-back of one that went through (api.py says why)."""
        with join_gate:
            if joins.full(who) or drop_joins.full(drop):
                raise ApiError(429, "slow_down", "Too many joins right now. Wait a minute.")
            joins.allow(who)
            drop_joins.allow(drop)

    class Handler(BaseHTTPRequestHandler):
        server_version = "CrowdPrice"
        sys_version = ""
        timeout = READ_TIMEOUT
        error_content_type = "application/json; charset=utf-8"
        error_message_format = '{"ok":false,"error":{"code":"http_%(code)d","message":"%(message)s"}}'

        def setup(self):
            super().setup()
            self.rfile.close()          # the plain reader gives way to one with a total deadline
            self.rfile = io.BufferedReader(_Deadline(self.connection, REQUEST_DEADLINE), 8192)

        def end_headers(self):          # every response, the standard library's own errors included, carries the policy
            self.connection.settimeout(READ_TIMEOUT)      # reading may have left a short deadline on the socket: writing gets a fair one
            if not getattr(self, "_policy_sent", False):
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", CSP)
                self._policy_sent = True
            super().end_headers()

        def send_response(self, *a, **kw):
            self._policy_sent = False
            super().send_response(*a, **kw)

        def _who(self) -> str:
            """The client's address. Behind a trusted proxy that is the RIGHT-most X-Forwarded-For entry: the one the proxy
            appended. Everything to its left was written by the client (or by proxies further out) and can be anything."""
            if trust_proxy:
                fwd = ",".join(self.headers.get_all("X-Forwarded-For") or []).split(",")[-1].strip()
                if fwd:
                    return fwd[:64]
            return self.client_address[0]

        def _host_ok(self) -> bool:
            if hosts is None:
                return True
            return (self.headers.get("Host") or "").strip().lower() in hosts

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
        def _arrived(self):
            """The request has been read in full: from here on the server's own work (a queue for the drop, a slow PayPal call) is
            not the client's doing, and does not count against its address's share of connections."""
            done = getattr(self.server, "arrived", None)
            if done is not None:
                done(self.request)

        def _body(self) -> dict:
            try:
                return self._read_body()
            finally:
                self._arrived()

        def _read_body(self) -> dict:
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
            except TimeoutError:
                self.close_connection = True
                raise ApiError(408, "too_slow", "That request took too long to arrive.") from None
            except ValueError:
                raise ApiError(400, "bad_json", "That is not valid JSON.") from None
            if not isinstance(data, dict):
                raise ApiError(400, "bad_json", "Send a JSON object.")
            return data

        def _maker(self):
            """The right key always works, whatever else came from this address; only wrong keys count toward the limit."""
            got = self.headers.get("X-Maker-Key") or ""
            if hmac.compare_digest(got.encode("utf-8", "replace"), maker_key.encode()):
                return
            who = self._who()
            if key_fails.full(who):
                raise ApiError(429, "slow_down", "Too many wrong keys from here. Wait a minute.")
            key_fails.allow(who)
            time.sleep(0.4)          # a wrong key costs time
            raise ApiError(401, "maker_key", "The maker key is missing or wrong.")

        def _parts(self) -> list[str]:
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            return [p for p in path.split("/") if p]

        def _drop_id(self, s: str) -> str:
            if not DROP_ID.match(s):
                raise ApiError(404, "drop_not_found", "This drop does not exist.")
            return s

        def _refuse_method(self):
            self._arrived()
            self._error(ApiError(405, "method", "Not here."))

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse_method

        def do_HEAD(self):
            self._arrived()
            self._headers(405, "application/json; charset=utf-8", 0)

        def do_GET(self):
            self._arrived()
            if not self._host_ok():
                return self._error(ApiError(421, "host", "Unknown host."))
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
                if p == ["api", "info"]:
                    return self._json(200, {"ok": True, "data": api.info()})
                if len(p) == 2 and p[0] == "media":
                    body, ctype = api.photo(p[1])
                    self._headers(200, ctype, len(body))
                    return self.wfile.write(body)
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
            if not (len(p) == 4 and p[:2] == ["api", "drops"]):         # only these read a body; the others are read in full already
                self._arrived()
            if not self._host_ok():
                self._arrived()
                return self._error(ApiError(421, "host", "Unknown host."))
            try:
                if len(p) == 4 and p[:2] == ["api", "drops"]:
                    drop, action = self._drop_id(p[2]), p[3]
                    body = self._body()
                    if action == "join":
                        who = self._who()
                        data = api.join(drop, body.get("name"), body.get("card"), body.get("secret"), lambda: admit_join(who, drop))
                        return self._json(200, {"ok": True, "data": data})
                    if action == "me":
                        return self._json(200, {"ok": True, "data": api.me(drop, body.get("buyer"), body.get("secret"))})
                    if action == "leave":
                        return self._json(200, {"ok": True, "data": api.leave(drop, body.get("buyer"), body.get("secret"))})
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


def serve(api: Api, maker_key: str, host: str = "127.0.0.1", port: int = 8780, quiet: bool = True,
          public_host: str | None = None, trust_proxy: bool = False) -> ThreadingHTTPServer:
    """Listen. The maker key must be at least MIN_MAKER_KEY characters. On this machine only (the default), a request must
    name this machine as its Host, which stops a web page elsewhere from reaching the server through DNS rebinding; with
    `public_host` that name is accepted too. Listening on every address with no public host accepts any Host.

    A request must arrive within REQUEST_DEADLINE in all, and one address may hold only MAX_CONNECTIONS_PER_ADDRESS connections.
    With `trust_proxy` the client is the right-most X-Forwarded-For entry (the proxy must set it), and the per-address connection
    cap is off, since every connection comes from the proxy: the proxy should buffer slow requests itself."""
    if not isinstance(maker_key, str) or len(maker_key) < MIN_MAKER_KEY:
        raise ValueError(f"the maker key must be at least {MIN_MAKER_KEY} characters")
    httpd = CappedServer((host, port), make_handler(api, maker_key, quiet, None, trust_proxy),
                         per_address=0 if trust_proxy else None)       # behind a proxy every client is the proxy: no per-address cap
    real = httpd.server_address[1]
    hosts = None
    if host in ("127.0.0.1", "localhost", "::1") or public_host:
        hosts = {f"{h}:{real}" for h in ("localhost", "127.0.0.1", "[::1]")} | ({public_host.lower()} if public_host else set())
    httpd.RequestHandlerClass = make_handler(api, maker_key, quiet, hosts, trust_proxy)
    return httpd
