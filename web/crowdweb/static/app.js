// Crowd Price: shared by the pages. Every amount shown is a string the server sent; this file only formats it.
"use strict";

const CP = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const calm = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false)
      n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return n;
  }
  function svg(tag, attrs, ...kids) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) n.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid) n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return n;
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: Object.assign({}, opts.headers || {}) };
    if (opts.body !== undefined) {
      init.method = opts.method || "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let res, data;
    try {
      res = await fetch(path, init);
      data = await res.json();
    } catch (e) {
      throw { code: "offline", message: "The drop could not be reached." };
    }
    if (!res.ok || !data.ok) throw (data && data.error) || { code: "error", message: "That did not work." };
    return data.data;
  }

  // A browser secret: 32 random bytes, made BEFORE asking to join, so a join can be asked again and handed back.
  function secret() {
    const b = new Uint8Array(32);
    crypto.getRandomValues(b);
    return btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  // Which PayPal this server runs on, said plainly in the header and the footer of every page.
  const MODE = {
    fake: { env: "Test mode", more: " · no money moves", foot: "Every price and count here comes from the drop's engine. This copy runs in test mode, with a stand-in for PayPal: no money moves." },
    sandbox: { env: "PayPal sandbox", more: " · test money only", foot: "Every price and count here comes from the drop's engine. Holds are placed in the PayPal sandbox with test cards: no real money moves." },
  };
  function showMode(mode) {
    const m = MODE[mode];
    const env = document.getElementById("env"), foot = document.getElementById("foot");
    if (!m) { if (env) env.replaceChildren(); if (foot) foot.textContent = ""; return; }     // unknown: make no claim
    if (env) env.replaceChildren(m.env, el("span", { class: "more", text: m.more }));
    if (foot) foot.textContent = m.foot;
  }

  // "1665.00" -> "$1,665.00"; formatting only.
  function money(s) {
    if (s === null || s === undefined) return "";
    const [w, c] = String(s).split(".");
    return "$" + w.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (c !== undefined ? "." + c : "");
  }
  const short = s => money(s).replace(/\.00$/, "");
  function splitMoney(s) {
    const [w, c] = String(s).split(".");
    return { whole: w.replace(/\B(?=(\d{3})+(?!\d))/g, ","), cents: c !== undefined ? "." + c : "" };
  }
  function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + "s")}`; }

  // Time left, from the server's own clock reading, so a wrong clock on this device does not change it.
  function clockFrom(drop) {
    if (drop.time_left_seconds === null || drop.time_left_seconds === undefined) return null;
    const ends = Date.parse(drop.ends_at);
    const skew = Date.parse(drop.server_time) - Date.now();
    return () => Math.max(0, Math.floor((ends - (Date.now() + skew)) / 1000));
  }
  function duration(sec) {
    if (sec <= 0) return "0m";
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
    return `${m}m ${String(s).padStart(2, "0")}s`;
  }
  function hhmm(iso) {
    const t = new Date(iso);
    return isNaN(t) ? "" : t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  // ---- the ladder: price steps down as the crowd grows ------------------------------------------------
  // d: {steps:[{from_buyers, price}], buyers, minimum, minimum_met, locked_step}
  function ladder(host, d) {
    const W = Math.max(300, Math.round(host.getBoundingClientRect().width || 700));
    const narrow = W < 560;
    const H = narrow ? 200 : 228;
    const padL = 6, padR = narrow ? 12 : 18, padT = 44, padB = 34;
    const steps = d.steps;
    const last = steps[steps.length - 1].from_buyers;
    const xmax = Math.max(Math.ceil(last * 1.18), d.buyers + Math.ceil(last * 0.08), last + 6);
    const x = n => padL + (n / xmax) * (W - padL - padR);
    const hi = parseFloat(steps[0].price), lo = parseFloat(steps[steps.length - 1].price);
    const span = hi - lo || 1;
    const y = p => padT + 10 + ((hi - parseFloat(p)) / span) * (H - padT - padB - 26);
    const stepAt = n => { let i = 0; steps.forEach((s, j) => { if (n >= s.from_buyers) i = j; }); return i; };
    const locked = d.locked_step === null || d.locked_step === undefined ? -1 : d.locked_step;
    const reachN = Math.max(d.buyers, locked >= 0 ? steps[locked].from_buyers : 0);

    // the staircase as points, split where the crowd has got to
    const pts = [[x(0), y(steps[0].price)]];
    for (let i = 1; i < steps.length; i++) pts.push([x(steps[i].from_buyers), y(steps[i - 1].price)], [x(steps[i].from_buyers), y(steps[i].price)]);
    pts.push([x(xmax), y(steps[steps.length - 1].price)]);
    const cut = x(reachN), done = [], todo = [];
    for (let i = 0; i < pts.length; i++) {
      const [px, py] = pts[i];
      if (px <= cut) done.push([px, py]);
      if (i > 0) { const [qx, qy] = pts[i - 1]; if (qx < cut && px > cut) { done.push([cut, qy]); todo.push([cut, qy]); } }
      if (px >= cut) todo.push([px, py]);
    }
    const path = a => a.length > 1 ? "M" + a.map(p => p.map(v => v.toFixed(1)).join(" ")).join(" L") : "";

    host.replaceChildren();
    const s = svg("svg", { class: "ladder", viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
      "aria-label": `Price ladder: ${steps.map(t => `${money(t.price)} from ${t.from_buyers} buyers`).join(", ")}. ${d.buyers} joined.` });
    s.append(svg("line", { class: "axis", x1: padL, x2: W - padR, y1: H - padB, y2: H - padB }));
    const xm = x(d.minimum);
    s.append(svg("line", { class: "minline", x1: xm, x2: xm, y1: padT - 8, y2: H - padB }));
    s.append(svg("path", { class: "todo", d: path(todo) }));
    const donePath = (d.buyers > 0 || locked >= 0) ? svg("path", { class: d.minimum_met ? "done" : "done short", d: path(done) }) : null;
    if (donePath) s.append(donePath);

    // labels go where nothing else is: each takes the first free spot from its own short list
    const boxes = [];
    const hit = r => r.x1 < 0 || r.x2 > W || boxes.some(b => r.x1 < b.x2 && r.x2 > b.x1 && r.y1 < b.y2 && r.y2 > b.y1);
    const box = (bx, by, txt, size, anchor) => {
      const w = txt.length * size * 0.62;
      const x1 = anchor === "end" ? bx - w : (anchor === "middle" ? bx - w / 2 : bx);
      return { x1: x1 - 2, x2: x1 + w + 2, y1: by - size, y2: by + 4 };
    };
    const place = (cands, txt, size) => {
      for (const c of cands) { const b = box(c[0], c[1], txt, size, c[2]); if (!hit(b)) { boxes.push(b); return c; } }
      const c = cands[0]; boxes.push(box(c[0], c[1], txt, size, c[2])); return c;
    };

    // the crowd marker: its label first, the line it stands on, the dot
    const cx = x(d.buyers), cy = y(steps[stepAt(d.buyers)].price);
    const ctext = `${d.buyers} joined`;
    const anchor = cx > W - 80 ? "end" : (cx < 50 ? "start" : "middle");
    const ldx = anchor === "end" ? -8 : (anchor === "start" ? 8 : 0);
    boxes.push(box(cx + ldx, padT - 14, ctext, 13, anchor), { x1: cx - 2, x2: cx + 2, y1: padT - 2, y2: H - padB });
    const prev = host._prev;
    const circle = svg("circle", { cx: 0, cy: cy, r: 6.5 });
    const g = svg("g", { class: "crowd" },
      svg("line", { x1: 0, x2: 0, y1: padT - 2, y2: H - padB }), circle,
      svg("text", { x: ldx, y: padT - 14, "text-anchor": anchor }, ctext));
    s.append(g);

    // the price of each step, and its buyer count on the axis
    let lastTick = -Infinity;
    steps.forEach((t, i) => {
      const tx = i === 0 ? x(0) : x(t.from_buyers), ty = y(t.price), txt = short(t.price);
      const [px, py, pa] = place([[tx + 4, ty - 10, "start"], [tx + 4, ty + 22, "start"], [tx - 6, ty - 10, "end"], [tx - 6, ty + 22, "end"]], txt, 15);
      const cls = "price" + (i === locked ? " now" : (i < locked ? " reached" : ""));
      s.append(svg("text", { class: cls, x: px, y: py, "text-anchor": pa }, txt));
      const ax = x(t.from_buyers);
      s.append(svg("line", { class: "axis", x1: ax, x2: ax, y1: H - padB, y2: H - padB + 5 }));
      if (ax - lastTick > 26) { s.append(svg("text", { x: ax, y: H - padB + 20, "text-anchor": "middle" }, String(t.from_buyers))); lastTick = ax; }
    });
    const mtext = `minimum ${d.minimum}`;
    const [mx, my, ma] = place([[xm + 8, padT - 14, "start"], [xm - 8, padT - 14, "end"], [xm + 8, H - padB - 8, "start"], [xm - 8, H - padB - 8, "end"]], mtext, 12);
    s.append(svg("text", { x: mx, y: my, "text-anchor": ma }, mtext));
    const endFree = (W - padR) - lastTick > 70;
    s.append(svg("text", { x: endFree ? W - padR : padL, y: H - padB + 20, "text-anchor": endFree ? "end" : "start" }, "buyers"));

    // motion: the marker slides along, the dot springs down a step, newly reached ground is drawn in. The starting position is
    // set before the chart is attached, so the first draw never animates from the left edge.
    const move = prev && prev.W === W && !calm();      // a redraw at a new width places the marker; it never slides
    g.style.transform = `translateX(${move ? prev.cx : cx}px)`;
    circle.style.cy = `${move ? prev.cy : cy}px`;
    host.append(s);
    const doneLen = donePath ? donePath.getTotalLength() : 0;
    if (move && donePath && doneLen > prev.doneLen + 1) {
      donePath.style.strokeDasharray = `${doneLen}`;
      donePath.style.strokeDashoffset = `${doneLen - prev.doneLen}`;
    }
    host._prev = { cx, cy, doneLen, W };
    if (move) requestAnimationFrame(() => requestAnimationFrame(() => {
      g.style.transform = `translateX(${cx}px)`;
      circle.style.cy = `${cy}px`;
      if (donePath && donePath.style.strokeDasharray) {
        donePath.style.transition = "stroke-dashoffset 0.8s ease-out";
        donePath.style.strokeDashoffset = "0";
      }
    }));
  }

  // Re-run `tick` every `ms` while the page is visible, and at once when it comes back.
  function poll(tick, ms) {
    let t = null;
    const run = async () => { clearTimeout(t); try { await tick(); } finally { if (!document.hidden) t = setTimeout(run, ms); } };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) run(); });
    run();
  }

  function store(key, val, where) {
    try {
      const st = where || localStorage;
      if (val === undefined) return JSON.parse(st.getItem(key) || "null");
      if (val === null) st.removeItem(key); else st.setItem(key, JSON.stringify(val));
    } catch (e) { return null; }
  }

  return { el, svg, api, secret, showMode, money, short, splitMoney, plural, clockFrom, duration, hhmm, ladder, poll, store, calm };
})();
