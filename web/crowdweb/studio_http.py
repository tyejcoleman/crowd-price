"""The studio's routes. server.py hands a request here first when the server runs a studio (`api.studio` is set).

  /studio#key=...                                the studio page (the key stays in the fragment, as on /maker)
  /static/studio.js  /static/studio.css
  GET  /api/studio                               the session: is approving here on, is the assistant busy, which drop
  GET  /api/studio/events?after=N&epoch=E        the conversation as NDJSON lines, streamed as it happens (~25 s, then
                                                 the page asks again from the last line it got)
  POST /api/studio/say       {"text"}            a message from the maker to the assistant
  POST /api/studio/reset                         a new conversation
  GET  /api/studio/drops/<drop>                  the panel: the maker's view of that drop (a draft too)
  GET  /api/studio/approvals/<id>                Togi's review of a waiting approval: exactly what would be signed
  POST /api/studio/approvals/<id>/approve        {"review", "passphrase", "word"}: the person's yes

Every route needs the maker key (header X-Maker-Key), which the assistant never has: it is not in its environment, its
prompt or any command output. Every POST must come from this server's own page (Origin and Sec-Fetch-Site), so another
site open in the same browser cannot post here. The approval routes do nothing unless a person started this server
(the Togi desk opened; studio_main.py). The passphrase is read from the request body, handed to Togi and dropped; the
server logs only request lines (the path), never a body.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs

from .api import ApiError

STATIC = Path(__file__).with_name("static")
FILES = {"studio.js": "text/javascript", "studio.css": "text/css"}
STREAM_SECONDS = 25
PING_SECONDS = 10
APPROVES_PER_MINUTE = 10       # each try is an scrypt check; Togi's own counter locks after 5 wrong in any case


class _Limit:
    def __init__(self, per_minute: int):
        self.per_minute, self.hits, self.lock = per_minute, deque(), threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self.lock:
            while self.hits and now - self.hits[0] > 60:
                self.hits.popleft()
            if len(self.hits) >= self.per_minute:
                return False
            self.hits.append(now)
            return True


_approves = _Limit(APPROVES_PER_MINUTE)


def _same_origin(h) -> None:
    """A POST must come from this server's own page: the browser says so in Origin (and Sec-Fetch-Site, where sent)."""
    origin = (h.headers.get("Origin") or "").strip().lower()
    host = (h.headers.get("Host") or "").strip().lower()
    site = (h.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if not origin or not host or origin != f"http://{host}" or (site and site != "same-origin"):
        raise ApiError(403, "not_this_page", "Only the studio page itself can do that.")


def _query(h) -> dict:
    q = h.path.split("?", 1)[1] if "?" in h.path else ""
    return {k: v[0] for k, v in parse_qs(q.split("#", 1)[0]).items()}


def _int(v, default: int = 0) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def handle_get(h, studio, p: list[str]) -> bool:
    """True if this was a studio route (answered here)."""
    if p == ["studio"]:
        h._file("studio.html", "text/html; charset=utf-8")
        return True
    if len(p) == 2 and p[0] == "static" and p[1] in FILES:
        h._file(p[1], FILES[p[1]])
        return True
    if p[:2] != ["api", "studio"]:
        return False
    try:
        h._maker()
        rest = p[2:]
        if not rest:
            h._json(200, {"ok": True, "data": studio.info()})
        elif rest == ["events"]:
            q = _query(h)
            _stream(h, studio, _int(q.get("after")), _int(q.get("epoch"), studio.epoch))
        elif len(rest) == 2 and rest[0] == "drops":
            h._json(200, {"ok": True, "data": studio.panel(h._drop_id(rest[1]))})
        elif len(rest) == 2 and rest[0] == "approvals":
            h._json(200, {"ok": True, "data": studio.review(rest[1])})
        else:
            h._not_found()
    except ApiError as e:
        h._error(e)
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        pass
    except Exception:  # noqa: BLE001 - never show a trace to a browser
        h.log_error("internal error on GET %s", h.path.split("?", 1)[0])
        h._error(ApiError(500, "internal", "Something went wrong on our side."))
    return True


def handle_post(h, studio, p: list[str]) -> bool:
    if p[:2] != ["api", "studio"]:
        return False
    try:
        h._maker()
        _same_origin(h)
        rest = p[2:]
        if rest == ["say"]:
            body = h._body()
            h._json(200, {"ok": True, "data": studio.say(body.get("text"))})
        elif rest == ["reset"]:
            h._json(200, {"ok": True, "data": studio.reset()})
        elif len(rest) == 3 and rest[0] == "approvals" and rest[2] == "approve":
            if not _approves.allow():
                raise ApiError(429, "slow_down", "Too many tries. Wait a minute.")
            body = h._body()
            passphrase = body.pop("passphrase", None)
            try:
                out = studio.approve(rest[1], body.get("review"), passphrase, body.get("word"))
            finally:
                passphrase = None    # noqa: F841 - the only copy this server held
                body.clear()
            h._json(200, {"ok": True, "data": out})
        else:
            h._not_found()
    except ApiError as e:
        h._error(e)
    except Exception:  # noqa: BLE001
        h.log_error("internal error on POST %s", h.path.split("?", 1)[0])
        h._error(ApiError(500, "internal", "Something went wrong on our side."))
    return True


def _stream(h, studio, after: int, epoch: int) -> None:
    """NDJSON, one event per line, as the events happen; a ping line every PING_SECONDS so the page knows it is live."""
    h.send_response(200)
    h.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Connection", "close")
    h.end_headers()
    h.close_connection = True
    end = time.monotonic() + STREAM_SECONDS
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        evs, ep = studio.wait(after, epoch, min(PING_SECONDS, left))
        if ep != epoch:
            h.wfile.write((json.dumps({"t": "epoch", "epoch": ep}) + "\n").encode())
            h.wfile.flush()
            return
        lines = [json.dumps(e, ensure_ascii=False) for e in evs] or [json.dumps({"t": "ping"})]
        h.wfile.write(("\n".join(lines) + "\n").encode("utf-8"))
        h.wfile.flush()
        if evs:
            after = evs[-1]["seq"]
