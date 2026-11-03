// Crowd Price Studio: a chat with the maker's assistant, beside a live panel of the drop it is working on.
// Everything the assistant says is shown as text (textContent), never as HTML. Every amount in the panel is a string the
// server sent (the engine's, or Togi's recomputed approval); this file only lays it out. The passphrase is read from its
// box when the person approves, sent once to this server's approval route (Togi's desk), and cleared; it is never kept in
// the page's state, its storage or anything the assistant can see.
"use strict";
(() => {
  const { el, api, showMode, money, plural, clockFrom, duration, hhmm, ladder, store } = CP;
  const KEYSTORE = "crowd-price:maker-key";
  const DROP_ID = /^[A-Za-z0-9_-]{1,64}$/;
  const AP_ID = /^ap_[0-9a-f]{6,32}$/;
  const EXAMPLE = "I'm Juniper. I'm firing a batch of speckled mugs. Clay and glaze cost me 9 dollars a mug and the kiln firing is 260 dollars, and I need at least 30 mugs sold or I lose money. Please open a 48 hour drop called 'Speckled mugs' with a price ladder built from my costs, and publish it.";

  const fromLink = new URLSearchParams(location.hash.slice(1)).get("key");
  if (fromLink) store(KEYSTORE, fromLink, sessionStorage);
  history.replaceState(null, "", location.pathname);
  const s = { key: store(KEYSTORE, undefined, sessionStorage) || "", info: null, epoch: 0, after: 0, busy: false, focus: null,
              items: [], byId: {}, d: null, dErr: null, review: null, reviewErr: null, approving: false, approveMsg: null,
              approved: {}, checks: {}, busyCheck: {}, tab: "chat", keyErr: null, keys: {} };
  const hdr = () => ({ "X-Maker-Key": s.key });
  const $ = id => document.getElementById(id);
  const log = $("log"), side = $("side"), say = $("say"), send = $("send");
  const narrow = () => window.matchMedia("(max-width: 860px)").matches;

  // ---- the assistant's words, as text: paragraphs, lists, **bold** and `code`, all built with textContent -----------------
  function inline(line) {
    const out = [];
    const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0, m;
    while ((m = re.exec(line))) {
      if (m.index > last) out.push(line.slice(last, m.index));
      const t = m[0];
      out.push(t.startsWith("**") ? el("strong", { text: t.slice(2, -2) }) : el("code", { text: t.slice(1, -1) }));
      last = m.index + t.length;
    }
    if (last < line.length) out.push(line.slice(last));
    return out;
  }
  const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;
  function prose(text) {
    // paragraphs at blank lines; inside one, a run of "- item" / "1. item" lines is a list and other lines a paragraph
    const frag = document.createDocumentFragment();
    for (const block of String(text).split(/\n{2,}/)) {
      const lines = block.split("\n").filter(l => l.trim() !== "");
      let run = null, para = null;
      for (const l of lines) {
        if (BULLET.test(l)) {
          para = null;
          if (!run) { run = el(/^\s*\d/.test(l) ? "ol" : "ul"); frag.append(run); }
          run.append(el("li", {}, inline(l.replace(BULLET, ""))));
        } else {
          run = null;
          if (!para) { para = el("p"); frag.append(para); } else para.append(el("br"));
          para.append(...inline(l.replace(/^#+\s+/, "")));
        }
      }
    }
    return frag;
  }

  // ---- the transcript ---------------------------------------------------------------------------------------------------
  const MARK = { running: "", ok: "✓", needs_you: "●", refused: "×", error: "×" };
  const STATE_WORDS = { running: "working", ok: "done", needs_you: "needs you", refused: "refused", error: "did not work" };

  function nearBottom() {
    const box = narrow() ? document.scrollingElement : log;
    return box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  }
  function toBottom() {
    if (narrow()) window.scrollTo(0, document.body.scrollHeight); else log.scrollTop = log.scrollHeight;
  }

  function stepNode(it) {
    const open = it.node ? it.node.open : false;
    const n = el("details", { class: "step s-" + it.state, open },
      el("summary", {},
        el("span", { class: "mark", "aria-hidden": "true", text: MARK[it.state] || "" }),
        el("span", { class: "lbl", text: it.label }),
        el("span", { class: "st", text: STATE_WORDS[it.state] || it.state })),
      el("div", { class: "exact" },
        el("div", { class: "k", text: "Command" }), el("pre", { class: "cmdline", text: it.command || "" }),
        it.result !== undefined ? [el("div", { class: "k", text: it.state === "ok" ? "Result" : `Result · ${it.outcome || ""}` }),
                                   el("pre", { text: it.result })] : el("div", { class: "k", text: "Running…" })));
    return n;
  }
  function itemNode(it) {
    if (it.kind === "user") return el("li", { class: "msg-user" }, el("div", { class: "bubble", text: it.text }));
    if (it.kind === "text") {
      const n = el("li", { class: "msg-text" + (it.live ? " live" : "") });
      n.append(prose(it.text));
      return n;
    }
    if (it.kind === "step") return el("li", { class: "msg-step" }, stepNode(it));
    if (it.kind === "note") return el("li", { class: "msg-note" + (it.k === "approved" ? " approved" : ""), text: it.text });
    if (it.kind === "error") return el("li", { class: "msg-error", text: it.text });
    if (it.kind === "cta") return el("li", { class: "msg-cta" },
      el("button", { class: "btn quiet inline", type: "button", onclick: () => showApproval(), text: "Review the charge" }),
      el("span", { class: "muted", text: narrow() ? " It is waiting under Drop." : " It is waiting in the panel." }));
    return el("li");
  }
  function draw(it) {
    const stick = nearBottom();
    const fresh = itemNode(it);
    if (it.li && it.li.parentNode) it.li.replaceWith(fresh); else log.append(fresh);
    it.li = fresh;
    it.node = it.kind === "step" ? fresh.querySelector("details") : fresh;
    if (stick) toBottom();
  }
  function add(it) {
    s.items.push(it);
    if (it.id) s.byId[it.id] = it;
    drawEmpty();
    draw(it);
  }
  function drawWorking() {
    let w = $("working");
    const show = s.busy && !s.items.some(i => i.kind === "text" && i.live);
    if (show && !w) { w = el("li", { id: "working", class: "msg-working", text: "Working" }); log.append(w); if (nearBottom()) toBottom(); }
    if (!show && w) w.remove();
    if (show && w && w !== log.lastElementChild) log.append(w);
  }
  function drawEmpty() {
    const e = $("empty");
    if (s.items.length) { if (e) e.remove(); return; }
    if (e) return;
    log.append(el("li", { id: "empty", class: "msg-empty" },
      el("p", { class: "lead", text: "Tell your assistant what your next batch costs." }),
      el("p", { text: "It asks the engine for a price ladder from your costs, opens the drop and runs it. The drop shows up beside this chat as it happens. Nothing is charged until you approve it, in the panel." }),
      el("button", { class: "linkbtn", type: "button", onclick: () => { say.value = EXAMPLE; grow(); say.focus(); }, text: "Use Juniper's example" })));
  }

  function apply(ev) {
    if (ev.seq) s.after = Math.max(s.after, ev.seq);
    const it = ev.id ? s.byId[ev.id] : null;
    switch (ev.t) {
      case "user": add({ kind: "user", text: ev.text }); break;
      case "text_start": if (!it) add({ kind: "text", id: ev.id, text: "", live: true }); break;
      case "delta":
        if (it) { it.text += ev.text; draw(it); } else add({ kind: "text", id: ev.id, text: ev.text, live: true });
        break;
      case "text":
        if (it) { it.text = ev.text; it.live = false; draw(it); } else add({ kind: "text", id: ev.id, text: ev.text, live: false });
        break;
      case "text_end": if (it) { it.live = false; draw(it); } break;
      case "step": add({ kind: "step", id: ev.id, label: ev.label, command: ev.command, state: "running" }); break;
      case "step_done":
        if (it) { Object.assign(it, { state: ev.state, outcome: ev.outcome, result: ev.result }); draw(it); }
        if (s.focus) refreshPanel();
        break;
      case "note": {
        const cta = ev.kind === "approved" && s.byId["cta-" + ev.approval];
        if (cta && cta.li) { cta.li.remove(); cta.li = null; }        // approved: the prompt to review it is spent
        add({ kind: "note", text: ev.text, k: ev.kind });
        break;
      }
      case "error": add({ kind: "error", text: ev.text }); break;
      case "busy": s.busy = !!ev.busy; drawComposer(); break;
      case "focus": if (DROP_ID.test(ev.drop || "")) { s.focus = ev.drop; s.d = null; s.review = null; refreshPanel(); } break;
      case "approval":
        if (AP_ID.test(ev.id || "") && !s.byId["cta-" + ev.id]) { add({ kind: "cta", id: "cta-" + ev.id }); refreshPanel(); }
        break;
    }
    drawWorking();
  }

  function clearChat() {
    s.items = []; s.byId = {}; s.after = 0; s.focus = null; s.d = null; s.review = null; s.approveMsg = null;
    log.replaceChildren(); drawEmpty(); renderPanel(true);
  }

  // ---- the stream: NDJSON lines from /api/studio/events, asked again from the last line whenever it ends -----------------
  async function stream() {
    let fails = 0;
    for (;;) {
      if (!s.key) return;
      try {
        const res = await fetch(`/api/studio/events?after=${s.after}&epoch=${s.epoch}`, { headers: hdr(), cache: "no-store" });
        if (res.status === 401 || res.status === 429) { const b = await res.json().catch(() => ({})); keyFailed(b.error); return; }
        if (!res.ok || !res.body) throw new Error("stream");
        const reader = res.body.getReader(), dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, i); buf = buf.slice(i + 1);
            if (!line.trim()) continue;
            let ev; try { ev = JSON.parse(line); } catch (e) { continue; }
            if (ev.t === "ping") continue;
            if (ev.t === "epoch") { s.epoch = ev.epoch; clearChat(); continue; }
            apply(ev);
          }
        }
        fails = 0;
        setLive(true);
      } catch (e) {
        fails += 1; setLive(false);
        await new Promise(r => setTimeout(r, Math.min(6000, 400 * 2 ** fails)));
      }
    }
  }
  function setLive(on) { document.body.classList.toggle("offline", !on); }

  // ---- talking -------------------------------------------------------------------------------------------------------------
  function grow() { say.style.height = "auto"; say.style.height = Math.min(180, say.scrollHeight) + "px"; }
  function drawComposer() {
    const ready = s.info && s.info.ready;
    send.disabled = s.busy || !ready;          // the label stays "Send" (the log says the assistant is working)
    $("reset").hidden = !s.items.length;
    if (s.info && !ready) $("composefine").textContent = `The assistant cannot start here: ${s.info.why_not_ready}.`;
  }
  $("composer").addEventListener("submit", async e => {
    e.preventDefault();
    const text = say.value.trim();
    if (!text || s.busy) return;
    s.busy = true; drawComposer();
    try {
      await api("/api/studio/say", { method: "POST", headers: hdr(), body: { text } });
      say.value = ""; say.style.height = "";
    } catch (err) {
      s.busy = false; drawComposer();
      add({ kind: "error", text: err.message || "That did not reach your assistant." });
    }
  });
  say.addEventListener("input", grow);
  say.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); }
  });
  $("reset").addEventListener("click", async () => {
    try { const r = await api("/api/studio/reset", { method: "POST", headers: hdr(), body: {} }); s.epoch = r.epoch; clearChat(); }
    catch (e) { add({ kind: "error", text: e.message }); }
  });

  // ---- the panel ----------------------------------------------------------------------------------------------------------
  // the plan an approval is bound to: capture:45:37.00:1665.00, void:22, either +release:K. Read from Togi's recomputed args.
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
    if (d.draft) { claim = `${name} is being set up.`; turn = "Buyers can join once it opens."; }
    else if (d.blocked) { claim = `Settling ${d.title} stopped.`; turn = "It needs you."; }
    else if (d.state === "live") turn = d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.`
                                                       : `${d.buyers} of ${d.minimum} joined. Everyone pays ${money(d.price)} once ${d.minimum} join.`;
    else if (d.state === "ended") turn = d.minimum_met ? "Time is up. Your assistant closes it next." : "Time is up, and it missed its minimum.";
    else if (d.state === "closed") turn = d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.` : `${d.buyers} of ${d.minimum} joined. Every hold will be released.`;
    else if (d.state === "settling") turn = st && st.outcome === "funded" ? `Charging everyone ${money(st.final_price)}.` : "Releasing every hold.";
    else if (st && st.outcome === "funded") turn = `${plural(st.counts.captured, "buyer")} paid ${money(st.final_price)}.`;
    else if (st) { claim = `${name} missed its minimum.`; turn = `${plural(st.counts.voided, "hold")} released. Nobody paid.`; }
    return el("h1", { class: "two" }, el("span", { text: claim }), " ", el("span", { class: "turn", text: turn }));
  }

  const pendingOf = d => (d.approvals || []).find(a => a.status === "pending");

  function approvalCard(d, pending) {
    const desk = (s.info && s.info.desk) || { open: false };
    const kids = [el("div", { class: "state ask", text: "Needs your approval" })];
    if (!desk.open) {
      const p = plan(pending.expect);
      kids.push(el("p", { class: "what", text: p.what }), el("p", { class: "muted", text: p.total }),
        el("p", { text: "Your assistant asked to settle. Only you can approve it, in your own terminal:" }),
        el("div", { class: "cmd" }, el("span", { text: `togi approve ${pending.id}` })),
        el("p", { class: "fine", text: `Approving in this panel is off: ${desk.why || "this server cannot approve"}.` }));
      return kids;
    }
    const rv = s.review && s.review.id === pending.id ? s.review.data : null;
    if (!rv) {
      kids.push(el("p", { class: "what", text: "Checking the request…" }),
        s.reviewErr ? el("p", { class: "msg stop", text: s.reviewErr }) : null);
      return kids;
    }
    const p = plan((rv.args || {}).expect);
    kids.push(el("p", { class: "what", id: "approve-what", text: p.what }), el("p", { class: "muted", text: p.total }),
      el("p", { text: `Your assistant asked to settle ${rv.title || "this drop"}. Only you can approve it; your assistant cannot.` }),
      el("details", { class: "signed" }, el("summary", { text: "Exactly what you sign" }),
        el("pre", { text: (rv.lines || []).join("\n") })));
    const input = el("input", { type: "password", id: "passphrase", name: "approver-passphrase", autocomplete: "off",
                                spellcheck: "false", autocapitalize: "off", "aria-describedby": "approve-fine", required: true });
    kids.push(el("form", { class: "approve", onsubmit: e => approve(e, pending.id, input) },
      el("label", { class: "field" }, el("span", { text: "Approver passphrase" }), input),
      el("button", { class: "btn", type: "submit", disabled: s.approving, text: s.approving ? "Checking…" : "Approve the charge" })),
      s.approveMsg ? el("p", { class: "msg stop", role: "alert", text: s.approveMsg }) : null,
      el("p", { class: "fine", id: "approve-fine", text: `Your passphrase goes only to Togi's approver check on this computer. Your assistant never sees it. If you approve, it can be used once, until ${hhmm(rv.expires_if_approved)}.${desk.scope === "test" ? " This is a test approver: it counts only on the pretend PayPal." : ""}` }));
    return kids;
  }

  function needs(d) {
    const pending = d.state !== "settled" ? pendingOf(d) : null;
    const approved = (d.approvals || []).find(a => a.status === "approved");
    const st = d.settlement;
    const kids = [];
    const state = (text, cls) => el("div", { class: "state" + (cls ? " " + cls : ""), text });
    if (d.draft) {
      kids.push(state("Setting up"), el("p", { class: "what", text: "Not open yet" }), el("p", { text: "Your assistant opens the drop to buyers next." }));
    } else if (d.blocked) {
      kids.push(state("Settlement stopped", "bad"), el("p", { class: "what", text: `PayPal stopped it: ${d.blocked.words}` }),
        el("p", { text: "Nothing more is charged until it is sorted out. Ask your assistant to explain the drop." }));
    } else if (pending) {
      kids.push(...approvalCard(d, pending));
    } else if (approved && !st) {
      kids.push(state("Approved"), el("p", { class: "what", text: plan(approved.expect).what }), el("p", { text: "You approved it. Your assistant settles next." }));
    } else if (d.state === "settling") {
      kids.push(state("Settling"), el("p", { class: "what", text: st && st.outcome === "funded" ? `Charging ${money(st.final_price)} each` : "Releasing every hold" }));
    } else if (d.state === "settled") {
      const t = st.totals || {}, c = st.counts || {};
      const ok = s.info && s.info.paypal === "fake" ? "Every hold and capture matches the pretend PayPal." : "Every hold and capture matches PayPal.";
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
          el("button", { class: "btn quiet inline", disabled: s.busyCheck.verify, onclick: () => check("verify"), text: s.busyCheck.verify ? "Asking PayPal…" : "Check with PayPal" })),
        resultVerify(ok));
    } else if (d.state === "closed") {
      kids.push(state("Closed"), el("p", { class: "what", text: d.minimum_met ? `Ready to charge ${plural(d.buyers, "buyer")} ${money(d.price)}` : `Ready to release ${plural(d.buyers, "hold")}` }),
        el("p", { text: "Your assistant asks you to approve the settlement. The request shows up here." }));
    } else if (d.state === "ended") {
      kids.push(state("Time is up"), el("p", { class: "what", text: `${plural(d.buyers, "buyer")} joined` }), el("p", { text: "Your assistant closes the drop next. Nobody can join now." }));
    } else {
      kids.push(state("Nothing needs you"), el("p", { class: "what", text: `${plural(d.buyers, "buyer")} so far` }),
        el("p", { text: "Your assistant runs the drop. When it closes, you approve the charge here. Nothing is charged before that." }));
    }
    return el("div", { class: "card needs" + (pending || d.blocked ? " urgent" : ""), id: "needs" }, kids);
  }

  function resultVerify(ok) {
    const r = s.checks[s.focus];
    if (!r) return null;
    if (r.error) return el("div", { class: "result stop", text: r.error });
    if (r.ok) return el("div", { class: "result" }, el("span", { class: "go", text: ok + " " }), el("span", { class: "muted", text: `Checked ${hhmm(r.checked_at)}.` }));
    return el("div", { class: "result" }, el("span", { class: "stop", text: "PayPal disagrees with the record:" }),
      el("ul", {}, ((r.report || {}).problems || []).slice(0, 6).map(p => el("li", { text: p }))));
  }
  async function check(which) {
    const drop = s.focus;
    s.busyCheck[which] = true; renderPanel();
    try { s.checks[drop] = await api(`/api/maker/drops/${encodeURIComponent(drop)}/${which}`, { method: "POST", headers: hdr(), body: {} }); }
    catch (e) { s.checks[drop] = { error: e.message }; }
    finally { s.busyCheck[which] = false; renderPanel(); }
  }

  const STATUS = { held: "held", captured: "paid", left: "left", voided: "released", capture_failed: "charge failed",
                   hold_failed: "declined", void_failed: "release failed", excluded: "taken out" };
  const BAD = new Set(["capture_failed", "hold_failed", "void_failed"]);
  function buyers(d) {
    const counts = Object.entries(d.buyers_by_status || {}).map(([k, n]) => `${n} ${STATUS[k] || k}`);
    if (d.simulated) counts.push(`${d.simulated} simulated`);
    return el("div", { class: "panel buyers-panel" },
      el("h3", {}, el("span", { text: "Buyers" }), el("span", { class: "counts", text: counts.join(" · ") })),
      (d.buyers_list || []).length ? el("ul", { class: "buyers" }, d.buyers_list.map(b =>
        el("li", {}, el("bdi", { text: b.name }),
          el("span", { class: "st" + (BAD.has(b.status) ? " bad" : ""), text: `${STATUS[b.status] || b.status}${b.charged ? " " + money(b.charged) : (b.held ? " " + money(b.held) : "")}` }))))
        : el("p", { class: "muted small", text: "Nobody yet." }));
  }
  function feed(d) {
    return el("div", { class: "panel" }, el("h3", { text: "Activity" }),
      (d.feed || []).length ? el("ul", { class: "feed" }, d.feed.map(f => el("li", { class: "k-" + f.kind },
        el("time", { datetime: f.at, text: hhmm(f.at) }),
        el("span", {}, f.who ? [el("bdi", { class: "who", text: f.who }), " "] : null, f.text))))
        : el("p", { class: "muted small", text: "Nothing yet." }));
  }
  function profitRow(d) {
    if (!d.profit || !d.profit.length) return null;
    const now = d.locked_step === null || d.locked_step === undefined ? -1 : d.locked_step;
    return el("p", { class: "profitrow" }, el("span", { class: "k", text: "Profit after your costs" }),
      d.profit.map((r, i) => el("span", { class: i === now ? "now" : "" }, `${r.from_buyers} buyers `, el("span", { class: "v", text: money(r.profit) }))));
  }

  // Each part of the panel is redrawn only when its own data changed: a poll never wipes a half-typed passphrase.
  const CLOCK = new Set(["server_time", "time_left", "time_left_seconds"]);
  const keyOf = v => JSON.stringify(v, (k, x) => (CLOCK.has(k) ? undefined : x));
  function part(id, key, build, host) {
    if (!host) return;
    if (s.keys[id] === key && $(id)) return;
    s.keys[id] = key;
    const fresh = build();
    fresh.id = id;
    const old = $(id);
    if (old) old.replaceWith(fresh); else host.append(fresh);
  }

  function renderPanel(force) {
    if (force) s.keys = {};
    updateTab();
    if (!s.focus || !s.d) {
      const k = keyOf(["empty", s.focus, s.dErr && s.dErr.message]);
      if (s.keys.root !== k) {
        s.keys = { root: k };
        side.replaceChildren(el("div", { class: "sideempty" },
          el("h2", { text: s.focus ? "Loading the drop…" : "Your drop shows up here" }),
          el("p", { class: "muted", text: s.dErr ? s.dErr.message : "As your assistant opens it: the price ladder, the buyers as they join, and the price stepping down for everyone." })));
      }
      return;
    }
    const d = s.d;
    if (s.keys.root !== "drop:" + d.drop) {
      s.keys = { root: "drop:" + d.drop };
      side.replaceChildren(el("div", { class: "mhead", id: "phead-host" }),
        el("div", { class: "panel ladder-panel", id: "ladder-panel" },
          el("h3", {}, el("span", { text: "Price ladder" }),
            el("a", { class: "counts", href: `/d/${encodeURIComponent(d.drop)}`, target: "_blank", rel: "noopener", text: "buyer page ↗" })),
          el("div", { id: "ladder" }), el("div", { id: "caption-host" })),
        el("div", { class: "pgrid" }, el("div", { id: "colA" }), el("div", { id: "colB" })));
    }
    document.title = `${d.title} · studio · Crowd Price`;
    const clock = clockFrom(d);
    s.clock = clock;
    part("phead", keyOf([d.title, d.state, d.buyers, d.minimum, d.minimum_met, d.price, d.settlement, d.blocked, d.draft]), () =>
      el("div", { class: "pheadin" }, headline(d),
        d.state === "live" && clock ? el("div", { class: "clock" }, el("div", { class: "label", text: "Joining closes in" }),
          el("div", { class: "t", id: "clock", text: duration(clock()) })) : null), $("phead-host"));
    const final = d.settled && d.settled.outcome === "funded";
    part("caption", keyOf([d.buyers, d.minimum, d.price, d.settled, d.profit, d.locked_step]), () => el("div", {},
      el("p", { class: "caption" }, el("span", {}, el("span", { class: "n", text: String(d.buyers) }), " joined"),
        el("span", {}, "minimum ", el("span", { class: "n", text: String(d.minimum) })),
        el("span", {}, final ? "final price " : (d.settled ? "start price " : "price now "),
          el("span", { class: "n", text: money(final ? d.settled.final_price : (d.settled ? d.start_price : d.price)) }))),
      profitRow(d)), $("caption-host"));
    const lkey = keyOf([d.steps, d.buyers, d.minimum, d.minimum_met, d.locked_step, side.clientWidth]);
    if (s.keys.ladder !== lkey) { s.keys.ladder = lkey; const host = $("ladder"); requestAnimationFrame(() => ladder(host, d)); }
    const pending = pendingOf(d);
    part("needs", keyOf([d.state, d.blocked, d.settlement, d.approvals, d.buyers, d.price, d.minimum_met, d.draft, s.info && s.info.desk,
                         pending && s.review && s.review.id === pending.id ? s.review.data.review : null, s.reviewErr, s.approving,
                         s.approveMsg, s.checks[d.drop], s.busyCheck]), () => needs(d), $("colA"));
    part("feed", keyOf([d.feed]), () => feed(d), $("colB"));
    part("buyerlist", keyOf([d.buyers_list, d.buyers_by_status, d.simulated]), () => buyers(d), $("colB"));
    if (pending && s.info && s.info.desk && s.info.desk.open) wantReview(pending.id);
  }

  async function refreshPanel() {
    if (!s.focus || !s.key) { renderPanel(); return; }
    const drop = s.focus;
    try {
      const d = await api(`/api/studio/drops/${encodeURIComponent(drop)}`, { headers: hdr() });
      if (drop !== s.focus) return;
      s.d = d; s.dErr = null;
    } catch (e) {
      if (e.code === "maker_key") return keyFailed(e);
      s.dErr = e;
    }
    renderPanel();
  }

  // ---- approving: Togi's review first (what will be signed), then the person's yes with the passphrase -------------------
  let reviewing = null;
  async function wantReview(id, again) {
    const fresh = s.review && s.review.id === id && Date.now() - s.review.at < 75 * 1000;
    const box = $("passphrase");
    const typing = box && (box.value || document.activeElement === box) && s.review && s.review.id === id;
    if (((fresh || typing) && !again) || reviewing === id) return;     // never swap the form under someone typing
    reviewing = id;
    try {
      const data = await api(`/api/studio/approvals/${encodeURIComponent(id)}`, { headers: hdr() });
      s.review = { id, data, at: Date.now() }; s.reviewErr = null;
    } catch (e) {
      s.review = null; s.reviewErr = e.message || "The request could not be checked.";
    } finally { reviewing = null; renderPanel(); }
  }

  async function approve(e, id, input) {
    e.preventDefault();
    const rv = s.review && s.review.id === id ? s.review.data : null;
    if (!rv || s.approving) return;
    if (!input.value) { s.approveMsg = "Type your approver passphrase."; renderPanel(); return; }
    s.approving = true; s.approveMsg = null;
    const body = { review: rv.review, word: rv.confirm_word, passphrase: input.value };
    input.value = "";
    renderPanel();
    try {
      await api(`/api/studio/approvals/${encodeURIComponent(id)}/approve`, { method: "POST", headers: hdr(), body });
      s.approved[id] = true; s.review = null;
      const to = { top: 0, behavior: CP.calm() ? "auto" : "smooth" };      // back to the headline: it says what happened
      if (narrow()) window.scrollTo(to); else side.scrollTo(to);
    } catch (err) {
      s.approveMsg = err.message || "That did not work. Nothing was approved.";
      if (err.again) { s.review = null; wantReview(id, true); }
    } finally {
      body.passphrase = null;
      s.approving = false;
      await refreshPanel();
    }
  }

  function showApproval() {
    if (narrow()) setTab("drop");
    const n = $("needs");
    if (n) n.scrollIntoView({ behavior: CP.calm() ? "auto" : "smooth", block: "center" });
    const p = $("passphrase");
    if (p) setTimeout(() => p.focus({ preventScroll: true }), 350);
  }

  // ---- phone: the chat and the drop as two tabs ----------------------------------------------------------------------------
  function setTab(t) {
    s.tab = t;
    document.body.classList.toggle("tab-chat", t === "chat");
    document.body.classList.toggle("tab-drop", t === "drop");
    $("tab-chat").setAttribute("aria-selected", String(t === "chat"));
    $("tab-drop").setAttribute("aria-selected", String(t === "drop"));
    if (t === "drop") { s.keys.ladder = null; renderPanel(); }
    window.scrollTo(0, t === "chat" ? document.body.scrollHeight : 0);
  }
  function updateTab() {
    const note = $("tabnote"), d = s.d;
    if (!note) return;
    let t = "", cls = "tabnote";
    if (d && pendingOf(d) && d.state !== "settled") { t = "needs you"; cls += " ask"; }
    else if (d && !d.draft) t = d.settled && d.settled.outcome === "funded" ? money(d.settled.final_price) : (d.state === "live" || d.state === "closed" ? money(d.price) : "");
    note.textContent = t; note.className = cls;
  }
  $("tab-chat").addEventListener("click", () => setTab("chat"));
  $("tab-drop").addEventListener("click", () => setTab("drop"));

  // ---- the key --------------------------------------------------------------------------------------------------------------
  function keyFailed(err) {
    s.key = ""; store(KEYSTORE, null, sessionStorage); s.keyErr = err;
    keyForm();
  }
  function keyForm() {
    const input = el("input", { placeholder: "Maker key", autocomplete: "off" });
    $("studio").replaceChildren(el("div", { class: "card keyform" }, el("h2", { text: "Studio key" }),
      el("p", { class: "muted", text: "The studio prints its link with the key when it starts. Open that link, or paste the key here." }),
      s.keyErr && s.keyErr.code === "slow_down" ? el("p", { class: "msg stop", text: s.keyErr.message }) : null,
      el("form", { onsubmit: e => { e.preventDefault(); s.key = input.value.trim(); store(KEYSTORE, s.key, sessionStorage); location.reload(); } },
        el("label", { class: "field" }, input), el("button", { class: "btn", text: "Open" }))));
  }

  // ---- start ------------------------------------------------------------------------------------------------------------------
  async function start() {
    if (!s.key) return keyForm();
    try {
      s.info = await api("/api/studio", { headers: hdr() });
    } catch (e) {
      if (e.code === "maker_key" || e.code === "slow_down") return keyFailed(e);
      log.append(el("li", { class: "msg-error", text: e.message }));
      return;
    }
    s.epoch = s.info.epoch;
    s.focus = s.info.focus;
    showMode(s.info.paypal);
    $("tabs").hidden = false;
    drawEmpty(); drawComposer(); renderPanel();
    if (s.focus) refreshPanel();
    stream();
    (function loop() { setTimeout(async () => { if (!document.hidden) await refreshPanel(); loop(); }, 1200); })();
    setInterval(async () => {                           // the desk's state (on, or why not) can change only on restart; the
      try { s.info = await api("/api/studio", { headers: hdr() }); drawComposer(); } catch (e) { /* the stream says */ }
    }, 15000);                                          // busy flag is in the stream; this only keeps `info` honest
  }
  setInterval(() => { const c = $("clock"); if (c && s.d && s.d.state === "live" && s.clock) c.textContent = duration(s.clock()); }, 1000);
  window.addEventListener("resize", () => { s.keys.ladder = null; if (s.d) renderPanel(); });
  start();
})();
