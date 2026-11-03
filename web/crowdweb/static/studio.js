// Crowd Price Studio: a chat with the maker's assistant, beside a live panel of the drop it is working on.
// Everything the assistant says is shown as text (textContent), never as HTML. Every amount in the panel is a string the
// server sent (the engine's, or Togi's recomputed approval); this file only lays it out. The approval card is built from
// Togi's signed review: what is charged, the drop it binds to, when the yes stops working. The drop's name is the
// assistant's words, so it is shown only quoted, isolated and labelled as the assistant's. The passphrase is read from its
// box when the person approves, sent once to this server's approval route (Togi's desk), and cleared; it is never kept in
// the page's state, its storage or anything the assistant can see. The panel's shared pieces are panel.js's.
"use strict";
(() => {
  const { el, api, showMode, money, plural, clockFrom, duration, hhmm, ladder, store } = CP;
  const P = CPPanel;
  const KEYSTORE = "crowd-price:maker-key";
  const DROP_ID = /^[A-Za-z0-9_-]{1,64}$/;
  const AP_ID = /^ap_[0-9a-f]{6,32}$/;
  const EXAMPLE = "I'm Juniper. I'm firing a batch of speckled mugs. Clay and glaze cost me 9 dollars a mug and the kiln firing is 260 dollars, and I need at least 30 mugs sold or I lose money. Please open a 48 hour drop called 'Speckled mugs' with a price ladder built from my costs, and publish it.";

  const fromLink = new URLSearchParams(location.hash.slice(1)).get("key");
  if (fromLink) store(KEYSTORE, fromLink, sessionStorage);
  history.replaceState(null, "", location.pathname);
  const s = { key: store(KEYSTORE, undefined, sessionStorage) || "", info: null, epoch: 0, after: 0, busy: false, focus: null,
              items: [], byId: {}, d: null, dErr: null, review: null, reviewErr: null, unreadable: {}, approving: false, approveMsg: null,
              approved: {}, later: {}, checks: {}, busyCheck: {}, tab: "chat", keyErr: null, keys: {} };
  const hdr = () => ({ "X-Maker-Key": s.key });
  const $ = id => document.getElementById(id);
  const log = $("log"), side = $("side"), say = $("say"), send = $("send");
  const narrow = () => window.matchMedia("(max-width: 860px)").matches;
  const wherePanel = () => (narrow() ? "under the Drop tab" : "in the panel");

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
  const MARK = { running: "", ok: "✓", needs_you: "●", refused: "×", error: "×", lapsed: "–" };
  const STATE_WORDS = { running: "working", needs_you: "needs you", refused: "refused", error: "did not work", lapsed: "lapsed" };   // done: the ✓ says it

  function nearBottom() {
    const box = narrow() ? document.scrollingElement : log;
    return box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  }
  function toBottom() {
    if (narrow()) window.scrollTo(0, document.body.scrollHeight); else log.scrollTop = log.scrollHeight;
  }

  function stepNode(it) {
    const open = it.node ? it.node.open : false;
    const rawOpen = it.node ? !!(it.node.querySelector(".raw") || {}).open : false;
    const body = [];
    if (it.state === "running") body.push(el("p", { class: "words muted", text: "Working on it…" }));
    else body.push(el("p", { class: "words", text: it.words || it.outcome || "" }));
    if (it.call || it.result !== undefined) {
      body.push(el("details", { class: "raw", open: rawOpen }, el("summary", { text: "Show what Togi returned" }),
        it.call ? [el("div", { class: "k", text: "The call" }), el("pre", { class: "cmdline", text: it.call })] : null,
        it.result !== undefined && it.result !== null ? [el("div", { class: "k", text: "Togi's reply" }), el("pre", { text: it.result })] : null));
    }
    return el("details", { class: "step s-" + it.state, open },
      el("summary", {},
        el("span", { class: "mark", "aria-hidden": "true", text: MARK[it.state] || "" }),
        el("span", { class: "lbl", text: it.label }),
        el("span", { class: "st", text: STATE_WORDS[it.state] || "" })),
      el("div", { class: "exact" }, body));
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
      el("span", { class: "muted", text: ` It is waiting ${wherePanel()}.` }));
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
      el("p", { text: `It asks the engine for a price ladder from your costs, opens the drop and runs it. You see the drop ${narrow() ? "under the Drop tab" : "beside this chat"} as it happens. Nothing is charged until you approve it there.` }),
      el("button", { class: "linkbtn", type: "button", onclick: () => { say.value = EXAMPLE; grow(); say.focus(); }, text: "Use Juniper's example" })));
  }

  // A step the person has answered no longer waits: the server says so when they approve in the panel; a request approved in
  // the terminal is seen in the drop's own record (no request pending, one approved, or the drop settling or settled).
  function answered(d) {
    if (!d || d.draft) return;
    const ap = d.approvals || [];
    if (ap.some(a => a.status === "pending")) return;
    const yes = ap.some(a => a.status === "approved" || a.status === "consumed") || ["settling", "settled"].includes(d.state);
    const lapsed = !yes && ap.some(a => a.status === "expired" || a.status === "denied");
    if (!yes && !lapsed) return;
    for (const it of s.items) if (it.kind === "step" && it.state === "needs_you" && it.drop === d.drop) {
      Object.assign(it, yes ? { state: "ok", outcome: "You approved it", words: "You approved it." }
                            : { state: "lapsed", outcome: "Not approved", words: "Not approved: the request is no longer waiting, and nothing was charged. Your assistant can ask again." });
      draw(it);
    }
  }

  function apply(ev) {
    if (ev.seq) s.after = Math.max(s.after, ev.seq);
    let it = ev.id ? s.byId[ev.id] : null;
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
      case "step": {
        const last = s.items[s.items.length - 1];
        if (ev.tool === "crowd_skill" && last && last.kind === "step" && last.tool === "crowd_skill") {     // reading the guide: one step
          s.byId[ev.id] = last; Object.assign(last, { state: "running", call: [last.call, ev.call].filter(Boolean).join("\n") }); draw(last);
          break;
        }
        add({ kind: "step", id: ev.id, label: ev.label, tool: ev.tool, drop: ev.drop, call: ev.call, state: "running" });
        break;
      }
      case "step_done":
        if (it) {
          const raw = it.tool === "crowd_skill" && it.result && ev.result ? it.result + "\n\n" + ev.result : ev.result;
          Object.assign(it, { state: ev.state, outcome: ev.outcome, words: ev.words, result: raw === undefined ? it.result : raw }); draw(it);
        }
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
    s.items = []; s.byId = {}; s.after = 0; s.focus = null; s.d = null; s.review = null; s.approveMsg = null; s.later = {};
    log.replaceChildren(); drawEmpty(); drawComposer(); renderPanel(true);
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
    $("composefine").textContent = s.info && !ready ? `The assistant cannot start here: ${s.info.why_not_ready}.`
      : `Never type your passphrase here. You approve charges ${wherePanel()}.`;
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

  // ---- the approval card: Togi's signed review, in plain words first, the exact signed text one click further ------------
  const pendingOf = d => (d.approvals || []).find(a => a.status === "pending");
  const isDrop = rv => rv.confirm_word && rv.confirm_word === rv.drop;

  function binding(rv) {
    // the drop the yes binds to (Togi's confirmation word for a settle), and beside it, quoted and isolated, what the assistant calls it
    return el("p", { class: "binds" },
      el("span", { class: "k", text: isDrop(rv) ? "Drop " : "Confirmation word " }), el("span", { class: "mono", text: rv.confirm_word || "" }),
      rv.named !== null && rv.named !== undefined ? [el("span", { class: "muted", text: " · your assistant's name for it: " }),
        el("q", { class: "named" }, el("bdi", { text: rv.named }))] : null);
  }
  function rechecks(rv) { return /re-checks every hold|reads them again before any money moves/i.test(rv.summary || ""); }

  // a request whose plan is not exactly a plan token (a space around it, say) is not shown or approved here: the server refuses to
  // review it (`request_unreadable`), and this page does not even ask. Nothing about money is said of it.
  const unreadable = pending => P.plan(pending.expect).kind === "other" || !!s.unreadable[pending.id];

  function approvalCard(d, pending) {
    const desk = (s.info && s.info.desk) || { open: false };
    const kids = [el("div", { class: "state ask", text: "Needs your approval" })];
    if (unreadable(pending)) {
      kids.push(el("p", { class: "what", id: "approve-what", text: "This request cannot be read" }),
        el("p", { text: "Nothing is charged, and it cannot be approved here. Ask your assistant to ask again." }));
      return kids;
    }
    if (!desk.open) {
      const p = P.plan(pending.expect);
      kids.push(el("p", { class: "what", text: p.what }), el("p", { class: "muted", text: p.totalLine }),
        el("p", { text: "Your assistant asked to settle. Only you can approve it, in your own terminal:" }),
        el("div", { class: "cmd" }, el("span", { text: `togi approve ${pending.id}` })),
        el("p", { class: "fine", text: `Approving here is off: ${desk.why || "this server cannot approve"}.` }));
      return kids;
    }
    const rv = s.review && s.review.id === pending.id ? s.review.data : null;
    if (!rv) {
      kids.push(el("p", { class: "what", text: "Checking the request…" }),
        s.reviewErr ? el("p", { class: "msg stop", text: s.reviewErr }) : null);
      return kids;
    }
    const p = P.plan((rv.args || {}).expect);
    const until = hhmm(rv.expires_if_approved);
    const test = rv.scope === "test" ? "Signed by a test approver: it counts only on the Pretend PayPal." : null;
    kids.push(el("p", { class: "what", id: "approve-what", text: p.what }), el("p", { class: "total", text: p.totalLine }),
      p.kind === "capture" && rv.hold_each && rv.back_each !== null && rv.back_each !== undefined
        ? el("p", { class: "holds", text: `Each buyer's ${money(rv.hold_each)} hold is charged ${money(p.price)}${rv.back_each !== "0.00" ? `, and the other ${money(rv.back_each)} goes back to them` : ""}.` }) : null,
      binding(rv));
    if (s.later[pending.id]) {
      kids.push(el("p", { class: "later", text: `Not approved. The request waits until ${hhmm(pending.expires)}; if you do not approve it by then, it lapses: nothing is charged, and your assistant can ask again.` }),
        el("button", { class: "btn quiet inline", type: "button", onclick: () => { s.later[pending.id] = false; renderPanel(); showApproval(); }, text: "Review it again" }));
      return kids;
    }
    kids.push(el("details", { class: "signed" }, el("summary", { text: "Exactly what you sign" }),
      el("ul", { class: "plain" },
        p.signed ? el("li", {}, p.signed) : null,
        p.sum ? el("li", { class: "mono" }, p.sum) : null,
        p.extra ? el("li", {}, p.extra.trim()) : null,
        el("li", {}, `Works once, until ${until}`),
        rechecks(rv) ? el("li", {}, "Every hold is checked again with PayPal before any money moves") : null),
      el("details", { class: "exacttext" }, el("summary", { text: "Show the signed text" }),
        el("pre", { text: (rv.lines || []).join("\n") }))));
    const input = el("input", { type: "password", id: "passphrase", name: "approver-passphrase", autocomplete: "off",
                                spellcheck: "false", autocapitalize: "off", "aria-describedby": "approve-fine", required: true });
    kids.push(el("form", { class: "approve", onsubmit: e => approve(e, pending.id, input) },
      el("label", { class: "field" }, el("span", { text: "Approver passphrase" }), input),
      el("div", { class: "actions" },
        el("button", { class: "btn", type: "submit", disabled: s.approving, text: s.approving ? "Checking…" : "Approve the charge" }),
        el("button", { class: "linkbtn notnow", type: "button", disabled: s.approving, onclick: () => { s.later[pending.id] = true; s.approveMsg = null; renderPanel(); }, text: "Not now" }))),
      s.approveMsg ? el("p", { class: "msg stop", role: "alert", text: s.approveMsg }) : null,
      el("p", { class: "fine", id: "approve-fine", text: `Your yes works once, until ${until}. Your passphrase goes only to Togi's approver check on this computer; your assistant never sees it.${test ? " " + test : ""}` }));
    return kids;
  }

  async function check(which) {
    const drop = s.focus;
    s.busyCheck[which] = true; renderPanel();
    const got = s.checks[drop] || (s.checks[drop] = {});
    try { got[which] = await api(`/api/maker/drops/${encodeURIComponent(drop)}/${which}`, { method: "POST", headers: hdr(), body: {} }); }
    catch (e) { got[which] = { error: e.message }; }
    finally { s.busyCheck[which] = false; renderPanel(); }
  }
  const needs = d => P.needs(d, { ask: approvalCard, where: "here", mode: s.info && s.info.paypal, checks: s.checks[d.drop] || {},
                                  busy: s.busyCheck, check, tieout: false });

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
      const k = keyOf(["empty", s.focus, s.dErr && s.dErr.message, narrow()]);
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
            el("a", { class: "buyerlink", href: `/d/${encodeURIComponent(d.drop)}`, target: "_blank", rel: "noopener", text: "Buyer page ↗" })),
          el("div", { id: "ladder" }), el("div", { id: "caption-host" })),
        el("div", { class: "pgrid" }, el("div", { id: "colA" }), el("div", { id: "colB" })));
    }
    document.title = `${P.shown(d.title).slice(0, 60)} · studio · Crowd Price`;
    const clock = clockFrom(d);
    s.clock = clock;
    part("phead", keyOf([d.title, d.state, d.buyers, d.minimum, d.minimum_met, d.price, d.settlement, d.blocked, d.draft, P.waiting(d)]), () =>
      el("div", { class: "pheadin" }, P.headline(d),
        d.state === "live" && clock ? el("div", { class: "clock" }, el("div", { class: "label", text: "Joining closes in" }),
          el("div", { class: "t", id: "clock", text: duration(clock()) })) : null), $("phead-host"));
    const final = d.settled && d.settled.outcome === "funded";
    part("caption", keyOf([d.buyers, d.minimum, d.price, d.settled, d.profit, d.profit_now]), () => el("div", {},
      el("p", { class: "caption" }, el("span", {}, el("span", { class: "n", text: String(d.buyers) }), " joined"),
        el("span", {}, "minimum ", el("span", { class: "n", text: String(d.minimum) })),
        el("span", {}, final ? "final price " : (d.settled ? "start price " : "price now "),
          el("span", { class: "n", text: money(final ? d.settled.final_price : (d.settled ? d.start_price : d.price)) }))),
      P.profitRow(d)), $("caption-host"));
    const lkey = keyOf([d.steps, d.buyers, d.minimum, d.minimum_met, d.locked_step, side.clientWidth]);
    if (s.keys.ladder !== lkey) { s.keys.ladder = lkey; const host = $("ladder"); requestAnimationFrame(() => ladder(host, d)); }
    const pending = pendingOf(d);
    part("needs", keyOf([d.state, d.blocked, d.settlement, d.profit_now, d.approvals, d.buyers, d.price, d.minimum_met, d.draft, s.info && s.info.desk,
                         pending && s.review && s.review.id === pending.id ? s.review.data.review : null, s.reviewErr, pending && s.unreadable[pending.id], s.approving,
                         s.approveMsg, s.later, s.checks[d.drop], s.busyCheck, narrow()]), () => needs(d), $("colA"));
    part("feed", keyOf([d.feed]), () => P.feed(d), $("colB"));
    part("buyerlist", keyOf([d.buyers_list, d.buyers_by_status, d.simulated]), () => P.buyers(d), $("colB"));
    if (pending && s.info && s.info.desk && s.info.desk.open && !unreadable(pending)) wantReview(pending.id);
  }

  async function refreshPanel() {
    if (!s.focus || !s.key) { renderPanel(); return; }
    const drop = s.focus;
    try {
      const d = await api(`/api/studio/drops/${encodeURIComponent(drop)}`, { headers: hdr() });
      if (drop !== s.focus) return;
      s.d = d; s.dErr = null;
      answered(d);
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
      if (e.code === "request_unreadable") s.unreadable[id] = true;          // asked once: the server will not show it
    } finally { reviewing = null; renderPanel(); }
  }

  async function approve(e, id, input) {
    e.preventDefault();
    const rv = s.review && s.review.id === id ? s.review.data : null;
    if (!rv || s.approving) return;
    if (!input.value) { s.approveMsg = "Type your approver passphrase."; renderPanel(); return; }
    s.approving = true; s.approveMsg = null;
    const body = { review: rv.review, word: rv.confirm_word, passphrase: input.value };     // the word is the one the card shows
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
    if (n) n.scrollIntoView({ behavior: CP.calm() ? "auto" : "smooth", block: "start" });
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
  let wasNarrow = narrow();
  window.addEventListener("resize", () => {
    s.keys.ladder = null;
    if (narrow() !== wasNarrow) {                        // the words that say where the drop is follow the layout
      wasNarrow = narrow(); drawComposer();
      const e = $("empty"); if (e) { e.remove(); drawEmpty(); }
      for (const it of s.items) if (it.kind === "cta" && it.li) draw(it);
    }
    renderPanel();
  });
  start();
})();
