"use strict";
(async () => {
  const list = document.getElementById("drops");
  try {
    const drops = await CP.api("/api/drops");
    const open = drops.filter(d => d.state === "live");
    if (open.length === 1 && drops.length >= 1) { location.replace(`/d/${open[0].drop}`); return; }
    for (const d of drops) {
      list.append(CP.el("li", {}, CP.el("a", { href: `/d/${d.drop}` },
        CP.el("span", {}, CP.el("span", { class: "t", text: d.title }), CP.el("br"),
          CP.el("span", { class: "muted", text: `${d.buyers} joined · minimum ${d.minimum}` })),
        CP.el("span", { class: "mono", text: `${CP.money(d.price)} · ${d.state === "live" ? d.time_left || "open" : d.state}` }))));
    }
    if (!drops.length) document.getElementById("empty").textContent = "No drops yet.";
  } catch (e) {
    document.getElementById("empty").textContent = e.message || "Cannot reach the drops right now.";
  }
})();
