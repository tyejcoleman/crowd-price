"""PayPal Transaction Search records -> plain events. Money is Decimal, never float."""
import hashlib, json
from dataclasses import dataclass, asdict
from decimal import Decimal

from .paypal import parse_time

# Event-code families from PayPal's Transaction Search reference. Used only to label parts for
# people; the certifier never depends on these names.
FAMILIES = {
    "T00": "payment", "T01": "non-payment fee", "T02": "currency conversion", "T03": "bank deposit",
    "T04": "bank withdrawal", "T05": "debit card", "T06": "credit card withdrawal", "T07": "credit card deposit",
    "T08": "bonus", "T09": "incentive", "T10": "bill pay", "T11": "reversal / refund", "T12": "adjustment",
    "T13": "authorization", "T14": "dividend", "T15": "hold / release", "T16": "buyer credit",
    "T17": "non-bank withdrawal", "T18": "buyer credit withdrawal", "T19": "account correction",
    "T20": "funds transfer", "T21": "reserve / release", "T22": "transfer", "T30": "generic", "T50": "general",
    "T97": "PayPal-initiated", "T98": "other", "T99": "other",
}


@dataclass(frozen=True)
class Event:
    id: str
    seq: int            # order PayPal returned it (tiebreak for same-second records)
    at: str             # initiation time, ISO UTC
    code: str           # transaction_event_code, e.g. T0005
    status: str         # S success, P pending, V reversed, D denied, F partially refunded
    currency: str
    gross: Decimal      # transaction_amount (signed)
    fee: Decimal        # fee_amount (signed; usually <= 0)
    ending_balance: Decimal | None
    note: str = ""      # invoice id / subject, for people only

    @property
    def net(self):
        return self.gross + self.fee

    @property
    def family(self):
        return FAMILIES.get(self.code[:3], "unknown")

    def public(self):
        d = asdict(self)
        d.pop("seq")
        for k in ("gross", "fee", "ending_balance"):
            d[k] = None if d[k] is None else str(d[k])
        d["family"] = self.family
        return d


def _money(obj):
    return Decimal(obj["value"]) if obj and obj.get("value") not in (None, "") else Decimal("0")


def from_paypal(records):
    events = []
    for seq, r in enumerate(records):
        i = r.get("transaction_info") or {}
        amt = i.get("transaction_amount") or {}
        eb = i.get("ending_balance")
        events.append(Event(
            id=i["transaction_id"], seq=seq,
            at=parse_time(i["transaction_initiation_date"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
            code=i.get("transaction_event_code", ""), status=i.get("transaction_status", ""),
            currency=amt.get("currency_code", ""), gross=_money(amt), fee=_money(i.get("fee_amount")),
            ending_balance=_money(eb) if eb else None,
            note=i.get("invoice_id") or i.get("transaction_subject") or "",
        ))
    # Stable: by time, then PayPal's own order within the same second.
    return sorted(events, key=lambda e: (e.at, e.seq))


def digest(events):
    """Hash of the exact records a certificate was computed from (order-sensitive)."""
    rows = [[e.id, e.at, e.code, e.status, e.currency, str(e.gross), str(e.fee),
             None if e.ending_balance is None else str(e.ending_balance), e.note] for e in events]
    return "sha256:" + hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
