"""Words the assistant chose, made safe to put on the page: a drop's name, which is shown beside amounts the person signs."""
from __future__ import annotations

import unicodedata

MAX_MARKS = 2        # combining marks one character may carry: ordinary writing needs at most two (Vietnamese, Thai), a stack of
                     # forty ("zalgo") draws over the lines above and below it, such as the amounts


def visible(s: str, cap: int = 120) -> str:
    """`s` as a person reads it: characters that can move or hide text (controls, format characters such as bidi overrides and
    zero-width ones, line and paragraph separators) are shown as their code, never applied; a run of combining marks is cut to
    MAX_MARKS; the whole is cut at `cap` characters."""
    out, run = [], 0
    for ch in unicodedata.normalize("NFC", str(s)[:cap * 2]):         # (NFC folds é written as e + accent back into one character)
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"):
            out.append(f"\\u{ord(ch):04x}")
            run = 0
        elif cat[0] == "M":
            run += 1
            if run <= MAX_MARKS:
                out.append(ch)
        else:
            out.append(ch)
            run = 0
    t = "".join(out)
    return t if len(t) <= cap else t[:cap - 1] + "…"
