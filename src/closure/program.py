"""The agent's program: a small, fixed vocabulary for splitting a balance change into named parts.

The agent never writes a number. It writes a program like:

    {"question": "How much did we refund in October?",
     "currency": "USD", "start": "2026-10-01", "end": "2026-11-01",
     "answer": "refunds",
     "parts": [
       {"name": "refunds", "take": "gross", "where": {"code_prefix": ["T11"]}},
       {"name": "fees",    "take": "fee"},
       {"name": "everything else", "take": "rest"}]}

Every transaction has two components, gross and fee. Each part claims components of the
transactions its `where` matches: `gross`, `fee`, or `net` (both). A `rest` part (at most one,
last) claims whatever no earlier part claimed. A component claimed twice makes the program
invalid. That way the parts can add up to the whole balance change, and the certifier can tell.
"""
import re, unicodedata
from decimal import Decimal

TAKES = {"gross", "fee", "net", "rest"}
WHERE_KEYS = {"code", "code_prefix", "status", "sign", "id", "note_contains"}
MAX_PARTS = 24


# Text the agent writes (question, part names, note filters) may not carry a money figure: only the
# certifier writes numbers. Checked after Unicode normalisation (fullwidth digits and signs fold to
# ASCII). A bare integer (a year, "Q3", "Nov 10, 2026") is allowed; a known limit.
CURRENCY_WORDS = r"(usd|eur|gbp|jpy|cad|aud|inr|dollars?|euros?|cents?|bucks?|pounds?|yen|quid)"
MONEY = re.compile(
    r"\d[.,·٫'’]\d"                      # 12.50  12,50  1,234  1'500  12·50
    r"|(^|\s)[.,]\d"                      # .99
    r"|\d\s+[.·٫]\s*\d|\d\s*[.·٫]\s+\d"   # 4 2 . 5 0
    r"|\d\s+\d{3}\b"                      # 1 234
    r"|\b" + CURRENCY_WORDS + r"\s*\d"      # USD 42
    r"|\d\s*" + CURRENCY_WORDS + r"\b",     # 42 dollars, 99 cents
    re.I)
WHOLE_SECOND = re.compile(r"\A\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:?\d{2}))?\Z")
TOKEN = re.compile(r"\A[A-Z0-9]{1,24}\Z")  # event codes, statuses, transaction ids


def has_money(v):
    t = unicodedata.normalize("NFKC", v)
    return any(unicodedata.category(ch) == "Sc" for ch in t) or bool(MONEY.search(t))


class ProgramError(ValueError):
    pass


def _text(v, what, limit):
    if not isinstance(v, str) or not 0 < len(v) <= limit:
        raise ProgramError(f"{what} must be a short string")
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in v):
        raise ProgramError(f"{what} contains invalid characters")
    if has_money(v):
        raise ProgramError(f"{what} may not contain a money figure or currency: {v!r}")


def _strs(v, key):
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and 0 < len(x) <= 64 for x in v):
        raise ProgramError(f"where.{key} must be a non-empty list of short strings")
    return v


def validate(p):
    if not isinstance(p, dict):
        raise ProgramError("program must be an object")
    unknown = set(p) - {"question", "currency", "start", "end", "answer", "parts"}
    if unknown:
        raise ProgramError(f"unknown program keys: {sorted(unknown)}")
    for k in ("currency", "start", "end", "answer", "parts"):
        if k not in p:
            raise ProgramError(f"program is missing '{k}'")
    if "question" in p:
        _text(p["question"], "question", 300)
    for k in ("start", "end"):
        if not (isinstance(p[k], str) and WHOLE_SECOND.match(p[k])):
            raise ProgramError(f"{k} must be a date or a whole-second ISO time like 2026-10-01T00:00:00Z")
    if not (isinstance(p["currency"], str) and len(p["currency"]) == 3 and p["currency"].isalpha()):
        raise ProgramError("currency must be a 3-letter code like USD")
    parts = p["parts"]
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        raise ProgramError(f"parts must be a list of 1..{MAX_PARTS}")
    names = []
    for i, part in enumerate(parts):
        if not isinstance(part, dict) or set(part) - {"name", "take", "where"}:
            raise ProgramError(f"part {i}: only name, take, where are allowed")
        name, take = part.get("name"), part.get("take")
        _text(name, f"part {i} name", 64)
        if name in names:
            raise ProgramError(f"part name '{name}' used twice")
        names.append(name)
        if take not in TAKES:
            raise ProgramError(f"part '{name}': take must be one of {sorted(TAKES)}")
        if take == "rest":
            if "where" in part:
                raise ProgramError(f"part '{name}': a rest part takes no where")
            if i != len(parts) - 1:
                raise ProgramError(f"part '{name}': the rest part must be last")
            continue
        where = part.get("where", {})
        if not isinstance(where, dict) or set(where) - WHERE_KEYS:
            raise ProgramError(f"part '{name}': where keys must be among {sorted(WHERE_KEYS)}")
        for key in ("code", "code_prefix", "status", "id"):
            if key in where:
                _strs(where[key], key)
                if not all(TOKEN.match(x) for x in where[key]):
                    raise ProgramError(f"part '{name}': where.{key} values must be letters and digits, like T1107")
        if "sign" in where and where["sign"] not in ("in", "out"):
            raise ProgramError(f"part '{name}': sign must be 'in' or 'out'")
        if "note_contains" in where:
            _text(where["note_contains"], f"part '{name}' note_contains", 64)
    answer = p["answer"]
    answer = [answer] if isinstance(answer, str) else answer
    if not isinstance(answer, list) or not answer or any(a not in names for a in answer):
        raise ProgramError("answer must name one or more parts")
    return p


def matches(where, e):
    if "code" in where and e.code not in where["code"]:
        return False
    if "code_prefix" in where and not any(e.code.startswith(x) for x in where["code_prefix"]):
        return False
    if "status" in where and e.status not in where["status"]:
        return False
    if "id" in where and e.id not in where["id"]:
        return False
    if "sign" in where and not ((e.gross > 0) if where["sign"] == "in" else (e.gross < 0)):
        return False
    if "note_contains" in where and where["note_contains"].lower() not in e.note.lower():
        return False
    return True


def evaluate(p, events):
    """Assign every (transaction, component) to a part. Deterministic; no AI.

    Returns (parts, conflicts, unclaimed):
      parts      [{name, take, value, lines: [{id, component, amount}]}]
      conflicts  [(id, component, first_part, second_part)]   -> program is wrong
      unclaimed  [(id, component, amount)] non-zero, no part   -> answer would be incomplete
    """
    owner = {}
    parts, conflicts = [], []
    for part in p["parts"]:
        lines = []
        if part["take"] == "rest":
            for e in events:
                for comp in ("gross", "fee"):
                    if (e.id, comp) not in owner and getattr(e, comp) != 0:
                        owner[(e.id, comp)] = part["name"]
                        lines.append({"id": e.id, "component": comp, "amount": getattr(e, comp)})
        else:
            comps = ("gross", "fee") if part["take"] == "net" else (part["take"],)
            for e in events:
                if not matches(part.get("where", {}), e):
                    continue
                for comp in comps:
                    if getattr(e, comp) == 0:
                        continue
                    if (e.id, comp) in owner:
                        conflicts.append((e.id, comp, owner[(e.id, comp)], part["name"]))
                        continue
                    owner[(e.id, comp)] = part["name"]
                    lines.append({"id": e.id, "component": comp, "amount": getattr(e, comp)})
        parts.append({"name": part["name"], "take": part["take"],
                      "value": sum((l["amount"] for l in lines), Decimal("0.00")), "lines": lines})
    unclaimed = [(e.id, c, getattr(e, c)) for e in events for c in ("gross", "fee")
                 if getattr(e, c) != 0 and (e.id, c) not in owner]
    return parts, conflicts, unclaimed
