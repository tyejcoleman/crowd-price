// Crowd Price: the maker's panel, shared by the maker view (maker.js) and the studio (studio.js). The headline, the card
// that says what needs the maker, the buyers, the activity, the profit row and the read-only checks with PayPal. Every
// amount is a string the server sent (the engine's, or Togi's); this file only lays it out. A drop's title is the
// assistant's words: it is always drawn in its own isolated element, cut short, never as part of a sentence's markup.
"use strict";
const CPPanel = (() => {
  const { el, money, plural, hhmm } = CP;
  const TITLE_MAX = 60;

  // the drop's title, as text in its own bidi-isolated element, cut at TITLE_MAX characters; a character that moves or hides
  // text (a control, a bidi override or isolate, a zero-width one) is shown as its code, never applied, and a run of combining
  // marks is cut to two (a stack of forty draws over the lines around it)
  const HIDDEN = /[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]/g;
  const MARKS = /(\p{M}{2})\p{M}+/gu;
  const shown = t => String(t || "").normalize("NFC").replace(HIDDEN, c => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0")).replace(MARKS, "$1");
  function title(d, max) {
    const t = shown(d.title);
    const n = max || TITLE_MAX;
    return el("bdi", { class: "title", text: t.length > n ? t.slice(0, n - 1) + "…" : t });
  }

  // what an approval asks for: capture:45:37.00:1665.00, void:22, either +release:K. This is the plan the approval is bound
  // to; settle checks it against the drop again and refuses if it changed. Only that exact form is read: anything else (a
  // space around it, a second +release) is "cannot be read" and says nothing about money. (The studio server refuses to show
  // such a request at all; this is the page's own guard.)
  const PLAN = /^(?:capture:(\d+):(\d+\.\d{2}):(\d+\.\d{2})|void:(\d+))(?:\+release:(\d+))?$/;
  function plan(expect) {
    const m = PLAN.exec(typeof expect === "string" ? expect : "");
    if (!m) return { kind: "other", what: "This request cannot be read", sum: "", totalLine: "", extra: "", signed: "" };
    const extra = m[5] !== undefined ? ` Also releases ${plural(+m[5], "failed hold")}.` : "";
    if (m[1] !== undefined) return { kind: "capture", n: +m[1], price: m[2], total: m[3], what: `Charge ${plural(+m[1], "buyer")} ${money(m[2])} each`,
                    sum: `${m[1]} × ${money(m[2])} = ${money(m[3])}`, totalLine: `${money(m[3])} in total.${extra}`, extra,
                    signed: "Settle the drop: charge every hold at the final price" };
    return { kind: "void", n: +m[4], what: `Release all ${plural(+m[4], "hold")}`, sum: `${plural(+m[4], "hold")} released, nobody pays`,
             totalLine: `Nobody pays.${extra}`, extra, signed: "Settle the drop: release every hold" };
  }

  // a request for money is waiting for the person (the drop is not settled and an approval is pending)
  const waiting = d => d.state !== "settled" && (d.approvals || []).some(a => a.status === "pending");

  // the headline: what the drop is doing (the claim), and what that means now (the turn, on a line of its own whose height
  // is kept, so the headline does not jump when the minimum is reached)
  // While a request is waiting, the headline does not carry the assistant's name for the drop: it is the assistant's words in the
  // largest type on the page, beside the request, and could read as a sentence about the money. The name is on the card, quoted
  // and labelled as the assistant's.
  function headline(d) {
    const st = d.settlement;
    const hide = waiting(d);
    const name = () => hide ? ["Your drop"] : ["Your ", title(d), " drop"];
    let claim = [...name(), ` is ${d.state}.`], turn = "";
    if (d.draft) { claim = [...name(), " is being set up."]; turn = "Buyers can join once it opens."; }
    else if (d.blocked) { claim = hide ? ["Settling your drop stopped."] : ["Settling ", title(d), " stopped."]; turn = "It needs you."; }
    else if (d.state === "live") turn = d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.`
                                                       : `${d.buyers} of ${d.minimum} joined. Everyone pays ${money(d.price)} once ${d.minimum} join.`;
    else if (d.state === "ended") turn = d.minimum_met ? "Time is up. Your assistant closes it next." : "Time is up, and it missed its minimum.";
    else if (d.state === "closed") turn = d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.` : `${d.buyers} of ${d.minimum} joined. Every hold will be released.`;
    else if (d.state === "settling") turn = st && st.outcome === "funded" ? `Charging everyone ${money(st.final_price)}.` : "Releasing every hold.";
    else if (st && st.outcome === "funded") turn = `${plural(st.counts.captured, "buyer")} paid ${money(st.final_price)}.`;
    else if (st) { claim = [...name(), " missed its minimum."]; turn = `${plural(st.counts.voided, "hold")} released. Nobody paid.`; }
    return el("h1", { class: "two s-" + (d.draft ? "draft" : d.state) }, el("span", { class: "claim" }, claim), " ", el("span", { class: "turn", text: turn }));
  }

  // ---- the card that says what needs the maker. `o.ask(d, pending)` draws a waiting approval (the maker view: the terminal
  // command; the studio: its approval desk); `o.where` says where approving happens ("in your terminal", "here").
  function needs(d, o) {
    const pending = d.state !== "settled" ? (d.approvals || []).find(a => a.status === "pending") : null;
    const approved = (d.approvals || []).find(a => a.status === "approved");
    const st = d.settlement;
    const kids = [];
    const state = (text, cls) => el("div", { class: "state" + (cls ? " " + cls : ""), text });
    if (d.draft) {
      kids.push(state("Setting up"), el("p", { class: "what", text: "Not open yet" }), el("p", { text: "Your assistant opens the drop to buyers next." }));
    } else if (d.blocked) {
      kids.push(state("Settlement stopped", "bad"), el("p", { class: "what", text: `PayPal stopped it: ${d.blocked.words}` }),
        el("p", { text: "Nothing more is charged until it is sorted out. Ask your assistant to explain the drop; it names the buyer and the way out." }),
        d.blocked.issue ? el("p", { class: "fine mono", text: `PayPal code: ${d.blocked.issue}` }) : null);
    } else if (pending) {
      kids.push(...o.ask(d, pending));
    } else if (approved && !st) {
      kids.push(state("Approved"), el("p", { class: "what", text: plan(approved.expect).what }), el("p", { text: "You approved it. Your assistant settles next." }));
    } else if (d.state === "settling") {
      kids.push(state("Settling"), el("p", { class: "what", text: st && st.outcome === "funded" ? `Charging ${money(st.final_price)} each` : "Releasing every hold" }));
    } else if (d.state === "settled") {
      kids.push(...settled(d, o));
    } else if (d.state === "closed") {
      kids.push(state("Closed"), el("p", { class: "what", text: d.minimum_met ? `Ready to charge ${plural(d.buyers, "buyer")} ${money(d.price)}` : `Ready to release ${plural(d.buyers, "hold")}` }),
        el("p", { text: `Your assistant asks you to approve the settlement. The request shows up ${o.where === "here" ? "here" : "here, and you approve it in your terminal"}.` }));
    } else if (d.state === "ended") {
      kids.push(state("Time is up"), el("p", { class: "what", text: `${plural(d.buyers, "buyer")} joined` }), el("p", { text: "Your assistant closes the drop next. Nobody can join now." }));
    } else {
      kids.push(state("Nothing needs you"), el("p", { class: "what", text: `${plural(d.buyers, "buyer")} so far` }),
        el("p", { text: `Your assistant runs the drop. When it closes, you approve the charge ${o.where === "here" ? "here" : "in your terminal"}. Nothing is charged before that.` }));
    }
    return el("div", { class: "card needs" + (pending || d.blocked ? " urgent" : ""), id: "needs" }, kids);
  }

  function settled(d, o) {
    const st = d.settlement, t = st.totals || {}, c = st.counts || {};
    const pn = d.profit_now && d.profit_now.settled ? d.profit_now : null;
    const ok = o.mode === "fake" ? "Every hold and capture matches the test-mode payments." : "Every hold and capture matches PayPal.";
    const row = (k, v, cls) => el("div", { class: cls || null }, el("dt", { text: k }), el("dd", { class: "v", text: v }));
    return [el("div", { class: "state", text: "Settled" }),
      el("p", { class: "what", text: st.outcome === "funded" ? `${plural(c.captured, "buyer")} paid ${money(st.final_price)}` : "Nobody paid" }),
      el("dl", { class: "facts" },
        st.outcome === "funded" ? [row("Charged", money(t.gross)), row("PayPal fees", money(t.fees)), row("Paid to you, before your costs", money(t.net)),
                                   pn ? row("After your costs", money(pn.profit), "keep") : null] : null,
        row("Holds released", String(c.voided || 0)),
        c.failed ? el("div", {}, el("dt", { class: "stop", text: "Charges failed" }), el("dd", { class: "v stop", text: String(c.failed) })) : null),
      el("div", { class: "checks" },
        el("button", { class: "btn quiet inline", type: "button", disabled: !!o.busy.verify, onclick: () => o.check("verify"), text: o.busy.verify ? "Asking PayPal…" : "Check with PayPal" }),
        o.tieout ? el("button", { class: "btn quiet inline", type: "button", disabled: !!o.busy.tieout, onclick: () => o.check("tieout"), text: o.busy.tieout ? "Reading the balance…" : "Match PayPal's balance" }) : null),
      resultVerify(o.checks.verify, ok), o.tieout ? resultTieout(o.checks.tieout) : null];
  }

  function resultVerify(r, ok) {
    if (!r) return null;
    if (r.error) return el("div", { class: "result stop", text: r.error });
    if (r.ok) return el("div", { class: "result" }, el("span", { class: "go", text: ok + " " }), el("span", { class: "muted", text: `Checked ${hhmm(r.checked_at)}.` }));
    return el("div", { class: "result" }, el("span", { class: "stop", text: "PayPal disagrees with the record:" }),
      el("ul", {}, ((r.report || {}).problems || []).slice(0, 6).map(p => el("li", { text: p }))));
  }

  function resultTieout(r) {
    if (!r) return null;
    if (r.error) return el("div", { class: "result stop", text: r.error });
    const rep = r.report || {};
    if (rep.kind === "certificate") {
      return el("div", { class: "result" }, rep.matches_engine
        ? el("span", { class: "go", text: `PayPal's balance moved by exactly this drop's ${money(rep.engine_net)}, to the cent.` })
        : el("span", { class: "stop", text: `PayPal's balance does not match this drop's ${money(rep.engine_net)}.` }));
    }
    const why = { no_transaction_search: "Test mode keeps no balance history. Run the drop in the PayPal sandbox to match PayPal's balance.",
                  not_yet_indexed: "PayPal has not indexed these charges yet. It can take a few hours; try again later.",
                  nothing_captured: "Nothing was charged, so there is no balance change to match." }[rep.reason];
    return el("div", { class: "result muted", text: why || rep.what_to_do || "No answer yet." });
  }

  // ---- the buyers: what became of each hold, as the engine's log says it
  const STATUS = { held: "held", captured: "paid", left: "left", voided: "released", capture_failed: "charge failed",
                   hold_failed: "declined", void_failed: "release failed", excluded: "taken out",
                   release_stuck: "a hold may still be open", not_confirmed: "never confirmed, nothing held",
                   left_open: "left, hold not released", excluded_open: "taken out, hold not released",
                   lapsed: "hold ran out", gone: "hold gone at PayPal", taken_outside: "charged outside the drop",
                   excluded_taken: "taken out, charged outside" };
  const BAD = new Set(["capture_failed", "hold_failed", "void_failed", "release_stuck", "left_open", "excluded_open", "taken_outside", "excluded_taken"]);
  // The engine files three different things under "hold_failed" and says which in `error`: a card or PayPal refusing the join (declined);
  // a join nobody confirmed that PayPal holds nothing for, or that was released (never confirmed: nothing is held and nobody was refused);
  // and a join whose hold PayPal would not release or describe (a hold may be open on the buyer's card: the maker has to look; the server
  // says so in `hold_may_be_open`). A buyer who is out of the crowd has a `fate`, what became of the hold, from the engine's log: a leave
  // or a removal PayPal would not release is not "left" or "taken out", and a hold that was charged outside the drop is not "released".
  const BY_FATE = { "left:not_released": "left_open", "excluded:not_released": "excluded_open", "excluded:taken_outside": "excluded_taken",
                    "voided:lapsed": "lapsed", "voided:gone": "gone", "voided:taken_outside": "taken_outside" };
  function kind(b) {
    if (b.status !== "hold_failed") return BY_FATE[b.status + ":" + b.fate] || b.status;
    if (b.hold_may_be_open) return "release_stuck";
    if (!b.error || b.error === "join_unconfirmed") return "not_confirmed";
    return "hold_failed";
  }

  function buyers(d) {
    const tally = {};
    for (const b of d.buyers_list || []) tally[kind(b)] = (tally[kind(b)] || 0) + 1;
    const counts = Object.entries(tally).map(([k, n]) => `${n} ${STATUS[k] || k}`);
    if (d.simulated) counts.push(`${d.simulated} simulated`);
    return el("div", { class: "panel buyers-panel" },
      el("h3", {}, el("span", { text: "Buyers" }), el("span", { class: "counts", text: counts.join(" · ") })),
      (d.buyers_list || []).length ? el("ul", { class: "buyers" }, d.buyers_list.map(b =>
        el("li", {}, el("bdi", { text: b.name }),
          el("span", { class: "st" + (BAD.has(kind(b)) ? " bad" : ""), text: `${STATUS[kind(b)] || kind(b)}${b.charged ? " " + money(b.charged) : (b.held ? " " + money(b.held) : "")}` }))))
        : el("p", { class: "muted small", text: "Nobody yet." }));
  }

  // Lines that say a hold may still be open, or was charged outside the drop, are drawn like any hold PayPal would not release.
  const ATTENTION = new Set(["left_held", "taken_outside"]);
  function feed(d) {
    return el("div", { class: "panel" }, el("h3", { text: "Activity" }),
      (d.feed || []).length ? el("ul", { class: "feed" }, d.feed.map(f => el("li", { class: "k-" + f.kind + (ATTENTION.has(f.kind) ? " k-void_failed" : "") },
        el("time", { datetime: f.at, text: hhmm(f.at) }),
        el("span", {}, f.who ? [el("bdi", { class: "who", text: f.who }), " "] : null, f.text))))
        : el("p", { class: "muted small", text: "Nothing yet." }));
  }

  // The profit at each step is at exactly that step's count; the green figure is the profit NOW, at the crowd's own count
  // (the server's `profit_now`), never a step's.
  function profitRow(d) {
    if (!d.profit || !d.profit.length) return null;
    const pn = d.profit_now;
    return el("div", { class: "profit" },
      pn ? el("p", { class: "profitnow" }, el("span", { class: "k", text: pn.settled ? "After your costs, with " : "Profit now, at " }),
        `${plural(pn.buyers, "buyer")}: `, el("span", { class: "v", text: money(pn.profit) })) : null,
      el("p", { class: "profitrow" }, el("span", { class: "k", text: "At each step" }),
        d.profit.map(r => el("span", {}, `${r.from_buyers} buyers `, el("span", { class: "v", text: money(r.profit) })))));
  }

  return { title, shown, plan, waiting, headline, needs, resultVerify, resultTieout, STATUS, BAD, kind, buyers, feed, profitRow };
})();
