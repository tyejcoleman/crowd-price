// The buyer's drop page. It shows the engine's numbers, and lets this browser join or leave with its own secret.
"use strict";
(() => {
  const { el, api, secret, showMode, money, short, splitMoney, plural, clockFrom, duration, ladder, poll, store } = CP;
  const dropId = location.pathname.split("/")[2];
  const KEY = "crowd-price:" + dropId;      // {name, card, secret, pending} until the join is confirmed, then {buyer, secret}
  const $ = id => document.getElementById(id);

  const s = { drop: null, me: null, cards: [], busy: false, error: null, clock: null, lastPrice: null, lastLadder: "",
              lastCard: "", confirmLeave: false, copied: false, form: { name: "", card: 0 }, gone: false };

  // ---- talking to the server
  async function tick() {
    const mine = store(KEY);
    try {
      if (mine && mine.buyer && mine.secret) {
        try {
          const r = await api(`/api/drops/${dropId}/me`, { body: { buyer: mine.buyer, secret: mine.secret } });
          s.drop = r.drop; s.me = r.me;
        } catch (e) {
          if (e.code !== "not_yours" && e.code !== "buyer_not_found") throw e;
          store(KEY, null); s.me = null;
          s.drop = await api(`/api/drops/${dropId}`);
        }
      } else if (mine && mine.pending) {
        await finishJoin(mine);              // a join whose answer never came: ask again with the same secret
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

  // Ask to join with a secret already written down. Asked again (same secret) after a lost answer, the server hands back
  // the same join; it never places a second hold.
  async function finishJoin(mine) {
    try {
      const r = await api(`/api/drops/${dropId}/join`, { body: { name: mine.name, card: mine.card, secret: mine.secret } });
      store(KEY, { buyer: r.buyer, secret: mine.secret });
      s.me = r.me; s.drop = r.drop; s.error = null;
    } catch (e) {
      if (e.code === "offline" || e.code === "paypal_unavailable" || e.code === "earlier_attempt_unresolved") {
        s.error = "We could not confirm your join yet. Checking again…";
        if (!s.drop) s.drop = await api(`/api/drops/${dropId}`);
      } else {
        store(KEY, null);
        s.error = e.message;
        s.drop = await api(`/api/drops/${dropId}`);
      }
    }
  }

  async function join(e) {
    e.preventDefault();
    if (s.busy) return;
    s.error = null;
    const name = s.form.name.trim();
    if (!name) { s.error = "Add your name."; render(); if ($("name")) $("name").focus(); return; }
    const mine = { name, card: s.form.card, secret: secret(), pending: true };
    store(KEY, mine);                        // written down before asking, so a lost answer can be asked again
    s.busy = true; render();
    try { await finishJoin(mine); if (s.me) s.form.name = ""; } finally { s.busy = false; render(); }
  }

  async function leave() {
    if (s.busy) return;
    s.busy = true; s.error = null; render();
    try {
      const mine = store(KEY);
      const r = await api(`/api/drops/${dropId}/leave`, { body: { buyer: mine.buyer, secret: mine.secret } });
      s.me = r.me; s.drop = r.drop; s.confirmLeave = false;
    } catch (err) {
      s.error = err.message;
    } finally {
      s.busy = false; render();
    }
  }

  async function share() {
    try { await navigator.clipboard.writeText(location.origin + location.pathname); s.copied = true; }
    catch (e) { s.copied = false; }
    render();
    setTimeout(() => { s.copied = false; render(); }, 2400);
  }

  // ---- what state the drop is in, in words
  const missed = d => (d.settled && d.settled.outcome !== "funded") || (["closed", "ended"].includes(d.state) && !d.minimum_met);
  const funded = d => d.settled && d.settled.outcome === "funded";
  const lockedFrom = d => (d.locked_step !== null && d.locked_step !== undefined) ? d.steps[d.locked_step].from_buyers : null;

  function renderLead(d) {
    document.title = `${d.title} · Crowd Price`;
    $("title").textContent = d.title + ".";
    $("by").replaceChildren(...(d.maker_name ? ["A drop by ", el("b", { text: d.maker_name })] : []));
    let turn = "The more people join, the lower the price, for everyone.";
    if (funded(d)) turn = `${d.settled.paid} people got it to ${short(d.settled.final_price)}.`;
    else if (missed(d)) turn = d.settled ? "It missed its minimum. Nobody paid." : "It missed its minimum. Nobody pays.";
    else if (d.state !== "live") turn = `${d.buyers} people got it to ${short(d.price)}.`;
    $("turn").textContent = turn;

    const det = d.details || {};
    const what = [det.what, det.ships].filter(Boolean).join(" · ");
    $("what").textContent = what; $("what").hidden = !what;
    const photo = $("photo");
    if (det.photo) { if (photo.getAttribute("src") !== det.photo) photo.src = det.photo; photo.alt = d.title; photo.hidden = false; }
    else photo.hidden = true;

    const price = $("price"), was = $("was");
    let label, next, saved = null;
    if (missed(d)) {
      label = "Missed its minimum";
      price.className = "price words";
      price.textContent = d.settled ? "Nobody paid" : "Nobody pays";
      next = `${short(d.start_price)} each if ${d.minimum} had joined. Every hold ${d.settled ? "was" : "is being"} released; the pending charge drops off your card, and when depends on your bank.`;
      s.lastPrice = null;
    } else {
      const shown = funded(d) ? d.settled.final_price : d.price;
      const { whole, cents } = splitMoney(shown);
      price.className = "price";
      price.replaceChildren(el("span", { class: "cur", text: "$" }), el("span", { text: whole }), el("span", { class: "cents", text: cents }));
      if (s.lastPrice !== null && parseFloat(shown) < parseFloat(s.lastPrice)) {      // the moment: the price just dropped
        price.classList.add("dropped");
        was.textContent = short(s.lastPrice); was.classList.remove("gone");
        setTimeout(() => { price.classList.remove("dropped"); was.classList.add("gone"); }, 2600);
      }
      s.lastPrice = shown;
      if (funded(d)) {
        label = "Final price";
        next = `${plural(d.settled.paid, "person", "people")} paid ${money(d.settled.final_price)} each. The rest of every hold was released.`;
        if (d.saved_each) saved = `${money(d.saved_each)} less than the ${money(d.start_price)} start price.`;
      } else if (d.state === "live") {
        label = d.minimum_met ? "Everyone pays" : `Everyone pays, once ${d.minimum} join`;
        next = d.next_message ? d.next_message.replace(/^./, c => c.toUpperCase()) + "." : "This is the lowest price on the ladder.";
        if (d.saved_each && lockedFrom(d)) saved = `Down from ${short(d.start_price)} because the crowd reached ${lockedFrom(d)}: ${money(d.saved_each)} less for everyone.`;
      } else {
        label = "Everyone pays";
        next = d.state === "settling" ? `Charging everyone ${money(d.price)} now.` : `Joining has closed. Each hold is charged ${money(d.price)} once, when the maker settles.`;
        if (d.saved_each) saved = `${money(d.saved_each)} less than the ${money(d.start_price)} start price.`;
      }
    }
    $("pricelabel").textContent = label;
    $("next").textContent = next;
    $("saved").textContent = saved || ""; $("saved").hidden = !saved;
  }

  function renderLadder(d) {
    const key = JSON.stringify([d.steps, d.buyers, d.locked_step, d.minimum_met, Math.round($("ladder").getBoundingClientRect().width)]);
    if (key !== s.lastLadder) { ladder($("ladder"), d); s.lastLadder = key; }
    s.clock = clockFrom(d);
    const live = d.state === "live";
    $("caption").replaceChildren(
      el("span", {}, el("span", { class: "n", text: String(d.buyers) }), " joined"),
      el("span", {}, "minimum ", el("span", { class: "n", text: String(d.minimum) }), d.minimum_met ? ", met" : (live ? ", not met yet" : ", not met")),
      el("span", { id: "clock" }));
    renderClock();
  }
  function renderClock() {
    const c = $("clock"), d = s.drop;
    if (!c || !d) return;
    if (d.state === "live" && s.clock) c.replaceChildren("closes in ", el("span", { class: "n", text: duration(s.clock()) }));
    else c.textContent = { ended: "time is up", closed: "closed", settling: "settling", settled: "settled" }[d.state] || "";
  }

  // ---- the card on the right
  function others(d) {
    const list = d.others || [];
    if (!list.length) return null;
    const who = d.maker_name || "This maker";
    return el("div", { class: "others" }, `${who} has ${list.length === 1 ? "another drop" : "other drops"} open: `,
      list.map((o, i) => [i ? ", " : "", el("a", { href: `/d/${encodeURIComponent(o.drop)}`, text: `${o.title} →` })]));
  }

  function cardForm(d) {
    const name = el("input", { id: "name", autocomplete: "name", maxlength: "60", placeholder: "Your name", value: s.form.name,
      oninput: e => { s.form.name = e.target.value; } });
    const card = el("select", { id: "card", onchange: e => { s.form.card = parseInt(e.target.value, 10); } },
      s.cards.map(c => el("option", { value: c.index, selected: c.index === s.form.card, text: c.label })));
    return [
      el("h2", { text: "Join the drop" }),
      el("p", { text: `We hold ${money(d.start_price)} on your card now. You're charged once, after the drop closes: the final price (${money(d.price)}${d.next_step ? ", lower if more people join" : ""}), or nothing if fewer than ${d.minimum} join.` }),
      el("form", { onsubmit: join, novalidate: true },
        el("label", { class: "field" }, el("span", { text: "Name" }), name),
        el("label", { class: "field" }, el("span", { text: "Card" }), card),
        el("button", { class: "btn", type: "submit", disabled: s.busy, text: s.busy ? "Placing your hold…" : `Hold ${money(d.start_price)} and join` })),
      s.error ? el("p", { class: "msg stop", text: s.error }) : null,
      el("p", { class: "small muted", text: "You can leave until it closes; your hold is released at once." }),
    ];
  }

  function cardMine(d, me) {
    const big = text => el("div", { class: "you" }, el("div", { class: "label", text: me.name }), el("div", { class: "big", text }));
    const out = [];
    if (me.status === "held") {
      const live = d.state === "live";
      out.push(el("div", { class: "you" }, el("div", { class: "label", text: `You're in, ${me.name}` }),
        el("div", { class: "big", text: live ? `You pay ${money(d.price)} or less` : `You pay ${money(d.price)}` })));
      out.push(el("dl", { class: "facts" },
        el("div", {}, el("dt", { text: "Held on your card" }), el("dd", { class: "v", text: money(me.held) })),
        el("div", {}, el("dt", { text: "Card" }), el("dd", { class: "v", text: (me.card || "").replace("Test card ending ", "test ···· ") }))));
      if (me.leaving) out.push(el("p", { class: "msg", text: "Your leave is recorded. PayPal will release the hold; you will not be charged." }));
      else if (live && !s.confirmLeave) {
        out.push(el("button", { class: "btn", onclick: share, text: s.copied ? "Link copied" : "Copy link to share" }),
          el("p", { class: "small muted", text: "Everyone you bring in can lower the price, for everyone." }),
          me.can_leave ? el("button", { class: "linkbtn", onclick: () => { s.confirmLeave = true; render(); }, text: "Leave the drop" }) : null);
      } else if (live && me.can_leave) {
        out.push(el("p", { text: `Release your ${money(me.held)} hold and leave?` }),
          el("div", { class: "checks" },
            el("button", { class: "btn inline", disabled: s.busy, onclick: leave, text: s.busy ? "Releasing…" : "Yes, leave" }),
            el("button", { class: "btn quiet inline", onclick: () => { s.confirmLeave = false; render(); }, text: "Stay in" })));
      } else out.push(el("p", { class: "small muted", text: "Joining has closed. Your hold is charged once, at the final price, when the maker settles." }));
    } else if (me.status === "captured") {
      out.push(big(`You paid ${money(me.charged)}`),
        el("p", { text: me.saved ? `${money(me.saved)} less than the ${money(me.start_price)} you held. The rest of your hold was released.`
                                 : `The rest of your ${money(me.start_price)} hold was released.` }), others(d));
    } else if (me.status === "left") {
      out.push(big("You left"), el("p", { text: `Your ${money(me.start_price)} hold was released. You pay nothing.` }));
      if (d.joinable) out.push(el("button", { class: "btn quiet", onclick: () => { store(KEY, null); s.me = null; render(); }, text: "Join again" }));
    } else if (me.status === "voided") {
      out.push(big("You paid nothing"),
        el("p", { text: "The drop missed its minimum, so your hold was released. The pending charge drops off your card; when depends on your bank." }), others(d));
    } else {
      out.push(big("Something needs the maker"),
        el("p", { text: "Your card could not be charged or released as planned. The maker sees it and will sort it out. You are never charged more than the final price." }));
    }
    if (s.error) out.push(el("p", { class: "msg stop", text: s.error }));
    return out;
  }

  function cardPending() {
    return [el("h2", { text: "Confirming your join…" }),
      el("p", { text: "If PayPal placed your hold, you are in. If not, nothing was held. This page checks again by itself." }),
      s.error ? el("p", { class: "msg", text: s.error }) : null];
  }

  function cardClosed(d) {
    if (d.settled) return [el("h2", { text: "This drop is settled" }),
      el("p", { text: funded(d) ? `${plural(d.settled.paid, "person", "people")} paid ${money(d.settled.final_price)} each.` : "It missed its minimum. Every hold was released; nobody paid." }),
      others(d)];
    return [el("h2", { text: "Joining has closed" }),
      el("p", { text: d.minimum_met ? `${d.buyers} joined. Everyone pays ${money(d.price)}.` : `${d.buyers} of ${d.minimum} joined, so every hold will be released.` })];
  }

  // ---- drawing it
  function render() {
    if (s.gone) {
      $("page").hidden = true;
      $("status").replaceChildren("This drop does not exist. ", el("a", { href: "/", text: "See the drops" }));
      return;
    }
    const d = s.drop;
    if (!d) { if (s.error) $("status").textContent = s.error; return; }
    $("status").hidden = true; $("page").hidden = false;
    showMode(d.paypal);
    renderLead(d);
    renderLadder(d);
    const pending = !s.me && (store(KEY) || {}).pending;
    const cardKey = JSON.stringify([s.me, pending, d.joinable, d.state, d.price, d.start_price, d.minimum, d.minimum_met, !!d.next_step,
                                    d.settled, d.others, s.error, s.busy, s.confirmLeave, s.copied, s.cards.length]);
    const join = $("join");
    const typing = document.activeElement && join.contains(document.activeElement) && ["INPUT", "SELECT"].includes(document.activeElement.tagName);
    if (cardKey !== s.lastCard && !(typing && !s.me && d.joinable && !s.error && !s.busy)) {
      s.lastCard = cardKey;
      join.replaceChildren(...(s.me ? cardMine(d, s.me) : (pending ? cardPending() : (d.joinable ? cardForm(d) : cardClosed(d)))).filter(Boolean));
    }
    sticky(d);
  }

  // on a phone, the join button follows you down the page until the card is in view
  let cardVisible = true;
  function sticky(d) {
    const want = d.joinable && !s.me && !cardVisible && window.innerWidth <= 860;
    $("sticky").hidden = !want;
    $("stickybtn").textContent = `Hold ${money(d.start_price)} and join`;
  }

  (async () => {
    try { s.cards = await api("/api/cards"); } catch (e) { s.cards = []; }
    $("stickybtn").addEventListener("click", () => { $("join").scrollIntoView({ behavior: CP.calm() ? "auto" : "smooth", block: "center" }); setTimeout(() => $("name") && $("name").focus(), 400); });
    if ("IntersectionObserver" in window) new IntersectionObserver(es => { cardVisible = es[0].isIntersecting; if (s.drop) sticky(s.drop); }).observe($("join"));
    poll(tick, 2000);
    setInterval(renderClock, 1000);
    let w = window.innerWidth;
    window.addEventListener("resize", () => { if (window.innerWidth !== w) { w = window.innerWidth; s.lastLadder = ""; if (s.drop) render(); } });
  })();
})();
