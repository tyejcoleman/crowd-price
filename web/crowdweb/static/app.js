// Crowd Price: shared by both pages. Every amount shown is a string the engine produced; this file only formats it.
"use strict";

const CP = (() => {
  const NS = "http://www.w3.org/2000/svg";

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false)
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
      throw { code: "offline", message: "Cannot reach the drop right now. Retrying." };
    }
    if (!res.ok || !data.ok) throw (data && data.error) || { code: "error", message: "That did not work." };
    return data.data;
  }

  // "1665.00" -> "$1,665.00"; formatting only.
  function money(s) {
    if (s === null || s === undefined) return "";
    const [w, c] = String(s).split(".");
    return "$" + w.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (c !== undefined ? "." + c : "");
  }
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
    return isNaN(t) ? "" : t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // ---- the ladder: price steps down as the crowd grows ------------------------------------------------
  // d: {steps:[{from_buyers, price}], buyers, minimum, minimum_met, locked_step}. profit (maker only): [{from_buyers, profit}]
  function ladder(host, d, profit) {
    const W = Math.max(300, Math.round(host.getBoundingClientRect().width || 900));
    const narrow = W < 560;
    const H = narrow ? 210 : 260;
    const padL = 6, padR = narrow ? 18 : 28, padT = 46, padB = 34;
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
    for (let i = 1; i < steps.length; i++) {
      pts.push([x(steps[i].from_buyers), y(steps[i - 1].price)], [x(steps[i].from_buyers), y(steps[i].price)]);
    }
    pts.push([x(xmax), y(steps[steps.length - 1].price)]);
    const cut = x(reachN);
    const done = [], todo = [];
    for (let i = 0; i < pts.length; i++) {
      const [px, py] = pts[i];
      if (px <= cut) done.push([px, py]);
      if (i > 0) {
        const [qx, qy] = pts[i - 1];
        if (qx < cut && px > cut) { done.push([cut, qy]); todo.push([cut, qy]); }
      }
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
    if (d.buyers > 0 || locked >= 0) s.append(svg("path", { class: d.minimum_met ? "done" : "done short", d: path(done) }));

    // the crowd marker
    const cx = x(d.buyers), cy = y(steps[stepAt(d.buyers)].price);
    const anchor = cx > W - 90 ? "end" : (cx < 60 ? "start" : "middle");
    const g = svg("g", { class: "crowd" },
      svg("line", { x1: 0, x2: 0, y1: padT - 2, y2: H - padB }),
      svg("circle", { cx: 0, cy: cy, r: 6.5 }),
      svg("text", { x: anchor === "end" ? -8 : (anchor === "start" ? 8 : 0), y: padT - 26 + 12, "text-anchor": anchor }, `${d.buyers} joined`));
    g.style.transform = `translateX(${host._cx === undefined ? cx : host._cx}px)`;   // slide from where it was
    s.append(g);
    steps.forEach((t, i) => {
      const tx = i === 0 ? x(0) : x(t.from_buyers);
      const cls = "price" + (i === locked ? " now" : (i < locked ? " reached" : ""));
      s.append(svg("text", { class: cls, x: tx + 4, y: y(t.price) - 12 }, money(t.price).replace(/\.00$/, "")));
      const lastStep = i === steps.length - 1 && tx > W - 170;
      if (profit && profit[i] && !narrow) s.append(svg("text", { class: "profit", x: lastStep ? W - padR : tx + 4, y: y(t.price) + 22,
        "text-anchor": lastStep ? "end" : null }, `you make ${money(profit[i].profit)}`));
      const ax = x(t.from_buyers);
      s.append(svg("line", { class: "axis", x1: ax, x2: ax, y1: H - padB, y2: H - padB + 5 }));
      s.append(svg("text", { x: ax, y: H - padB + 20, "text-anchor": "middle" }, String(t.from_buyers)));
    });
    // the minimum's label sits at the top unless the crowd's label is there; then it moves down beside the axis
    const crowdNear = Math.abs(x(d.buyers) - xm) < (narrow ? 130 : 150);
    s.append(svg("text", { x: xm + 6, y: crowdNear ? H - padB - 8 : padT - 12 }, `minimum ${d.minimum}`));

    host.append(s);
    host._cx = cx;
    requestAnimationFrame(() => requestAnimationFrame(() => { g.style.transform = `translateX(${cx}px)`; }));
  }

  // Re-run `tick` every `ms` while the page is visible, and at once when it comes back.
  function poll(tick, ms) {
    let t = null;
    const run = async () => { clearTimeout(t); try { await tick(); } finally { if (!document.hidden) t = setTimeout(run, ms); } };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) run(); });
    run();
  }

  function store(key, val) {
    try {
      if (val === undefined) return JSON.parse(localStorage.getItem(key) || "null");
      if (val === null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(val));
    } catch (e) { return null; }
  }

  return { el, svg, api, money, splitMoney, plural, clockFrom, duration, hhmm, ladder, poll, store };
})();
