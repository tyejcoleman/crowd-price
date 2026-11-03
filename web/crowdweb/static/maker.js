// The maker's view: the drop as the engine sees it, what needs the maker, and the read-only checks against PayPal.
// It cannot approve or settle anything: approval is the maker's own `togi approve`, in their terminal.
"use strict";
(() => {
  const { el, api, money, plural, clockFrom, duration, hhmm, ladder } = CP;
  const app = document.getElementById("app");
  const params = () => new URLSearchParams(location.hash.slice(1));
  const s = { key: params().get("key") || "", drop: params().get("drop") || "", drops: [], d: null, error: null,
              checks: {}, busy: {}, clock: null, lastLadder: "", copied: null };

  const hdr = () => ({ "X-Maker-Key": s.key });

  function setHash() {
    const p = new URLSearchParams();
    if (s.key) p.set("key", s.key);
    if (s.drop) p.set("drop", s.drop);
    history.replaceState(null, "", "#" + p.toString());
  }

  async function tick() {
    try {
      s.drops = await api("/api/drops");
      if (!s.drop && s.drops.length) { s.drop = s.drops[0].drop; setHash(); }
      if (s.drop) s.d = await api(`/api/maker/drops/${s.drop}`, { headers: hdr() });
      s.error = null;
    } catch (e) {
      s.error = e;
    }
    render();
  }

  // ---- what the token means: capture:45:37.00:1665.00, void:22, either +release:K
  function plan(expect) {
    const m = /^capture:(\d+):([\d.]+):([\d.]+)/.exec(expect || "");
    const r = /\+release:(\d+)/.exec(expect || "");
    const extra = r ? ` and release ${plural(+r[1], "failed hold")}` : "";
    if (m) return { what: `Charge ${plural(+m[1], "buyer")} ${money(m[2])} each`, total: `${money(m[3])} in total${extra}` };
    const v = /^void:(\d+)/.exec(expect || "");
    if (v) return { what: `Release all ${plural(+v[1], "hold")}`, total: `nobody pays${extra}` };
    return { what: "Settle this drop", total: "" };
  }

  function headline(d) {
    const st = d.settlement;
    let claim = `${d.title} is ${d.state}.`, turn;
    if (d.state === "live") turn = `${d.buyers} joined, minimum ${d.minimum} ${d.minimum_met ? "met" : "not met yet"}. Everyone pays ${money(d.price)}.`;
    else if (d.state === "ended") turn = "Time is up. Your assistant closes it next.";
    else if (d.state === "closed") turn = d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.` : `${d.buyers} of ${d.minimum} joined. Every hold will be released.`;
    else if (d.state === "settling") turn = st && st.outcome === "funded" ? `Charging everyone ${money(st.final_price)}.` : "Releasing every hold.";
    else if (st && st.outcome === "funded") turn = `${plural(st.counts.captured, "buyer")} paid ${money(st.final_price)}.`;
    else if (st) { claim = `${d.title} missed its minimum.`; turn = `${plural(st.counts.voided, "hold")} released. Nobody paid.`; }
    return el("h1", { class: "two" }, el("span", { text: claim }), " ", el("span", { class: "turn", text: turn }));
  }

  // ---- the card that says what needs the maker, if anything
  function needs(d) {
    const pending = (d.approvals || []).find(a => a.status === "pending");
    const approved = (d.approvals || []).find(a => a.status === "approved");
    const st = d.settlement;
    const kids = [];
    const state = (text, ask) => el("div", { class: "state" + (ask ? " ask" : ""), text });
    if (d.blocked) {
      kids.push(state("Settlement stopped", true), el("p", { class: "what", text: "PayPal refused a step" }),
        el("p", { text: `PayPal said: ${d.blocked.issue || "a refusal"}. Nothing more is charged until it is sorted out. Ask your assistant to explain the drop.` }));
    } else if (pending && d.state !== "settled") {
      const p = plan(pending.expect);
      kids.push(state("Needs your approval", true), el("p", { class: "what", text: p.what }), el("p", { class: "muted", text: p.total }),
        el("p", { text: "Your assistant asked to settle. Only you can approve it, in your own terminal. It cannot approve for you." }),
        el("div", { class: "cmd" }, el("span", { text: `togi approve ${pending.id}` }),
          el("button", { onclick: () => copy(`togi approve ${pending.id}`), text: s.copied === pending.id ? "copied" : "copy" })),
        pending.expires ? el("p", { class: "small muted", text: `The request lapses at ${hhmm(pending.expires)} if no one approves it.` }) : null);
    } else if (approved && !st) {
      kids.push(state("Approved"), el("p", { class: "what", text: plan(approved.expect).what }), el("p", { text: "You approved it. Your assistant settles next." }));
    } else if (d.state === "settling") {
      kids.push(state("Settling"), el("p", { class: "what", text: st && st.outcome === "funded" ? `Charging ${money(st.final_price)} each` : "Releasing every hold" }));
    } else if (d.state === "settled") {
      const t = st.totals || {}, c = st.counts || {};
      kids.push(state("Settled"),
        el("p", { class: "what", text: st.outcome === "funded" ? `${plural(c.captured, "buyer")} paid ${money(st.final_price)}` : "Nobody paid" }),
        el("dl", { class: "facts" },
          st.outcome === "funded" ? [
            el("div", {}, el("dt", { text: "Charged" }), el("dd", { class: "v", text: money(t.gross) })),
            el("div", {}, el("dt", { text: "PayPal fees" }), el("dd", { class: "v", text: money(t.fees) })),
            el("div", {}, el("dt", { text: "You receive" }), el("dd", { class: "v", text: money(t.net) })),
          ] : null,
          el("div", {}, el("dt", { text: "Holds released" }), el("dd", { class: "v", text: String(c.voided || 0) })),
          c.failed ? el("div", {}, el("dt", { class: "stop", text: "Charges failed" }), el("dd", { class: "v stop", text: String(c.failed) })) : null),
        el("div", { class: "checks" },
          el("button", { class: "btn quiet inline", disabled: s.busy.verify, onclick: () => check("verify"), text: s.busy.verify ? "Asking PayPal…" : "Check with PayPal" }),
          el("button", { class: "btn quiet inline", disabled: s.busy.tieout, onclick: () => check("tieout"), text: s.busy.tieout ? "Reading the balance…" : "Balance proof" })),
        resultVerify(), resultTieout());
    } else if (d.state === "closed") {
      kids.push(state("Closed"), el("p", { class: "what", text: d.minimum_met ? `Ready to charge ${plural(d.buyers, "buyer")} ${money(d.price)}` : `Ready to release ${plural(d.buyers, "hold")}` }),
        el("p", { text: "Your assistant asks you to approve the settlement. The request shows up here." }));
    } else if (d.state === "ended") {
      kids.push(state("Time is up"), el("p", { class: "what", text: `${d.buyers} joined` }), el("p", { text: "Your assistant closes the drop next. Nobody can join now." }));
    } else {
      kids.push(state("Nothing needs you"), el("p", { class: "what", text: `${plural(d.buyers, "buyer")} so far` }),
        el("p", { text: "Your assistant runs the drop. When it closes, you approve the settlement in your terminal. Nothing is charged before that." }));
    }
    return el("div", { class: "card needs" }, kids);
  }

  function resultVerify() {
    const r = s.checks.verify;
    if (!r) return null;
    if (r.error) return el("div", { class: "result stop", text: r.error });
    const rep = r.report || {};
    if (r.ok) return el("div", { class: "result" }, el("span", { class: "go", text: "Every hold and capture matches PayPal. " }),
      el("span", { class: "muted", text: `Checked ${hhmm(r.checked_at)}.` }));
    return el("div", { class: "result" }, el("span", { class: "stop", text: "PayPal disagrees with the record:" }),
      el("ul", {}, (rep.problems || []).slice(0, 6).map(p => el("li", { text: p }))));
  }

  function resultTieout() {
    const r = s.checks.tieout;
    if (!r) return null;
    if (r.error) return el("div", { class: "result stop", text: r.error });
    const rep = r.report || {};
    if (rep.kind === "certificate") {
      return el("div", { class: "result" }, rep.matches_engine
        ? el("span", { class: "go", text: `PayPal's balance change ties out: this drop's captures net ${money(rep.engine_net)}, to the cent.` })
        : el("span", { class: "stop", text: `The balance proof holds, but it does not match the drop's net of ${money(rep.engine_net)}.` }));
    }
    const why = { no_transaction_search: "The pretend PayPal has no transaction search. Run the drop in the sandbox for a balance proof.",
                  not_yet_indexed: "PayPal has not indexed these captures yet. It can take a few hours; try again later.",
                  nothing_captured: "Nothing was charged, so there is no balance change to prove." }[rep.reason];
    return el("div", { class: "result muted", text: why || rep.what_to_do || "No proof yet." });
  }

  async function check(which) {
    s.busy[which] = true; render();
    try {
      s.checks[which] = await api(`/api/maker/drops/${s.drop}/${which}`, { method: "POST", headers: hdr(), body: {} });
    } catch (e) {
      s.checks[which] = { error: e.message };
    } finally {
      s.busy[which] = false; render();
    }
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); s.copied = text.split(" ").pop(); render(); } catch (e) { /* the text is on screen */ }
  }

  const STATUS = { held: "held", captured: "paid", left: "left", voided: "released", capture_failed: "charge failed",
                   hold_failed: "declined", void_failed: "release failed", excluded: "taken out" };
  const BAD = new Set(["capture_failed", "hold_failed", "void_failed"]);

  function buyers(d) {
    const counts = Object.entries(d.buyers_by_status || {}).map(([k, n]) => `${n} ${STATUS[k] || k}`).join(" · ");
    return el("div", { class: "panel buyers-panel" },
      el("h3", {}, el("span", { text: "Buyers" }), el("span", { class: "counts", text: counts })),
      el("ul", { class: "buyers" }, (d.buyers_list || []).map(b =>
        el("li", {}, el("span", { text: b.name }),
          el("span", { class: "st" + (BAD.has(b.status) ? " bad" : ""), text: `${STATUS[b.status] || b.status}${b.charged ? " " + money(b.charged) : (b.held ? " " + money(b.held) : "")}` })))));
  }

  function feed(d) {
    return el("div", { class: "panel" }, el("h3", { text: "Activity" }),
      el("ul", { class: "feed" }, (d.feed || []).map(f => el("li", { class: "k-" + f.kind },
        el("time", { datetime: f.at, text: hhmm(f.at) }), el("span", { text: f.text })))));
  }

  function keyForm() {
    const input = el("input", { placeholder: "Maker key", autocomplete: "off" });
    return el("div", { class: "card keyform" }, el("h2", { text: "Maker key" }),
      el("p", { class: "muted", text: "The server prints a maker link with the key when it starts. Paste the key here." }),
      el("form", { onsubmit: e => { e.preventDefault(); s.key = input.value.trim(); setHash(); tick(); } },
        el("label", { class: "field" }, input), el("button", { class: "btn", text: "Open" })));
  }

  function render(force) {
    const CLOCK = new Set(["server_time", "time_left", "time_left_seconds"]);   // the clock ticks on its own; it is not a change
    const key = JSON.stringify([s.d, s.checks, s.busy, s.copied, s.error && s.error.code, s.drops.map(x => x.drop + x.state)],
                               (k, v) => (CLOCK.has(k) ? undefined : v));
    if (!force && key === s.lastRender) return;       // nothing changed: leave the page (and any selection in it) alone
    s.lastRender = key;
    if ((s.error && s.error.code === "maker_key") || !s.key) { app.replaceChildren(keyForm()); return; }
    if (!s.d) { app.replaceChildren(el("p", { class: "muted", text: s.error ? s.error.message : (s.drops.length ? "Loading…" : "No drops yet. Your assistant opens one.") })); return; }
    const d = s.d;
    document.title = `${d.title} · maker · Crowd Price`;
    s.clock = clockFrom(d);
    const pick = s.drops.length > 1 ? el("select", { class: "mono", onchange: e => { s.drop = e.target.value; s.d = null; s.lastLadder = ""; s.checks = {}; setHash(); tick(); } },
      s.drops.map(x => el("option", { value: x.drop, selected: x.drop === s.drop, text: `${x.title} · ${x.state}` }))) : null;
    const clock = d.state === "live" && s.clock ? el("div", { class: "clock" }, el("div", { class: "label", text: "Joining closes in" }),
      el("div", { class: "t", id: "clock", text: duration(s.clock()) })) : null;
    const ladderHost = el("div", { id: "ladder" });
    const left = el("div", {},
      el("div", { class: "panel" }, el("h3", {}, el("span", { text: "Price ladder" }), el("a", { class: "counts", href: `/d/${d.drop}`, target: "_blank", rel: "noopener", text: "buyer page ↗" })),
        ladderHost, el("p", { class: "caption" }, el("span", {}, el("span", { class: "n", text: String(d.buyers) }), " joined"),
          el("span", {}, "minimum ", el("span", { class: "n", text: String(d.minimum) })), el("span", {}, d.state === "settled" ? "final price " : "price now ", el("span", { class: "n", text: money(d.settled && d.settled.final_price || d.price) })))),
      feed(d));
    const right = el("div", {}, needs(d), buyers(d));
    const keep = document.getElementById("ladder");
    app.replaceChildren(el("div", { class: "mhead" }, el("div", {}, pick ? el("div", { class: "by" }, pick) : null, headline(d)), clock),
      el("div", { class: "mgrid" }, left, right));
    if (keep) ladderHost._cx = keep._cx;
    requestAnimationFrame(() => ladder(ladderHost, d, d.profit));
  }

  setInterval(() => { const c = document.getElementById("clock"); if (c && s.d && s.d.state === "live" && s.clock) c.textContent = duration(s.clock()); }, 1000);
  window.addEventListener("hashchange", () => { const p = params(); s.key = p.get("key") || s.key; if (p.get("drop") && p.get("drop") !== s.drop) { s.drop = p.get("drop"); s.d = null; } tick(); });
  window.addEventListener("resize", () => { if (s.d) render(true); });
  (function loop() { tick().finally(() => setTimeout(loop, 1500)); })();
})();
