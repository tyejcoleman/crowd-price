import copy, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from closure import certify as c  # noqa: E402
from closure.sources import Fixture, like_stock_toolkit  # noqa: E402
from closure.paypal import parse_time  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sandbox-2026-10-26.json")
NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
RAW = json.load(open(FIX))


def prog(**kw):
    p = {"question": "How much did we refund?", "currency": "USD",
         "start": "2026-10-25T00:00:00Z", "end": "2026-10-26T01:00:00Z", "answer": "refunds",
         "parts": [{"name": "refunds", "take": "gross", "where": {"code_prefix": ["T11"], "sign": "out"}},
                   {"name": "fees", "take": "fee"},
                   {"name": "everything else", "take": "rest"}]}
    p.update(kw)
    return p


def fixture(mutate=None):
    d = copy.deepcopy(RAW)
    if mutate:
        mutate(d)
    return Fixture(d)


class Dropping(Fixture):
    """Hands the certifier fewer records than PayPal has (a truncating fetch), while
    balances still come from PayPal's full history."""
    def __init__(self, full, keep):
        super().__init__(full)
        self.keep = keep

    def transactions(self, start, end):
        recs, lr, expected = super().transactions(start, end)
        return self.keep(recs), lr, expected  # PayPal's own count still covers what was dropped


def test_refunds_certified_to_the_cent():
    cert = c.certify(prog(), fixture(), now=NOW)
    assert cert["kind"] == "certificate", cert
    assert cert["answer"]["value"] == "-300.00"
    assert cert["opening"]["value"] == "0.00" and cert["closing"]["value"] == "6604.37"
    assert cert["left_over"] == "0.00"
    assert [l["id"] for l in cert["parts"][0]["lines"]] == [
        r["transaction_info"]["transaction_id"] for r in RAW["transaction_details"]
        if r["transaction_info"]["transaction_event_code"] == "T1107"]
    assert c.check_offline(cert) == []


def test_fees_and_conversion_in_eur():
    p = prog(currency="EUR", answer="fees",
             parts=[{"name": "sales", "take": "gross", "where": {"code": ["T0005"]}},
                    {"name": "fees", "take": "fee"},
                    {"name": "converted to USD", "take": "gross", "where": {"code_prefix": ["T02"]}}])
    cert = c.certify(p, fixture(), now=NOW)
    assert cert["kind"] == "certificate", cert
    assert cert["answer"]["value"] == "-8.43"
    assert {x["name"]: x["value"] for x in cert["parts"]} == {"sales": "310.40", "fees": "-8.43", "converted to USD": "-301.97"}


def test_same_second_records_reordered_by_the_chain():
    def shuffle(d):  # PayPal returned these three at 00:29:25; reverse them
        t = d["transaction_details"]
        idx = [i for i, r in enumerate(t) if r["transaction_info"]["transaction_initiation_date"].startswith("2026-10-26T00:29:25")]
        vals = [t[i] for i in idx][::-1]
        for i, v in zip(idx, vals):
            t[i] = v
    for cur in ("USD", "EUR"):
        cert = c.certify(prog(currency=cur), fixture(shuffle), now=NOW)
        assert cert["kind"] == "certificate", cert


def drop(ids):
    return lambda rs: [r for r in rs if r["transaction_info"]["transaction_id"] not in ids]


class Lying(Dropping):
    """Drops records AND reports the reduced count, so only the running balance can catch it."""
    def transactions(self, start, end):
        recs, lr, _ = super().transactions(start, end)
        return recs, lr, len(recs)


def test_a_missing_record_is_caught_by_the_count():
    out = c.certify(prog(), Dropping(copy.deepcopy(RAW), drop({"61401427AU9517523"})), now=NOW)
    assert out["reason"] == "incomplete_fetch" and out["detail"] == {
        "explanation": out["detail"]["explanation"], "paypal_count": 15, "fetched": 14}


def test_a_missing_record_is_named_by_the_chain_when_the_count_agrees():
    out = c.certify(prog(), Lying(copy.deepcopy(RAW), drop({"61401427AU9517523"})), now=NOW)
    assert out["reason"] == "chain_break"
    assert out["detail"]["gap"] == "243.03"  # 250.00 gross - 6.97 fee: exactly the missing sale


def test_zero_net_pair_dropped_is_refused():
    # Refuter case: pending payout (-150.25 net) + hold release (+150.25) sum to zero, so the chain
    # alone cannot see them go. PayPal's own count can.
    pair = {"0T20228522546222J", "1L0240486L192371X"}
    out = c.certify(prog(answer="fees"), Dropping(copy.deepcopy(RAW), drop(pair)), now=NOW)
    assert out["reason"] == "incomplete_fetch"


def test_duplicate_record_is_refused():
    def twice(rs):
        return rs + [r for r in rs if r["transaction_info"]["transaction_id"] in {"0T20228522546222J", "1L0240486L192371X"}]

    class Dup(Dropping):
        def transactions(self, start, end):
            recs, lr, _ = super().transactions(start, end)
            return recs, lr, len(recs)  # even if the count is fooled too
    assert c.certify(prog(), Dup(copy.deepcopy(RAW), twice), now=NOW)["reason"] == "duplicate_record"


def test_window_edges_exactly_on_records():
    # A window starting on the 1200.00 sale includes it; one ending on the payout hold excludes it.
    cert = c.certify(prog(start="2026-10-26T00:29:06Z", end="2026-10-26T00:29:35Z", answer="sales",
                          parts=[{"name": "sales", "take": "gross", "where": {"code": ["T0005"]}},
                                 {"name": "rest", "take": "rest"}]), fixture(), now=NOW)
    assert cert["kind"] == "certificate", cert
    assert cert["opening"]["value"] == "5041.39" and cert["closing"]["value"] == "6754.62"
    assert "61401427AU9517523" in {l["id"] for l in cert["parts"][0]["lines"]}
    assert "5LF27555UL681422G" not in {t["id"] for t in cert["transactions"]}


def test_stock_toolkit_page_cut_is_refused():
    src = Dropping(copy.deepcopy(RAW), lambda rs: like_stock_toolkit(Fixture(dict(RAW, transaction_details=rs)),
                                                                     parse_time("2026-10-25"), parse_time("2026-10-27"),
                                                                     page_size=10)[0])
    out = c.certify(prog(), src, now=NOW)
    assert out["reason"] == "incomplete_fetch" and out["detail"]["fetched"] == 10


def test_filtered_search_has_no_running_balance():
    def strip(d):
        for r in d["transaction_details"]:
            r["transaction_info"].pop("ending_balance", None)
    out = c.certify(prog(), fixture(strip), now=NOW)
    assert out["reason"] == "no_running_balance"


def test_counted_twice_is_refused():
    p = prog(parts=[{"name": "refunds", "take": "gross", "where": {"code_prefix": ["T11"]}},
                    {"name": "all money", "take": "net"}])
    assert c.certify(p, fixture(), now=NOW)["reason"] == "counted_twice"


def test_left_over_is_refused():
    # T11 is a family: T1107 is the refund, T1105 the payout-hold release. Exact codes here.
    p = prog(parts=[{"name": "refunds", "take": "gross", "where": {"code": ["T1107"]}}])
    out = c.certify(p, fixture(), now=NOW)
    assert out["reason"] == "left_over" and out["detail"]["left_over"] == "6904.37"


def test_window_not_yet_indexed():
    out = c.certify(prog(end="2026-10-26T01:59:00Z"), fixture(), now=NOW)
    assert out["reason"] == "not_yet_indexed"


def test_future_and_invalid_programs():
    assert c.certify(prog(end="2027-01-01"), fixture(), now=NOW)["reason"] == "window_in_future"
    assert c.certify(prog(parts=[{"name": "x", "take": "everything"}]), fixture(), now=NOW)["reason"] == "invalid_program"
    assert c.certify(prog(answer="nope"), fixture(), now=NOW)["reason"] == "invalid_program"
    assert c.certify(prog(sql="drop table"), fixture(), now=NOW)["reason"] == "invalid_program"
    assert c.certify(prog(parts=[{"name": "r", "take": "rest"}, {"name": "f", "take": "fee"}]), fixture(), now=NOW)["reason"] == "invalid_program"


def test_tampered_certificate_fails_offline_check():
    cert = c.certify(prog(), fixture(), now=NOW)
    bad = copy.deepcopy(cert)
    bad["answer"]["value"] = "-30.00"
    assert c.check_offline(bad)
    bad = copy.deepcopy(cert)
    bad["parts"][0]["lines"][0]["amount"] = "-3.00"
    assert c.check_offline(bad)
    bad = copy.deepcopy(cert)
    bad["transactions"].pop(3)
    assert c.check_offline(bad)


def test_replay_holds_and_detects_change():
    cert = c.certify(prog(), fixture(), now=NOW)
    assert c.replay(cert, fixture(), now=NOW)["holds"] is True

    def edit(d):  # PayPal later reports a different refund amount
        for r in d["transaction_details"]:
            i = r["transaction_info"]
            if i["transaction_event_code"] == "T1107":
                i["transaction_amount"]["value"] = "-299.00"
    assert c.replay(cert, fixture(edit), now=NOW)["holds"] is False


def test_offline_check_ties_meaning_to_numbers():
    cert = c.certify(prog(), fixture(), now=NOW)
    def bad(f):
        b = copy.deepcopy(cert)
        f(b)
        return c.check_offline(b)
    assert bad(lambda b: b.update(question="How much did we earn?"))
    assert bad(lambda b: b["program"].update(question="How much did we earn?"))  # hash no longer matches
    assert bad(lambda b: b.update(program_hash="sha256:00"))
    assert bad(lambda b: b["answer"].update(parts="refunds+fees"))
    def move(b):  # refund lines moved into the rest part, values recomputed
        line = b["parts"][0]["lines"].pop()
        b["parts"][0]["value"] = "0"
        b["parts"][-1]["lines"].append(line)
        b["answer"]["value"] = "0"
    assert bad(move)
    assert bad(lambda b: b["parts"][0]["lines"][0].update(component="id"))  # reported, not a crash
    assert bad(lambda b: b["transactions"].append(dict(b["transactions"][0])))


# --- second refuter pass (2026-10-25) ---

def test_fractional_second_windows_are_rejected():
    for k, v in (("end", "2026-10-26T00:29:09.500Z"), ("start", "2026-10-26T00:29:09.5Z"), ("end", "2026-10-26T00:29:09.999999+00:00")):
        assert c.certify(prog(**{k: v}), fixture(), now=NOW)["reason"] == "invalid_program"


def test_record_outside_window_is_refused():
    class Leaky(Fixture):  # returns one record from before the window, count agrees
        def transactions(self, start, end):
            recs, lr, _ = super().transactions(start, end)
            extra = [r for r in self.records if r["transaction_info"]["transaction_id"] == "39B54847KU5927051"]
            return recs + extra, lr, len(recs) + 1
    out = c.certify(prog(start="2026-10-26T00:29:10Z", end="2026-10-26T00:29:20Z"), Leaky(copy.deepcopy(RAW)), now=NOW)
    assert out["reason"] == "record_outside_window"


def test_no_money_figures_in_free_text():
    assert c.certify(prog(question="Did we refund $300?"), fixture(), now=NOW)["reason"] == "invalid_program"
    p = prog(parts=[{"name": "refunds (-9,999.00)", "take": "gross", "where": {"code": ["T1107"]}},
                    {"name": "rest", "take": "rest"}], answer="refunds (-9,999.00)")
    assert c.certify(p, fixture(), now=NOW)["reason"] == "invalid_program"
    assert c.certify(prog(question="Refunds in Q3 2026?"), fixture(), now=NOW)["kind"] == "certificate"


def test_same_second_order_search_backtracks():
    from closure.ledger import Event
    from decimal import Decimal as D
    def ev(i, net, eb):
        return Event(id=i, seq=0, at="2026-10-26T00:00:00Z", code="T0005", status="S", currency="USD",
                     gross=D(net), fee=D(0), ending_balance=D(eb))
    events = [ev("A", "1", "101"), ev("B", "-1", "99"), ev("C", "1", "100")]  # valid order: B, C, A
    ordered, brk = c.chain(events, D("100"))
    assert brk is None and [e.id for e in ordered] == ["B", "C", "A"]


def test_fixed_fields_and_notes_are_checked_offline():
    cert = c.certify(prog(), fixture(), now=NOW)
    def bad(f):
        b = copy.deepcopy(cert)
        f(b)
        return c.check_offline(b)
    assert bad(lambda b: b.update(left_over="123.45"))
    assert bad(lambda b: b.update(record_count={"fetched": 3, "paypal_says": 99}))
    assert bad(lambda b: b.update(checks=[]))
    assert bad(lambda b: b.update(certifier="certifier/0"))
    assert bad(lambda b: b["opening"].update(source="me"))
    assert bad(lambda b: b["transactions"][0].update(note="moved"))  # note is in data_hash now


def test_signature_catches_consistent_rewrites(tmp_path):
    from closure import sign, certify as cz
    key = sign.private_key(str(tmp_path / "k.pem"))
    pub = sign.public_hex(key)
    cert = sign.sign(c.certify(prog(), fixture(), now=NOW), key)
    assert c.check_offline(cert, trusted_key=pub) == []
    # Rewrite the question and recompute the program hash: consistent, but the signature breaks.
    b = copy.deepcopy(cert)
    b["program"]["question"] = b["question"] = "How much did we earn?"
    b["program_hash"] = cz._program_hash(b["program"])
    assert c.check_offline(b) == []                      # consistency alone cannot see it
    assert c.check_offline(b, trusted_key=pub)           # the pinned signature does
    # Re-signed by someone else's key: fails against the pinned key.
    other = sign.private_key(str(tmp_path / "other.pem"))
    assert c.check_offline(sign.sign(b, other), trusted_key=pub)


# --- third refuter pass (2026-10-26) ---

def test_no_money_in_filter_strings():
    p = prog(parts=[{"name": "refunds", "take": "gross", "where": {"code": ["T1107"]}},
                    {"name": "memo", "take": "gross", "where": {"note_contains": "Refunds were $9,999.99"}},
                    {"name": "rest", "take": "rest"}])
    assert c.certify(p, fixture(), now=NOW)["reason"] == "invalid_program"
    for key, val in (("id", ["USD 9999.99"]), ("code", ["$1,234.56"]), ("status", ["€5.00"])):
        p = prog(parts=[{"name": "x", "take": "gross", "where": {key: val}}, {"name": "rest", "take": "rest"}], answer="x")
        assert c.certify(p, fixture(), now=NOW)["reason"] == "invalid_program", key


def test_money_check_catches_unicode_and_spelled_forms():
    from closure.program import has_money
    for bad in ["＄1200", "﹩40", "₩5000", "₽100", "99¢", "12,50 EUR", ".99", "１２．５０", "12٫50", "12·50",
                "1 234 dollars", "1'500", "4 2 . 5 0", "USD 42", "42 dollars"]:
        assert has_money(bad), bad
    for q in [json.loads(l)["question"] for f in ("questions.jsonl", "dev-questions.jsonl")
              for l in open(os.path.join(os.path.dirname(__file__), "..", "eval", f))]:
        assert not has_money(q), q


def test_lone_surrogate_is_refused_not_a_crash():
    assert c.certify(prog(question="refunds \ud800"), fixture(), now=NOW)["reason"] == "invalid_program"


def test_trailing_newline_in_window_is_refused():
    assert c.certify(prog(start="2026-10-25\n"), fixture(), now=NOW)["reason"] == "invalid_program"


def test_agent_never_sees_amounts_in_refusals():
    from closure import service
    def amounts(o):
        return [x for x in json.dumps(o).replace('"', " ").split() if any(ch.isdigit() for ch in x) and "." in x]
    # Aimed left-over: claim everything but fees, no rest part.
    out = service.for_agent(c.certify(prog(answer="sales", parts=[{"name": "sales", "take": "gross"}]), fixture(), now=NOW))
    assert out["reason"] == "left_over" and amounts(out) == [], out
    assert all(set(e) <= {"id", "component"} for e in out["detail"]["examples"])
    out = service.for_agent(c.certify(prog(), Lying(copy.deepcopy(RAW), drop({"61401427AU9517523"})), now=NOW))
    assert out["reason"] == "chain_break" and amounts(out) == [], out


def test_verify_returns_no_new_figure(tmp_path, monkeypatch):
    from closure import service, sign
    monkeypatch.setattr(service, "STORE", str(tmp_path))
    monkeypatch.setattr(service, "REFUSALS", str(tmp_path / "refusals.jsonl"))
    key = sign.private_key(str(tmp_path / "k.pem"))
    monkeypatch.setattr(sign, "private_key", lambda path=None: key)
    monkeypatch.setattr(c, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    cid = service.ask(fixture(), prog())["certificate_id"]

    def edit(d):
        for r in d["transaction_details"]:
            if r["transaction_info"]["transaction_event_code"] == "T1107":
                r["transaction_info"]["transaction_amount"]["value"] = "-299.00"
    out = service.verify(fixture(edit), cid)
    assert out["holds"] is False and set(out) == {"certificate_id", "holds", "why"}
    assert "299" not in json.dumps(out)
    assert service.verify(fixture(), cid)["holds"] is True


def test_keyless_check_rejects_extra_and_inconsistent_fields():
    cert = c.certify(prog(), fixture(), now=NOW)
    def bad(f):
        b = copy.deepcopy(cert)
        f(b)
        return c.check_offline(b)
    assert bad(lambda b: b.update(summary="Refunds were 9999 dollars"))
    assert bad(lambda b: b["opening"].update(note="x"))
    assert bad(lambda b: b["answer"].update(display="$9,999.99"))
    assert bad(lambda b: b.update(indexed_through="2026-10-25T00:00:00Z"))
    assert bad(lambda b: b.update(issued_at="2020-01-01T00:00:00Z"))
    assert bad(lambda b: b["transactions"][4].update(family="payment"))
    assert bad(lambda b: b["transactions"][0].update(seq=3))
