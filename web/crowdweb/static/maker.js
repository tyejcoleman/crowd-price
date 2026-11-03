// The maker's view: the drop as the engine sees it, what needs the maker, and the read-only checks against PayPal.
// It cannot approve or settle anything: approval is the maker's own, in their terminal, with their passphrase.
// The panel's pieces (headline, buyers, activity, profit, checks) are panel.js's, shared with the studio.
"use strict";
(() => {
  const { el, api, showMode, money, clockFrom, duration, hhmm, ladder, store } = CP;
  const P = CPPanel;
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

  // ---- what needs the maker: a waiting approval is theirs to give, in their own terminal (the rest is the shared panel, panel.js)
  function ask(d, pending) {
    const p = P.plan(pending.expect);
    if (p.kind === "other") {          // not a plan the engine writes (a space around it, say): nothing to say about money, nothing to copy
      return [el("div", { class: "state ask", text: "Needs your approval" }), el("p", { class: "what", text: p.what }),
        el("p", { text: "Nothing is charged. Ask your assistant to ask again." })];
    }
    return [el("div", { class: "state ask", text: "Needs your approval" }), el("p", { class: "what", text: p.what }), el("p", { class: "muted", text: p.totalLine }),
      el("p", { text: "Your assistant asked to settle. Only you can approve it, in your own terminal; your assistant cannot." }),
      el("div", { class: "checks" },
        el("button", { class: "btn inline copycmd", onclick: () => copy(`togi approve ${pending.id}`, pending.id), text: s.copied === pending.id ? "Copied" : "Copy the approval command" }),
        el("span", { class: "reqid mono", text: `request ${pending.id}` })),
      el("p", { class: "fine", text: `Paste it in your terminal. It asks for your passphrase, which the assistant never sees.${pending.expires ? ` The request lapses at ${hhmm(pending.expires)}.` : ""} If the drop changes first, settling refuses and your assistant asks again.` })];
  }
  const needs = d => P.needs(d, { ask, where: "terminal", mode: s.mode, checks: s.checks, busy: s.busy, check, tieout: true });

  async function check(which) {
    s.busy[which] = true; render();
    try { s.checks[which] = await api(`${path()}/${which}`, { method: "POST", headers: hdr(), body: {} }); }
    catch (e) { s.checks[which] = { error: e.message }; }
    finally { s.busy[which] = false; render(); }
  }

  async function copy(text, id) {
    try { await navigator.clipboard.writeText(text); s.copied = id; render(); } catch (e) { /* the command is on screen */ }
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
        el("a", { class: "buyerlink", href: `/d/${encodeURIComponent(d.drop)}`, target: "_blank", rel: "noopener", text: "Buyer page ↗" })),
        ladderHost,
        el("p", { class: "caption" }, el("span", {}, el("span", { class: "n", text: String(d.buyers) }), " joined"),
          el("span", {}, "minimum ", el("span", { class: "n", text: String(d.minimum) })),
          el("span", {}, final ? "final price " : (d.settled ? "start price " : "price now "),
            el("span", { class: "n", text: money(final ? d.settled.final_price : (d.settled ? d.start_price : d.price)) }))),
        P.profitRow(d)),
      P.feed(d));
    const right = el("div", {}, needs(d), P.buyers(d));
    const keep = document.getElementById("ladder");
    if (keep) ladderHost._prev = keep._prev;
    app.replaceChildren(el("div", { class: "mhead" }, el("div", {}, pick ? el("div", { class: "by" }, pick) : null, P.headline(d)), clock),
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
