/* Advance Motor Insurance Solution — page extras: customer policy picker, additional drivers,
   vehicle damage map, AI estimate, depreciated value hints. Vanilla JS, no dependencies. */
(function () {
  "use strict";
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text != null) e.textContent = text;
    return e;
  }
  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ""; }

  // ---------------------------------------------------------------- customer search picker (quote screen)
  (function () {
    var wrap = document.getElementById("cust-picker"); if (!wrap) return;
    var hidden = document.getElementById("id_customer"), q = document.getElementById("cust-q"), list = document.getElementById("cust-results");
    var chosen = document.getElementById("cust-chosen"), search = document.getElementById("cust-search"), hint = document.getElementById("cust-hint");
    var timer = null, seq = 0;
    function show(c) {
      hidden.value = c ? c.id : "";
      document.getElementById("cust-name").textContent = c ? c.name : "";
      document.getElementById("cust-meta").textContent = c ? [c.type, c.identifier, c.phone].filter(Boolean).join(" · ") : "";
      chosen.hidden = !c; search.hidden = !!c; hint.hidden = !!c; list.hidden = true;
      hidden.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function run() {
      var text = q.value.trim(), my = ++seq;
      if (text.length < 2) { list.hidden = true; return; }
      fetch(wrap.dataset.url + "?q=" + encodeURIComponent(text), { headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (my !== seq) return;
          list.innerHTML = "";
          if (!d.results.length) { var n = document.createElement("li"); n.className = "is-empty"; n.textContent = "No customer found. Fill in the new customer's details below."; list.appendChild(n); }
          d.results.forEach(function (c) {
            var li = document.createElement("li"); li.setAttribute("role", "option"); li.tabIndex = 0;
            li.innerHTML = "<strong></strong><small></small>";
            li.firstChild.textContent = c.name;
            li.lastChild.textContent = [c.identifier, c.phone, c.policies != null ? c.policies + " policies" : ""].filter(Boolean).join(" · ");
            li.addEventListener("click", function () { show(c); });
            li.addEventListener("keydown", function (e) { if (e.key === "Enter") show(c); });
            list.appendChild(li);
          });
          list.hidden = false;
        });
    }
    q.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(run, 220); });
    document.addEventListener("click", function (e) { if (!wrap.contains(e.target)) list.hidden = true; });
    document.getElementById("cust-clear").addEventListener("click", function () { show(null); q.value = ""; q.focus(); });
    if (hidden.value) {
      fetch(wrap.dataset.url + "?id=" + encodeURIComponent(hidden.value)).then(function (r) { return r.json(); }).then(function (d) { if (d.results[0]) show(d.results[0]); });
    }
  })();

  // ---------------------------------------------------------------- customer policy picker (quote screen)
  var box = document.getElementById("customer-policies");
  var sel = document.getElementById("id_customer");
  if (box && sel) {
    var load = function () {
      box.innerHTML = ""; box.hidden = true;
      var nc = document.getElementById("new-customer"); if (nc) nc.hidden = !!sel.value;
      if (!sel.value) return;
      fetch(box.dataset.url + "?customer=" + encodeURIComponent(sel.value), { headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) { return r.json(); }).then(function (d) {
          box.hidden = false;
          box.appendChild(el("strong", {}, d.customer.name + " — policies on file (" + d.policies.length + ")"));
          if (!d.policies.length) { box.appendChild(el("p", { "class": "help" }, "No policies yet. Complete this quote to issue a new one.")); return; }
          var t = el("table"), h = el("thead"), hr = el("tr");
          ["Policy", "Vehicle", "Period", "Status", "Total", ""].forEach(function (x) { hr.appendChild(el("th", {}, x)); });
          h.appendChild(hr); t.appendChild(h);
          var b = el("tbody");
          d.policies.forEach(function (p) {
            var tr = el("tr"), a = el("a", { href: p.url }, p.policy_no), c0 = el("td"); c0.appendChild(a); tr.appendChild(c0);
            tr.appendChild(el("td", {}, p.vehicles)); tr.appendChild(el("td", {}, p.start + " – " + p.end));
            var st = el("td"); st.appendChild(el("span", { "class": "badge " + (p.status === "ACTIVE" ? "ok" : "warn") }, p.status.charAt(0) + p.status.slice(1).toLowerCase())); tr.appendChild(st);
            tr.appendChild(el("td", {}, p.currency + " " + p.total));
            var act = el("td");
            if (p.open_renewal) { act.appendChild(el("a", { "class": "btn ghost small", href: p.open_renewal.url }, "Open renewal " + p.open_renewal.number)); }
            else if (p.renewable) {
              var f = el("form", { method: "post", action: p.renew_url, style: "display:inline" });
              f.appendChild(el("input", { type: "hidden", name: "csrfmiddlewaretoken", value: csrf() }));
              f.appendChild(el("button", { "class": "btn gold small", type: "submit" }, "Renew")); act.appendChild(f);
            } else if (p.renewed) { act.appendChild(el("span", { "class": "help" }, "Renewed")); }
            tr.appendChild(act); b.appendChild(tr);
          });
          t.appendChild(b); box.appendChild(t);
          box.appendChild(el("p", { "class": "help" }, "Choose a policy to renew, or carry on below to issue a new policy for this customer."));
        });
    };
    sel.addEventListener("change", load); load();
  }

  // ---------------------------------------------------------------- additional drivers (vehicle cards)
  var KEYS = [["name", "Full name", "text"], ["licence_no", "Licence no.", "text"], ["date_of_birth", "Date of birth", "date"],
              ["licence_issue_date", "Licence issued", "date"], ["nationality", "Nationality", "text"]];
  function initDrivers(root) {
    root.querySelectorAll("[data-drivers]").forEach(function (c) {
      if (c.dataset.ready) return; c.dataset.ready = "1";
      var hidden = c.querySelector("input[name$='drivers_json']"), rows = c.querySelector("[data-driver-rows]");
      if (!hidden) return;
      var list = []; try { list = JSON.parse(hidden.value || "[]"); } catch (e) { list = []; }
      function sync() {
        var out = [];
        rows.querySelectorAll(".driver-row").forEach(function (r) {
          var o = {}; r.querySelectorAll("[data-k]").forEach(function (i) { if (i.value) o[i.dataset.k] = i.value; });
          if (Object.keys(o).length) out.push(o);
        });
        hidden.value = out.length ? JSON.stringify(out) : "";
      }
      function add(d) {
        var r = el("div", { "class": "driver-row" });
        KEYS.forEach(function (k) {
          var i = el("input", { "class": "input", type: k[2], placeholder: k[1], "aria-label": k[1], "data-k": k[0] }); i.value = (d && d[k[0]]) || ""; r.appendChild(i);
        });
        var rel = el("select", { "class": "select", "data-k": "relationship", "aria-label": "Relationship" });
        [["", "Relationship"], ["SPOUSE", "Spouse"], ["CHILD", "Child"], ["FAMILY", "Family"], ["EMPLOYEE", "Employee"], ["FRIEND", "Friend"], ["OTHER", "Other"]].forEach(function (o) {
          var op = el("option", { value: o[0] }, o[1]); rel.appendChild(op); });
        rel.value = (d && d.relationship) || ""; r.appendChild(rel);
        var x = el("button", { type: "button", "class": "btn ghost small", "aria-label": "Remove driver" }, "×");
        x.addEventListener("click", function () { r.remove(); sync(); hidden.dispatchEvent(new Event("change", { bubbles: true })); }); r.appendChild(x);
        r.addEventListener("input", sync); r.addEventListener("change", sync); rows.appendChild(r);
      }
      list.forEach(add);
      c.querySelector("[data-add-driver]").addEventListener("click", function () { add(); });
    });
  }
  initDrivers(document);
  var vs = document.getElementById("vehicles");
  if (vs) new MutationObserver(function () { initDrivers(vs); }).observe(vs, { childList: true, subtree: true });
  // the form validates the hidden JSON on submit: make sure it is current
  var qf = document.getElementById("quote-form");
  if (qf) qf.addEventListener("submit", function () { qf.querySelectorAll(".driver-row input").forEach(function (i) { i.dispatchEvent(new Event("input", { bubbles: true })); }); });

  // ---------------------------------------------------------------- damage map
  var map = document.getElementById("damage-map");
  if (map) {
    var form = map.closest("form"), field = form && form.querySelector("[name=damage_zones]");
    var zones = {}; try { zones = JSON.parse(map.dataset.zones || "{}"); } catch (e) { zones = {}; }
    if (field && field.value) { try { zones = JSON.parse(field.value); } catch (e) {} }
    var ro = map.dataset.readonly === "1", names = {};
    var NAMES = { front_bumper: "Front bumper", hood: "Bonnet", windscreen: "Windscreen", roof: "Roof", rear_glass: "Rear glass", trunk: "Boot / tailgate", rear_bumper: "Rear bumper",
      fl_wing: "Front-left wing", fl_door: "Front-left door", rl_door: "Rear-left door", rl_quarter: "Rear-left quarter",
      fr_wing: "Front-right wing", fr_door: "Front-right door", rr_door: "Rear-right door", rr_quarter: "Rear-right quarter" };
    var LAB = { 1: "Minor", 2: "Moderate", 3: "Severe" };
    function paint() {
      map.querySelectorAll(".dm-zone").forEach(function (z) {
        var s = zones[z.dataset.zone] || 0; z.setAttribute("class", "dm-zone" + (s ? " s" + s : ""));
        z.setAttribute("aria-label", NAMES[z.dataset.zone] + ": " + (s ? LAB[s] : "no damage"));
      });
      var ul = document.getElementById("damage-list"); ul.innerHTML = "";
      Object.keys(zones).forEach(function (k) { if (zones[k]) ul.appendChild(el("li", {}, NAMES[k] + " — " + LAB[zones[k]])); });
      if (!ul.children.length) ul.appendChild(el("li", { "class": "help" }, ro ? "No damage zones recorded." : "No damage marked yet."));
      if (field) field.value = JSON.stringify(zones);
    }
    map.querySelectorAll(".dm-zone").forEach(function (z) {
      z.setAttribute("tabindex", ro ? "-1" : "0"); z.setAttribute("role", "button");
      if (ro) return;
      var bump = function () { var k = z.dataset.zone, n = ((zones[k] || 0) + 1) % 4; if (n) zones[k] = n; else delete zones[k]; paint(); };
      z.addEventListener("click", bump);
      z.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); bump(); } });
    });
    paint();
  }

  // ---------------------------------------------------------------- claim form: policy search, vehicle fetched from the policy
  (function () {
    var wrap = document.getElementById("pol-picker"); if (!wrap) return;
    var hidden = document.getElementById("id_policy"), q = document.getElementById("pol-q"), list = document.getElementById("pol-results");
    var chosen = document.getElementById("pol-chosen"), box = document.getElementById("pol-search"), risk = document.getElementById("id_risk");
    var drv = document.querySelector("[name=driver]"), card = document.getElementById("vehicle-card"), timer = null, seq = 0;
    function opt(sel, value, label) { var o = document.createElement("option"); o.value = value; o.textContent = label; sel.appendChild(o); }
    function paintCard(r) {
      card.hidden = !r; if (!r) return;
      card.innerHTML = "";
      var t = document.createElement("strong"); t.textContent = r.make + " " + r.model + " " + r.year; card.appendChild(t);
      var s = document.createElement("small"); s.textContent = [r.plate, "chassis " + r.chassis_no, r.type, r.usage, r.colour, "SI " + Number(r.sum_insured).toLocaleString()].filter(Boolean).join(" \u00b7 "); card.appendChild(s);
      if (r.vehicle_id) { var a = document.createElement("a"); a.href = "/vehicles/" + r.vehicle_id + "/"; a.textContent = "Vehicle profile"; a.target = "_blank"; card.appendChild(a); }
    }
    function load(policyId, keepRisk) {
      fetch(wrap.dataset.vehicles.replace(/0\/vehicles\/$/, policyId + "/vehicles/")).then(function (r) { return r.json(); }).then(function (d) {
        var cur = keepRisk && risk.value;
        risk.innerHTML = ""; d.risks.forEach(function (r) { opt(risk, r.id, r.label); });
        if (cur && d.risks.some(function (r) { return String(r.id) === cur; })) risk.value = cur;
        var sync = function () {
          var r = d.risks.filter(function (x) { return String(x.id) === risk.value; })[0]; paintCard(r);
          if (drv) { var keep = drv.value; drv.innerHTML = ""; opt(drv, "", "Not on the policy / undisclosed driver"); (r ? r.drivers : []).forEach(function (x) { opt(drv, x.id, x.label); }); if (keep) drv.value = keep; }
        };
        risk.onchange = function () { sync(); }; sync();
        risk.closest(".field").classList.toggle("is-single", d.risks.length === 1);
        risk.dispatchEvent(new Event("change", { bubbles: true }));
        hidden.dispatchEvent(new CustomEvent("policycurrency", { detail: d.policy.currency }));
      });
    }
    function show(p, keep) {
      hidden.value = p ? p.id : "";
      document.getElementById("pol-name").textContent = p ? p.policy_no + " \u2014 " + p.customer : "";
      document.getElementById("pol-meta").textContent = p ? [p.product, p.period, p.in_force ? "In force" : p.status].join(" \u00b7 ") : "";
      chosen.hidden = !p; box.hidden = !!p; list.hidden = true;
      if (p) load(p.id, keep); else { risk.innerHTML = ""; opt(risk, "", "Select a policy first"); paintCard(null); }
    }
    function run() {
      var text = q.value.trim(), my = ++seq; if (text.length < 2) { list.hidden = true; return; }
      fetch(wrap.dataset.search + "?q=" + encodeURIComponent(text)).then(function (r) { return r.json(); }).then(function (d) {
        if (my !== seq) return; list.innerHTML = "";
        if (!d.results.length) { var n = document.createElement("li"); n.className = "is-empty"; n.textContent = "No policy found."; list.appendChild(n); }
        d.results.forEach(function (p) {
          var li = document.createElement("li"); li.tabIndex = 0; li.innerHTML = "<strong></strong><small></small>";
          li.firstChild.textContent = p.policy_no + " \u2014 " + p.customer;
          li.lastChild.textContent = [p.vehicles, p.period, p.in_force ? "In force" : p.status].filter(Boolean).join(" \u00b7 ");
          li.addEventListener("click", function () { show(p, false); });
          li.addEventListener("keydown", function (e) { if (e.key === "Enter") show(p, false); });
          list.appendChild(li);
        });
        list.hidden = false;
      });
    }
    q.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(run, 220); });
    document.addEventListener("click", function (e) { if (!wrap.contains(e.target)) list.hidden = true; });
    document.getElementById("pol-clear").addEventListener("click", function () { show(null); q.value = ""; q.focus(); });
    if (hidden.value) {
      fetch(wrap.dataset.vehicles.replace(/0\/vehicles\/$/, hidden.value + "/vehicles/")).then(function (r) { return r.json(); }).then(function (d) { show(d.policy, true); });
    }
  })();

  // ---------------------------------------------------------------- claim form: risk info, total loss, AI estimate
  var cf = document.getElementById("claim-form");
  if (cf) {
    var risk = cf.querySelector("[name=risk]"), pol = cf.querySelector("[name=policy]"), dol = cf.querySelector("[name=date_of_loss]");
    var drv = cf.querySelector("[name=driver]"), tl = cf.querySelector("[name=is_total_loss]"), info = document.getElementById("tl-info");
    var estField = cf.querySelector("[name=estimated_damage]"), ai = null;
    function riskInfo() {
      if (!risk || !risk.value) return;
      fetch(cf.dataset.riskInfo.replace(/0\/$/, risk.value + "/") + "?date=" + encodeURIComponent(dol.value || "")).then(function (r) { return r.json(); }).then(function (d) {
        if (drv) { var keep = drv.value; Array.prototype.forEach.call(drv.options, function (o) { if (o.value) o.hidden = !d.drivers.some(function (x) { return String(x.id) === o.value; }); });
          if (keep && drv.selectedOptions[0] && drv.selectedOptions[0].hidden) drv.value = ""; }
        var m = info && info.querySelector(".msg");
        if (m) m.textContent = "Depreciated market value at the date of loss: " + d.currency + " " + Number(d.depreciated_value).toLocaleString(undefined, { minimumFractionDigits: 3 }) +
          " (reducing balance " + d.depreciation_pct + "% p.a., capped at the sum insured " + Number(d.sum_insured).toLocaleString() + ").";
      });
    }
    function tlSync() { if (info) info.hidden = !(tl && tl.checked); }
    [risk, dol].forEach(function (x) { if (x) x.addEventListener("change", riskInfo); });
    if (tl) tl.addEventListener("change", tlSync); tlSync(); riskInfo();
    if (drv) drv.addEventListener("change", function () {
      var m = drv.selectedOptions[0] && drv.selectedOptions[0].textContent.match(/age (\d+)/);
      var age = cf.querySelector("[name=driver_age]"), young = cf.querySelector("[name=young_driver]");
      if (m && age) { age.value = m[1]; if (young) young.checked = Number(m[1]) < 25; }
    });
    var run = document.getElementById("ai-run"), out = document.getElementById("ai-out"), use = document.getElementById("ai-use");
    if (run) run.addEventListener("click", function () {
      var zs = {}; try { zs = JSON.parse(cf.querySelector("[name=damage_zones]").value || "{}"); } catch (e) {}
      out.innerHTML = ""; if (!risk.value) { out.appendChild(el("p", { "class": "ai-warn" }, "Choose the policy and vehicle first.")); return; }
      run.disabled = true; out.appendChild(el("p", { "class": "help" }, "Estimating… a local model can take up to a minute."));
      fetch(cf.dataset.estimate, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({ risk: risk.value, zones: zs, loss_type: cf.querySelector("[name=loss_type]").value, notes: (cf.querySelector("[name=damage_notes]") || {}).value || "", model: (cf.querySelector("[name=ai_model]") || {}).value || "" }) })
        .then(function (r) { return r.json(); }).then(function (d) {
          run.disabled = false; out.innerHTML = ""; ai = d;
          if (!d.ok) { out.appendChild(el("p", { "class": "ai-warn" }, d.error || "Estimate failed.")); return; }
          cf.querySelector("[name=ai_estimate_json]").value = JSON.stringify(d);
          var f = function (v) { return Number(v).toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 }); };
          if (d.expected) {
            out.appendChild(el("div", { "class": "ai-big" }, d.currency + " " + f(d.expected)));
            out.appendChild(el("div", { "class": "help" }, "Range " + f(d.low) + " – " + f(d.high) + " · source: " + (d.source === "ollama" ? "AI model " + d.model + " + company data" : "company data") + " (" + d.n + " similar claims)"));
          }
          if (d.reasoning) out.appendChild(el("p", {}, d.reasoning));
          if (d.likely_parts && d.likely_parts.length) out.appendChild(el("p", { "class": "help" }, "Likely parts: " + d.likely_parts.join(", ")));
          (d.warnings || []).forEach(function (w) { out.appendChild(el("p", { "class": "ai-warn" }, w)); });
          if (d.expected) { use.hidden = false; }
        }).catch(function () { run.disabled = false; out.innerHTML = ""; out.appendChild(el("p", { "class": "ai-warn" }, "The estimate request failed.")); });
    });
    if (use) use.addEventListener("click", function () { if (ai && estField) { estField.value = ai.expected; estField.dispatchEvent(new Event("change", { bubbles: true })); } });
  }

  // ---------------------------------------------------------------- demo-data page: poll progress
  var dp = document.getElementById("demo-progress");
  if (dp && dp.dataset.url && dp.dataset.running === "1") {
    var tick = function () {
      fetch(dp.dataset.url).then(function (r) { return r.json(); }).then(function (d) {
        var bar = dp.querySelector(".progressbar > span"), txt = dp.querySelector("[data-txt]");
        if (bar) bar.style.width = (d.total ? Math.round(100 * d.done / d.total) : 5) + "%";
        if (txt) txt.textContent = d.stage + (d.total ? " — " + d.done + " / " + d.total : "");
        if (d.status === "RUNNING") setTimeout(tick, 1500); else window.location.reload();
      }).catch(function () { setTimeout(tick, 3000); });
    };
    tick();
  }
})();


/* Buses, trucks, trailers and heavy equipment: usage can only be Commercial or Government */
(function () {
  var HEAVY = ["BUS", "TRUCK", "TRAILER"], OK = ["COMMERCIAL", "GOVERNMENT"];
  function apply(typeInput) {
    var m = typeInput.name.match(/^(.*)vehicle_type$/);
    if (!m || !typeInput.checked) return;
    var heavy = HEAVY.indexOf(typeInput.value) >= 0;
    var usages = document.querySelectorAll('input[type=radio][name="' + m[1] + 'usage"]');
    var fix = false;
    usages.forEach(function (u) {
      var bad = heavy && OK.indexOf(u.value) < 0;
      u.disabled = bad;
      var lab = u.closest("label"); if (lab) lab.style.opacity = bad ? ".35" : "";
      if (bad && u.checked) fix = true;
    });
    if (fix) usages.forEach(function (u) { if (u.value === "COMMERCIAL") { u.checked = true; u.dispatchEvent(new Event("change", { bubbles: true })); } });
  }
  document.addEventListener("change", function (e) { if (e.target.matches && e.target.matches("input[type=radio][name$=vehicle_type]")) apply(e.target); });
  document.addEventListener("DOMContentLoaded", function () { document.querySelectorAll("input[type=radio][name$=vehicle_type]:checked").forEach(apply); });
})();
