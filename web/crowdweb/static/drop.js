// The buyer's drop page. It shows the engine's numbers and lets this browser join, or leave, with its own token.
"use strict";
(() => {
  const { el, api, money, splitMoney, plural, clockFrom, duration, ladder, poll, store } = CP;
  const dropId = location.pathname.split("/")[2];
  const KEY = "crowd-price:" + dropId;
  const $ = id => document.getElementById(id);

  const s = { drop: null, me: null, cards: [], view: "form", busy: false, error: null, clock: null, lastPrice: null,
              lastLadder: "", confirmLeave: false, form: { name: "", card: 0 }, gone: false };

  async function tick() {
    const mine = store(KEY);
    try {
      if (mine && mine.buyer && mine.token) {
        try {
          const r = await api(`/api/drops/${dropId}/me`, { body: mine });
          s.drop = r.drop; s.me = r.me;
        } catch (e) {
          if (e.code !== "not_yours" && e.code !== "buyer_not_found") throw e;
          store(KEY, null); s.me = null;
          s.drop = await api(`/api/drops/${dropId}`);
        }
      } else {
        s.drop = await api(`/api/drops/${dropId}`);
      }
      s.gone = false;
    } catch (e) {
      if (e.code === "drop_not_found") s.gone = true;
      else if (!s.drop) s.error = e.message;
    }
    render();
  }

  // ---- the left side: title, price, what happens next
  function renderLead(d) {
    document.title = `${d.title} · Crowd Price`;
    $("title").textContent = d.title + ".";
    $("by").replaceChildren(...(d.maker_name ? ["A drop by ", el("b", { text: d.maker_name })] : []));
    const settled = d.settled, notFunded = (settled && settled.outcome !== "funded") || (["closed", "ended"].includes(d.state) && !d.minimum_met);
    const shown = settled && settled.outcome === "funded" ? settled.final_price : d.price;
    const { whole, cents } = splitMoney(shown);
    const price = $("price");
    price.replaceChildren(el("span", { class: "cur", text: "$" }), el("span", { text: whole }), el("span", { class: "cents", text: cents }));
    if (s.lastPrice !== null && parseFloat(shown) < parseFloat(s.lastPrice)) {
      price.classList.add("dropped");
      setTimeout(() => price.classList.remove("dropped"), 1600);
    }
    s.lastPrice = shown;
    price.classList.toggle("faint", notFunded);

    let label, next;
    if (settled && settled.outcome === "funded") {
      label = "Final price";
      next = `${plural(settled.paid, "buyer")} paid ${money(settled.final_price)} each. The rest of every hold was released.`;
    } else if (notFunded) {
      label = "Missed its minimum";
      next = settled ? `Fewer than ${d.minimum} joined, so every hold was released. Nobody paid.`
                     : `Fewer than ${d.minimum} joined, so every hold will be released. Nobody pays.`;
    } else if (d.state === "live") {
      label = d.minimum_met ? "Everyone pays" : `Everyone pays, once ${d.minimum} join`;
      next = d.next_message ? d.next_message.replace(/^./, c => c.toUpperCase()) + "." : "This is the lowest price on the ladder.";
    } else {
      label = "Everyone pays";
      next = d.state === "settling" ? `Charging everyone ${money(d.price)} now.`
                                    : `${d.buyers} joined. Each hold is charged ${money(d.price)} once, when the maker settles.`;
    }
    $("pricelabel").textContent = label;
    $("next").textContent = next;
  }

  // ---- the ladder and the line under it
  function renderLadder(d) {
    const key = JSON.stringify([d.steps, d.buyers, d.locked_step, d.minimum_met, Math.round($("ladder").getBoundingClientRect().width)]);
    if (key !== s.lastLadder) { ladder($("ladder"), d); s.lastLadder = key; }
    s.clock = clockFrom(d);
    const bits = [
      el("span", {}, el("span", { class: "n", text: String(d.buyers) }), " joined"),
      el("span", {}, "minimum ", el("span", { class: "n", text: String(d.minimum) }), d.minimum_met ? ", met" : ", not met yet"),
      el("span", { id: "clock" }),
    ];
    $("caption").replaceChildren(...bits);
    renderClock();
  }
  function renderClock() {
    const c = $("clock"), d = s.drop;
    if (!c || !d) return;
    if (d.state === "live" && s.clock) c.replaceChildren("closes in ", el("span", { class: "n", text: duration(s.clock()) }));
    else c.textContent = { ended: "time is up", closed: "closed", settling: "settling", settled: "settled" }[d.state] || "";
  }

  // ---- the card on the right
  function cardForm(d) {
    const name = el("input", { id: "name", autocomplete: "name", maxlength: "60", placeholder: "Your name", value: s.form.name,
      oninput: e => { s.form.name = e.target.value; } });
    const card = el("select", { id: "card", onchange: e => { s.form.card = parseInt(e.target.value, 10); } },
      s.cards.map(c => el("option", { value: c.index, selected: c.index === s.form.card, text: c.label })));
    const go = el("button", { class: "btn", type: "submit", disabled: s.busy, text: s.busy ? "Placing your hold…" : `Hold ${money(d.start_price)} and join` });
    const form = el("form", { onsubmit: join, novalidate: true },
      el("label", { class: "field" }, el("span", { text: "Name" }), name),
      el("label", { class: "field" }, el("span", { text: "Card" }), card),
      go);
    return [
      el("h2", { text: "Join the drop" }),
      el("p", { text: `We hold ${money(d.start_price)} on your card now. Nothing is charged until the drop closes. Then you pay the final price once (${money(d.price)}${d.next_step ? ", lower if more people join" : ""}), or nothing if fewer than ${d.minimum} join.` }),
      form,
      s.error ? el("p", { class: "msg stop", text: s.error }) : null,
      el("p", { class: "small", text: "You can leave until the drop closes; your hold is released at once." }),
    ];
  }

  function cardMine(d, me) {
    const out = [];
    if (me.status === "held") {
      out.push(el("div", { class: "you" }, el("div", { class: "label", text: "You're in" }), el("div", { class: "big", text: me.name })));
      out.push(el("dl", { class: "facts" },
        el("div", {}, el("dt", { text: "Held on your card" }), el("dd", { class: "v", text: money(me.held) })),
        el("div", {}, el("dt", { text: "Card" }), el("dd", { class: "v", text: (me.card || "").replace("Test card ending ", "test ···· ") })),
        el("div", {}, el("dt", { text: d.state === "live" ? "You pay at close" : "You pay" }), el("dd", { class: "v", text: d.state === "live" ? `${money(d.price)} or less` : money(d.price) }))));
      if (me.leaving) out.push(el("p", { class: "msg", text: "Your leave is recorded. PayPal will release the hold; you will not be charged." }));
      else if (me.can_leave && !s.confirmLeave) out.push(el("button", { class: "btn quiet", onclick: () => { s.confirmLeave = true; render(); }, text: "Leave the drop" }));
      else if (me.can_leave) out.push(
        el("p", { text: `Release your ${money(me.held)} hold and leave?` }),
        el("div", { class: "checks" },
          el("button", { class: "btn inline", disabled: s.busy, onclick: leave, text: s.busy ? "Releasing…" : "Yes, leave" }),
          el("button", { class: "btn quiet inline", onclick: () => { s.confirmLeave = false; render(); }, text: "Stay in" })));
      else out.push(el("p", { class: "small", text: "Joining has closed. Your hold is charged once, at the final price, when the maker settles." }));
    } else if (me.status === "captured") {
      out.push(el("div", { class: "you" }, el("div", { class: "label", text: me.name }), el("div", { class: "big", text: `You paid ${money(me.charged)}` })));
      out.push(el("p", { text: `That is the final price. The rest of your ${money(me.start_price)} hold was released.` }));
    } else if (me.status === "left") {
      out.push(el("div", { class: "you" }, el("div", { class: "label", text: me.name }), el("div", { class: "big", text: "You left" })));
      out.push(el("p", { text: `Your ${money(me.start_price)} hold was released. You pay nothing.` }));
      if (d.joinable) out.push(el("button", { class: "btn quiet", onclick: () => { store(KEY, null); s.me = null; render(); }, text: "Join again" }));
    } else if (me.status === "voided") {
      out.push(el("div", { class: "you" }, el("div", { class: "label", text: me.name }), el("div", { class: "big", text: "You paid nothing" })));
      out.push(el("p", { text: `The drop missed its minimum, so your ${money(me.start_price)} hold was released.` }));
    } else {
      out.push(el("div", { class: "you" }, el("div", { class: "label", text: me.name }), el("div", { class: "big", text: "Something needs the maker" })));
      out.push(el("p", { text: "Your card could not be charged or released as planned. The maker can see it and will sort it out; you are never charged more than the final price." }));
    }
    if (s.error) out.push(el("p", { class: "msg stop", text: s.error }));
    return out;
  }

  function cardClosed(d) {
    const settled = d.settled;
    if (settled) return [el("h2", { text: "This drop is settled" }),
      el("p", { text: settled.outcome === "funded" ? `${plural(settled.paid, "buyer")} paid ${money(settled.final_price)} each.` : "It missed its minimum. Every hold was released; nobody paid." })];
    return [el("h2", { text: "Joining has closed" }),
      el("p", { text: d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.` : `${d.buyers} of ${d.minimum} joined. Every hold will be released.` })];
  }

  function render() {
    if (s.gone) {
      $("join").replaceChildren(el("h2", { text: "This drop does not exist" }), el("p", {}, el("a", { href: "/", text: "See the drops" })));
      return;
    }
    const d = s.drop;
    if (!d) { if (s.error) $("join").replaceChildren(el("p", { class: "msg stop", text: s.error })); return; }
    renderLead(d);
    renderLadder(d);
    const join = $("join");
    const cardKey = JSON.stringify([s.me, d.joinable, d.state, d.price, d.start_price, d.minimum, d.minimum_met, !!d.next_step,
                                    d.settled, s.error, s.busy, s.confirmLeave, s.cards.length]);
    if (cardKey === s.lastCard) return;              // nothing on the card changed: leave it (and anyone typing in it) alone
    const active = document.activeElement && join.contains(document.activeElement) && ["INPUT", "SELECT"].includes(document.activeElement.tagName);
    if (active && !s.me && d.joinable && !s.error && !s.busy) return;      // do not rebuild the form under someone typing
    s.lastCard = cardKey;
    join.replaceChildren(...(s.me ? cardMine(d, s.me) : (d.joinable ? cardForm(d) : cardClosed(d))).filter(Boolean));
  }

  async function join(e) {
    e.preventDefault();
    if (s.busy) return;
    s.error = null;
    if (!s.form.name.trim()) { s.error = "Add your name."; render(); $("name") && $("name").focus(); return; }
    s.busy = true; render();
    try {
      const r = await api(`/api/drops/${dropId}/join`, { body: { name: s.form.name.trim(), card: s.form.card } });
      store(KEY, { buyer: r.buyer, token: r.token });
      s.me = r.me; s.drop = r.drop; s.form.name = "";
    } catch (err) {
      s.error = err.message;
    } finally {
      s.busy = false; render();
    }
  }

  async function leave() {
    if (s.busy) return;
    s.busy = true; s.error = null; render();
    try {
      const r = await api(`/api/drops/${dropId}/leave`, { body: store(KEY) });
      s.me = r.me; s.drop = r.drop; s.confirmLeave = false;
    } catch (err) {
      s.error = err.message;
    } finally {
      s.busy = false; render();
    }
  }

  (async () => {
    try { s.cards = await api("/api/cards"); } catch (e) { s.cards = []; }
    poll(tick, 2000);
    setInterval(renderClock, 1000);
    let w = 0;
    window.addEventListener("resize", () => { const nw = window.innerWidth; if (nw !== w) { w = nw; s.lastLadder = ""; if (s.drop) renderLadder(s.drop); } });
  })();
})();
