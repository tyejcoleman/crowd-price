// The maker's view: the drop as the engine sees it, what needs the maker, and the read-only checks against PayPal.
// It cannot approve or settle anything: approval is the maker's own, in their terminal, with their passphrase.
"use strict";
(() => {
  const { el, api, showMode, money, plural, clockFrom, duration, hhmm, ladder, store } = CP;
  const app = document.getElementById("app");
  const DROP_ID = /^[A-Za-z0-9_-]{1,64}$/;
  const KEYSTORE = "crowd-price:maker-key";

  // The key arrives in the link's fragment; it is kept for this tab only and taken out of the address bar at once, so it
  // never sits in the history or on screen.
  const params = () => new URLSearchParams(location.hash.slice(1));
  const fromLink = params().get("key");
  if (fromLink) store(KEYSTORE, fromLink, sessionStorage);
  const s = { key: store(KEYSTORE, undefined, sessionStorage) || "", drop: "", drops: [], d: null, error: null, mode: null,
              checks: {}, busy: {}, clock: null, copied: null, lastRender: "" };
  const wanted = params().get("drop");
  if (wanted && DROP_ID.test(wanted)) s.drop = wanted;
  setHash();

  function setHash() {
    history.replaceState(null, "", s.drop ? "#drop=" + encodeURIComponent(s.drop) : location.pathname);
  }
  const hdr = () => ({ "X-Maker-Key": s.key });
  const path = () => `/api/maker/drops/${encodeURIComponent(s.drop)}`;

  async function tick() {
    try {
      if (!s.mode) { s.mode = (await api("/api/info")).paypal; showMode(s.mode); }
      s.drops = await api("/api/drops");
      if (!s.drop && s.drops.length) { s.drop = s.drops[0].drop; setHash(); }
      if (s.drop && s.key) s.d = await api(path(), { headers: hdr() });
      s.error = null;
    } catch (e) {
      s.error = e;
      if (e.code === "maker_key") { s.key = ""; store(KEYSTORE, null, sessionStorage); }
    }
    render();
  }

  // ---- what the approval asks for: capture:45:37.00:1665.00, void:22, either +release:K. This is the plan the approval is
  // bound to, as the assistant asked for it; settle checks it against the drop again and refuses if it changed.
  function plan(expect) {
    const m = /^capture:(\d+):([\d.]+):([\d.]+)/.exec(expect || "");
    const r = /\+release:(\d+)/.exec(expect || "");
    const extra = r ? ` Also releases ${plural(+r[1], "failed hold")}.` : "";
    if (m) return { what: `Charge ${plural(+m[1], "buyer")} ${money(m[2])} each`, total: `${money(m[3])} in total.${extra}` };
    const v = /^void:(\d+)/.exec(expect || "");
    if (v) return { what: `Release all ${plural(+v[1], "hold")}`, total: `Nobody pays.${extra}` };
    return { what: "Settle this drop", total: "" };
  }

  function headline(d) {
    const st = d.settlement, name = `Your ${d.title} drop`;
    let claim = `${name} is ${d.state}.`, turn;
    if (d.blocked) { claim = `Settling ${d.title} stopped.`; turn = "It needs you."; }
    else if (d.state === "live") turn = d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.`
                                                       : `${d.buyers} of ${d.minimum} joined. Everyone pays ${money(d.price)} once ${d.minimum} join.`;
    else if (d.state === "ended") turn = d.minimum_met ? "Time is up. Your assistant closes it next." : "Time is up, and it missed its minimum.";
    else if (d.state === "closed") turn = d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.` : `${d.buyers} of ${d.minimum} joined. Every hold will be released.`;
    else if (d.state === "settling") turn = st && st.outcome === "funded" ? `Charging everyone ${money(st.final_price)}.` : "Releasing every hold.";
    else if (st && st.outcome === "funded") turn = `${plural(st.counts.captured, "buyer")} paid ${money(st.final_price)}.`;
    else if (st) { claim = `${name} missed its minimum.`; turn = `${plural(st.counts.voided, "hold")} released. Nobody paid.`; }
    return el("h1", { class: "two" }, el("span", { text: claim }), " ", el("span", { class: "turn", text: turn }));
  }

  // ---- the card that says what needs the maker, if anything
  function needs(d) {
    const pending = (d.approvals || []).find(a => a.status === "pending");
    const approved = (d.approvals || []).find(a => a.status === "approved");
    const st = d.settlement;
    const kids = [];
    const state = (text, cls) => el("div", { class: "state" + (cls ? " " + cls : ""), text });
    if (d.blocked) {
      kids.push(state("Settlement stopped", "bad"), el("p", { class: "what", text: `PayPal stopped it: ${d.blocked.words}` }),
        el("p", { text: "Nothing more is charged until it is sorted out. Ask your assistant to explain the drop; it names the buyer and the way out." }),
        d.blocked.issue ? el("p", { class: "fine mono", text: `PayPal code: ${d.blocked.issue}` }) : null);
    } else if (pending && d.state !== "settled") {
      const p = plan(pending.expect);
      kids.push(state("Needs your approval", "ask"), el("p", { class: "what", text: p.what }), el("p", { class: "muted", text: p.total }),
        el("p", { text: "Your assistant asked to settle. Only you can approve it, in your own terminal; your assistant cannot." }),
        el("div", { class: "checks" },
          el("button", { class: "btn inline copycmd", onclick: () => copy(`togi approve ${pending.id}`, pending.id), text: s.copied === pending.id ? "Copied" : "Copy the approval command" }),
          el("span", { class: "reqid mono", text: `request ${pending.id}` })),
        el("p", { class: "fine", text: `Paste it in your terminal. It asks for your passphrase, which the assistant never sees.${pending.expires ? ` The request lapses at ${hhmm(pending.expires)}.` : ""} If the drop changes first, settling refuses and your assistant asks again.` }));
    } else if (approved && !st) {
      kids.push(state("Approved"), el("p", { class: "what", text: plan(approved.expect).what }), el("p", { text: "You approved it. Your assistant settles next." }));
    } else if (d.state === "settling") {
      kids.push(state("Settling"), el("p", { class: "what", text: st && st.outcome === "funded" ? `Charging ${money(st.final_price)} each` : "Releasing every hold" }));
    } else if (d.state === "settled") {
      const t = st.totals || {}, c = st.counts || {};
      const ok = s.mode === "fake" ? "Every hold and capture matches the pretend PayPal." : "Every hold and capture matches PayPal.";
      kids.push(state("Settled"),
        el("p", { class: "what", text: st.outcome === "funded" ? `${plural(c.captured, "buyer")} paid ${money(st.final_price)}` : "Nobody paid" }),
        el("dl", { class: "facts" },
          st.outcome === "funded" ? [
            el("div", {}, el("dt", { text: "Charged" }), el("dd", { class: "v", text: money(t.gross) })),
            el("div", {}, el("dt", { text: "PayPal fees" }), el("dd", { class: "v", text: money(t.fees) })),
            el("div", {}, el("dt", { text: "Paid to you, before your costs" }), el("dd", { class: "v", text: money(t.net) })),
          ] : null,
          el("div", {}, el("dt", { text: "Holds released" }), el("dd", { class: "v", text: String(c.voided || 0) })),
          c.failed ? el("div", {}, el("dt", { class: "stop", text: "Charges failed" }), el("dd", { class: "v stop", text: String(c.failed) })) : null),
        el("div", { class: "checks" },
          el("button", { class: "btn quiet inline", disabled: s.busy.verify, onclick: () => check("verify"), text: s.busy.verify ? "Asking PayPal…" : "Check with PayPal" }),
          el("button", { class: "btn quiet inline", disabled: s.busy.tieout, onclick: () => check("tieout"), text: s.busy.tieout ? "Reading the balance…" : "Match PayPal's balance" })),
        resultVerify(ok), resultTieout());
    } else if (d.state === "closed") {
      kids.push(state("Closed"), el("p", { class: "what", text: d.minimum_met ? `Ready to charge ${plural(d.buyers, "buyer")} ${money(d.price)}` : `Ready to release ${plural(d.buyers, "hold")}` }),
        el("p", { text: "Your assistant asks you to approve the settlement. The request shows up here." }));
    } else if (d.state === "ended") {
      kids.push(state("Time is up"), el("p", { class: "what", text: `${plural(d.buyers, "buyer")} joined` }), el("p", { text: "Your assistant closes the drop next. Nobody can join now." }));
    } else {
      kids.push(state("Nothing needs you"), el("p", { class: "what", text: `${plural(d.buyers, "buyer")} so far` }),
        el("p", { text: "Your assistant runs the drop. When it closes, you approve the settlement in your terminal. Nothing is charged before that." }));
    }
    return el("div", { class: "card needs" }, kids);
  }

  function resultVerify(ok) {
    const r = s.checks.verify;
    if (!r) return null;
    if (r.error) return el("div", { class: "result stop", text: r.error });
    if (r.ok) return el("div", { class: "result" }, el("span", { class: "go", text: ok + " " }), el("span", { class: "muted", text: `Checked ${hhmm(r.checked_at)}.` }));
    return el("div", { class: "result" }, el("span", { class: "stop", text: "PayPal disagrees with the record:" }),
      el("ul", {}, ((r.report || {}).problems || []).slice(0, 6).map(p => el("li", { text: p }))));
  }

  function resultTieout() {
    const r = s.checks.tieout;
    if (!r) return null;
    if (r.error) return el("div", { class: "result stop", text: r.error });
    const rep = r.report || {};
    if (rep.kind === "certificate") {
      return el("div", { class: "result" }, rep.matches_engine
        ? el("span", { class: "go", text: `PayPal's balance moved by exactly this drop's ${money(rep.engine_net)}, to the cent.` })
        : el("span", { class: "stop", text: `PayPal's balance does not match this drop's ${money(rep.engine_net)}.` }));
    }
    const why = { no_transaction_search: "The pretend PayPal keeps no balance history. Run the drop in the sandbox to match PayPal's balance.",
                  not_yet_indexed: "PayPal has not indexed these charges yet. It can take a few hours; try again later.",
                  nothing_captured: "Nothing was charged, so there is no balance change to match." }[rep.reason];
    return el("div", { class: "result muted", text: why || rep.what_to_do || "No answer yet." });
  }

  async function check(which) {
    s.busy[which] = true; render();
    try { s.checks[which] = await api(`${path()}/${which}`, { method: "POST", headers: hdr(), body: {} }); }
    catch (e) { s.checks[which] = { error: e.message }; }
    finally { s.busy[which] = false; render(); }
  }

  async function copy(text, id) {
    try { await navigator.clipboard.writeText(text); s.copied = id; render(); } catch (e) { /* the command is on screen */ }
  }

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
      el("ul", { class: "buyers" }, (d.buyers_list || []).map(b =>
        el("li", {}, el("bdi", { text: b.name }),
          el("span", { class: "st" + (BAD.has(kind(b)) ? " bad" : ""), text: `${STATUS[kind(b)] || kind(b)}${b.charged ? " " + money(b.charged) : (b.held ? " " + money(b.held) : "")}` })))));
  }

  // Lines that say a hold may still be open, or was charged outside the drop, are drawn like any hold PayPal would not release.
  const ATTENTION = new Set(["left_held", "taken_outside"]);
  function feed(d) {
    return el("div", { class: "panel" }, el("h3", { text: "Activity" }),
      el("ul", { class: "feed" }, (d.feed || []).map(f => el("li", { class: "k-" + f.kind + (ATTENTION.has(f.kind) ? " k-void_failed" : "") },
        el("time", { datetime: f.at, text: hhmm(f.at) }),
        el("span", {}, f.who ? [el("bdi", { class: "who", text: f.who }), " "] : null, f.text)))));
  }

  function profitRow(d) {
    if (!d.profit || !d.profit.length) return null;
    const now = d.locked_step === null || d.locked_step === undefined ? -1 : d.locked_step;
    return el("p", { class: "profitrow" }, el("span", { class: "k", text: "Profit after your costs" }),
      d.profit.map((r, i) => el("span", { class: i === now ? "now" : "" }, `${r.from_buyers} buyers `, el("span", { class: "v", text: money(r.profit) }))));
  }

  function keyForm() {
    const input = el("input", { placeholder: "Maker key", autocomplete: "off" });
    return el("div", { class: "card keyform" }, el("h2", { text: "Maker key" }),
      el("p", { class: "muted", text: "The server prints a maker link with the key when it starts. Open that link, or paste the key here." }),
      s.error && s.error.code === "slow_down" ? el("p", { class: "msg stop", text: s.error.message }) : null,
      el("form", { onsubmit: e => { e.preventDefault(); s.key = input.value.trim(); store(KEYSTORE, s.key, sessionStorage); s.lastRender = ""; tick(); } },
        el("label", { class: "field" }, input), el("button", { class: "btn", text: "Open" })));
  }

  function render(force) {
    const CLOCK = new Set(["server_time", "time_left", "time_left_seconds"]);   // the clock ticks on its own; it is not a change
    const key = JSON.stringify([s.key ? 1 : 0, s.d, s.checks, s.busy, s.copied, s.error && s.error.code, s.drops.map(x => x.drop + x.state)],
                               (k, v) => (CLOCK.has(k) ? undefined : v));
    if (!force && key === s.lastRender) return;
    s.lastRender = key;
    if (!s.key || (s.error && ["maker_key", "slow_down"].includes(s.error.code))) { app.replaceChildren(keyForm()); return; }
    if (!s.d) { app.replaceChildren(el("p", { class: "status", text: s.error ? s.error.message : (s.drops.length ? "Loading…" : "No drops yet. Your assistant opens one.") })); return; }
    const d = s.d;
    document.title = `${d.title} · maker · Crowd Price`;
    s.clock = clockFrom(d);
    const pick = s.drops.length > 1 ? el("select", { onchange: e => { if (!DROP_ID.test(e.target.value)) return; s.drop = e.target.value; s.d = null; s.checks = {}; setHash(); tick(); } },
      s.drops.map(x => el("option", { value: x.drop, selected: x.drop === s.drop, text: `${x.title} · ${x.state}` }))) : null;
    const clock = d.state === "live" && s.clock ? el("div", { class: "clock" }, el("div", { class: "label", text: "Joining closes in" }),
      el("div", { class: "t", id: "clock", text: duration(s.clock()) })) : null;
    const ladderHost = el("div", { id: "ladder" });
    const final = d.settled && d.settled.outcome === "funded";
    const left = el("div", {},
      el("div", { class: "panel" }, el("h3", {}, el("span", { text: "Price ladder" }),
        el("a", { class: "counts", href: `/d/${encodeURIComponent(d.drop)}`, target: "_blank", rel: "noopener", text: "buyer page ↗" })),
        ladderHost,
        el("p", { class: "caption" }, el("span", {}, el("span", { class: "n", text: String(d.buyers) }), " joined"),
          el("span", {}, "minimum ", el("span", { class: "n", text: String(d.minimum) })),
          el("span", {}, final ? "final price " : (d.settled ? "start price " : "price now "),
            el("span", { class: "n", text: money(final ? d.settled.final_price : (d.settled ? d.start_price : d.price)) }))),
        profitRow(d)),
      feed(d));
    const right = el("div", {}, needs(d), buyers(d));
    const keep = document.getElementById("ladder");
    if (keep) ladderHost._prev = keep._prev;
    app.replaceChildren(el("div", { class: "mhead" }, el("div", {}, pick ? el("div", { class: "by" }, pick) : null, headline(d)), clock),
      el("div", { class: "mgrid" }, left, right));
    requestAnimationFrame(() => ladder(ladderHost, d));
  }

  setInterval(() => { const c = document.getElementById("clock"); if (c && s.d && s.d.state === "live" && s.clock) c.textContent = duration(s.clock()); }, 1000);
  window.addEventListener("resize", () => { if (s.d) render(true); });
  window.addEventListener("hashchange", () => {        // a link to another drop (or with a key) opened in this tab
    const p = params(), k = p.get("key"), dr = p.get("drop");
    if (k) { s.key = k; store(KEYSTORE, k, sessionStorage); }
    if (dr && DROP_ID.test(dr) && dr !== s.drop) { s.drop = dr; s.d = null; s.checks = {}; }
    setHash();
    tick();
  });
  (function loop() { tick().finally(() => setTimeout(loop, 1500)); })();
})();
