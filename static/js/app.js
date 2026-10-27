/* Advance Motor Insurance Solution front-end behaviour. Vanilla JS, no build step. */
(function () {
  "use strict";

  // ---- currency symbol support (new Omani rial / UAE dirham signs are Unicode 18) ----------------
  var glyphCache = {};
  var canvas = document.createElement("canvas");
  canvas.width = canvas.height = 60;
  var ctx = canvas.getContext && canvas.getContext("2d", { willReadFrequently: true });

  // Draw a character and return the size of what was painted. A font that lacks the glyph paints a "tofu"
  // box (or nothing), which has the same footprint as any other unassigned code point, so we compare the
  // candidate against U+20CF (unassigned in the currency block) rather than comparing exact pixels.
  function footprint(ch) {
    ctx.clearRect(0, 0, 60, 60);
    ctx.font = "32px " + getComputedStyle(document.body).fontFamily;
    ctx.textBaseline = "top";
    ctx.fillText(ch, 6, 6);
    var d = ctx.getImageData(0, 0, 60, 60).data, x0 = 60, y0 = 60, x1 = -1, y1 = -1;
    for (var y = 0; y < 60; y++) for (var x = 0; x < 60; x++) {
      if (d[(y * 60 + x) * 4 + 3] > 24) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    }
    return x1 < 0 ? null : { w: x1 - x0 + 1, h: y1 - y0 + 1 };
  }

  function glyphSupported(ch) {
    if (!ctx || /^[\x20-\x7e]+$/.test(ch)) return true; // plain ASCII always renders
    if (ch in glyphCache) return glyphCache[ch];
    var a = footprint(ch), tofu = footprint("\u20CF");
    var ok = !!a && !(tofu && Math.abs(a.w - tofu.w) <= 1 && Math.abs(a.h - tofu.h) <= 1);
    return (glyphCache[ch] = ok);
  }

  function applySymbol(el, symbol, fallback, img) {
    el.classList.remove("is-code");
    if (glyphSupported(symbol)) { el.textContent = symbol; return; }
    if (img) {
      el.textContent = "";
      var i = document.createElement("img");
      i.src = img; i.alt = fallback; el.appendChild(i);
    } else {
      el.textContent = fallback;
      if (/^[A-Z]{3}$/.test(fallback)) el.classList.add("is-code");
    }
  }

  function fixSymbols(root) {
    (root || document).querySelectorAll(".money .cur").forEach(function (el) {
      if (el.dataset.done) return;
      el.dataset.done = "1";
      applySymbol(el, el.textContent.trim(), el.dataset.fallback || "", el.dataset.img);
    });
  }

  var curData = {};
  var curNode = document.getElementById("currency-data");
  if (curNode) { try { curData = JSON.parse(curNode.textContent); } catch (e) {} }

  var override = null;
  function currencyCode() {
    if (override) return override;
    var s = document.querySelector("[data-currency-source]");
    return s ? s.value : null;
  }
  window.MotorPAS = { setCurrency: function (code) { override = code; updateMoneySymbols(); } };

  function updateMoneySymbols() {
    var code = currencyCode();
    var c = code && curData[code];
    document.querySelectorAll("[data-money-sym]").forEach(function (el) {
      if (c) applySymbol(el, c.symbol, c.fallback, null);
    });
  }

  function moneyNode(code, amount) {
    var c = curData[code] || { symbol: code, fallback: code };
    var wrap = document.createElement("span"); wrap.className = "money";
    var cur = document.createElement("span"); cur.className = "cur"; wrap.appendChild(cur);
    applySymbol(cur, c.symbol, c.fallback, null);
    var amt = document.createElement("span"); amt.className = "amt"; amt.textContent = amount;
    wrap.appendChild(amt);
    return wrap;
  }

  // ---- mobile nav --------------------------------------------------------------------------------
  var rail = document.getElementById("rail");
  var burger = document.getElementById("burger");
  if (burger && rail) {
    burger.addEventListener("click", function () { rail.classList.toggle("open"); });
    document.addEventListener("click", function (e) {
      if (rail.classList.contains("open") && !rail.contains(e.target) && e.target !== burger && !burger.contains(e.target)) rail.classList.remove("open");
    });
  }

  // ---- tabs ----------------------------------------------------------------------------------------
  document.querySelectorAll("[data-tabs]").forEach(function (group) {
    var tabs = group.querySelectorAll("[role=tab]");
    function show(id) {
      tabs.forEach(function (t) { t.setAttribute("aria-selected", String(t.dataset.target === id)); });
      document.querySelectorAll("[data-tabpanel]").forEach(function (p) { p.hidden = p.id !== id; });
      try { history.replaceState(null, "", "#" + id); } catch (e) {}
    }
    tabs.forEach(function (t) { t.addEventListener("click", function () { show(t.dataset.target); }); });
    var start = location.hash && group.querySelector('[data-target="' + location.hash.slice(1) + '"]') ? location.hash.slice(1) : tabs[0].dataset.target;
    show(start);
  });

  // ---- quote form --------------------------------------------------------------------------------
  var form = document.getElementById("quote-form");
  if (form) initQuoteForm(form);

  function initQuoteForm(form) {
    var container = document.getElementById("vehicles");
    var tpl = document.getElementById("vehicle-template");
    var total = form.querySelector("[name=risks-TOTAL_FORMS]");
    var summary = document.getElementById("summary");
    var previewUrl = form.dataset.preview;
    var timer = null, inflight = null;
    var products = JSON.parse((document.getElementById("product-meta") || { textContent: "{}" }).textContent);

    // customer: show/hide the "new customer" block
    var custSel = form.querySelector("[name=customer]");
    var newCust = document.getElementById("new-customer");
    function syncCustomer() { if (custSel && newCust) newCust.hidden = !!custSel.value; }
    if (custSel) { custSel.addEventListener("change", syncCustomer); syncCustomer(); }

    // policy period: expiry follows inception (12 months)
    var start = form.querySelector("[name=start_date]"), end = form.querySelector("[name=end_date]");
    if (start && end) {
      start.addEventListener("change", function () {
        if (!start.value) return;
        var d = new Date(start.value + "T00:00:00"); d.setFullYear(d.getFullYear() + 1); d.setDate(d.getDate() - 1);
        end.value = d.toISOString().slice(0, 10); schedule();
      });
    }

    function selectedProduct() {
      var r = form.querySelector("[name=product]:checked");
      return r ? r.value : null;
    }

    function syncProduct() {
      var pid = selectedProduct(), meta = products[pid] || {};
      // sum insured only matters for comprehensive cover
      container.querySelectorAll("[data-si-field]").forEach(function (f) { f.hidden = meta.includes_od === false; });
      // lock mandatory riders for this product
      container.querySelectorAll(".rider-chip").forEach(function (chip) {
        var ids = (chip.dataset.mandatoryFor || "").split(",").filter(Boolean);
        var box = chip.querySelector("input"), tag = chip.querySelector(".rider-chip__tag");
        var mandatory = ids.indexOf(pid) !== -1;
        chip.classList.toggle("is-locked", mandatory);
        if (mandatory) { box.checked = true; box.dataset.locked = "1"; tag.textContent = chip.dataset.regulatory ? "FSA" : "Included"; }
        else { delete box.dataset.locked; tag.textContent = ""; }
      });
      var disc = form.querySelectorAll("[data-discount-field]");
      disc.forEach(function (d) { d.hidden = !!meta.regulated; });
    }
    form.addEventListener("click", function (e) {
      var chip = e.target.closest && e.target.closest(".rider-chip.is-locked");
      if (chip) { e.preventDefault(); }
    });

    // vehicles -----------------------------------------------------------------------------------
    function renumber() {
      var n = 0;
      container.querySelectorAll(".vehicle").forEach(function (v) {
        if (v.classList.contains("is-deleted")) return;
        n++; var t = v.querySelector("[data-vehicle-title]"); if (t) t.textContent = "Vehicle " + n;
      });
      var visible = n;
      container.querySelectorAll("[data-remove]").forEach(function (b) { b.hidden = visible <= 1; });
    }

    function addVehicle(values) {
      var idx = parseInt(total.value, 10);
      var html = tpl.innerHTML.replace(/__prefix__/g, idx);
      var holder = document.createElement("div"); holder.innerHTML = html.trim();
      var node = holder.firstElementChild;
      container.appendChild(node);
      total.value = idx + 1;
      if (values) fill(node, idx, values);
      wire(node);
      syncProduct(); renumber(); schedule();
      return node;
    }

    function fill(node, idx, v) {
      Object.keys(v).forEach(function (k) {
        var els = node.querySelectorAll('[name="risks-' + idx + "-" + k + '"]');
        els.forEach(function (el) {
          if (el.type === "radio") el.checked = el.value === String(v[k]);
          else if (el.type === "checkbox") { var arr = Array.isArray(v[k]) ? v[k].map(String) : [String(v[k])]; el.checked = arr.indexOf(el.value) !== -1; }
          else el.value = v[k];
        });
      });
    }

    container.querySelectorAll(".vehicle").forEach(wire);

    function wire(node) {
      var del = node.querySelector("[data-remove]");
      if (del) del.addEventListener("click", function () {
        var box = node.querySelector("input[name$=-DELETE]");
        if (box) box.checked = true;
        node.classList.add("is-deleted"); renumber(); schedule();
      });
      // chassis / plate lookup to re-use existing vehicles
      var ch = node.querySelector("input[name$=-chassis_no]");
      if (ch) {
        var holder = ch.parentElement; holder.classList.add("lookup");
        var list = document.createElement("div"); list.className = "lookup__list"; list.hidden = true; holder.appendChild(list);
        var t2;
        ch.addEventListener("input", function () {
          clearTimeout(t2);
          if (ch.value.length < 4) { list.hidden = true; return; }
          t2 = setTimeout(function () {
            fetch(form.dataset.lookup + "?q=" + encodeURIComponent(ch.value)).then(function (r) { return r.json(); }).then(function (data) {
              list.innerHTML = ""; if (!data.results.length) { list.hidden = true; return; }
              data.results.forEach(function (v) {
                var b = document.createElement("button"); b.type = "button"; b.textContent = v.label + " · " + v.chassis_no;
                b.addEventListener("click", function () {
                  var idx = node.dataset.index; fill(node, idx, { vehicle_id: v.id, make: v.make, model: v.model, year: v.year, plate_code: v.plate_code,
                    plate_number: v.plate_number, chassis_no: v.chassis_no, engine_no: v.engine_no, color: v.color, vehicle_type: v.vehicle_type,
                    usage: v.usage, cc: v.cc, seats: v.seats });
                  list.hidden = true; schedule();
                });
                list.appendChild(b);
              }); list.hidden = false;
            });
          }, 250);
        });
        document.addEventListener("click", function (e) { if (!holder.contains(e.target)) list.hidden = true; });
      }
    }

    document.getElementById("add-vehicle").addEventListener("click", function () {
      var n = addVehicle(); n.scrollIntoView({ behavior: "smooth", block: "center" });
    });

    // currency, product & any field change -> refresh summary
    form.addEventListener("change", function (e) {
      if (e.target.name === "currency") updateMoneySymbols();
      if (e.target.name === "product") syncProduct();
      schedule();
    });
    form.addEventListener("input", function (e) { if (e.target.type === "number") schedule(); });

    function schedule() { clearTimeout(timer); timer = setTimeout(refresh, 350); }

    function refresh() {
      if (!previewUrl) return;
      if (inflight) inflight.abort();
      inflight = new AbortController();
      summary.classList.add("is-loading");
      fetch(previewUrl, { method: "POST", body: new FormData(form), signal: inflight.signal, headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) { return r.json(); })
        .then(render)
        .catch(function (e) { if (e.name !== "AbortError") summary.classList.remove("is-loading"); });
    }

    function row(label, valueNode, cls) {
      var r = document.createElement("div"); r.className = "row" + (cls ? " " + cls : "");
      var l = document.createElement("span"); l.textContent = label;
      var v = document.createElement("span"); v.appendChild(valueNode);
      r.appendChild(l); r.appendChild(v); return r;
    }

    function render(d) {
      summary.classList.remove("is-loading");
      var body = summary.querySelector("[data-summary-body]"); body.innerHTML = "";
      if (!d.ok) {
        var e = document.createElement("p"); e.className = "err"; e.textContent = d.error; body.appendChild(e); return;
      }
      d.lines.forEach(function (l) {
        var h = document.createElement("div"); h.className = "veh"; h.textContent = l.label; body.appendChild(h);
        if (d.includes_od) body.appendChild(row("Own damage", moneyNode(d.currency, l.od), "sub"));
        body.appendChild(row("Third-party liability (tariff)", moneyNode(d.currency, l.tp), "sub"));
        if (l.rider_lines.length) body.appendChild(row("Riders & extensions", moneyNode(d.currency, l.riders), "sub"));
        l.disc_lines.forEach(function (x) { var t = document.createElement("span"); t.textContent = "−" + x.pct + "%"; body.appendChild(row(x.label, t, "sub")); });
        body.appendChild(row("Premium", moneyNode(d.currency, l.net)));
      });
      if (d.voucher) {
        var vt = document.createElement("span");
        if (d.voucher.error) { vt.textContent = d.voucher.error; vt.style.color = "var(--bad, #b3261e)"; body.appendChild(row("Voucher " + d.voucher.code, vt, "sub")); }
        else { body.appendChild(row("Voucher " + d.voucher.code + " (" + d.voucher.label + ")", moneyNode(d.currency, "−" + d.voucher.applied), "sub")); }
      }
      body.appendChild(row("Policy fee", moneyNode(d.currency, d.fees)));
      body.appendChild(row("VAT " + d.vat_pct + "%", moneyNode(d.currency, d.tax)));
      var t = document.createElement("div"); t.className = "total";
      var tl = document.createElement("span"); tl.textContent = "Total payable";
      var tv = document.createElement("strong"); tv.appendChild(moneyNode(d.currency, d.total));
      t.appendChild(tl); t.appendChild(tv); body.appendChild(t);
      if (d.regulated) { var n = document.createElement("p"); n.className = "note"; n.textContent = "FSA-regulated tariff: discounts do not apply."; body.appendChild(n); }
    }

    // fleet rows loaded from an uploaded file are rendered server-side. Wire them all.
    syncProduct(); renumber(); updateMoneySymbols(); schedule();
  }

  // ---- endorsement form: show only the fields that apply ---------------------------------------
  var endo = document.getElementById("endorse-form");
  if (endo) {
    var sel = endo.querySelector("[name=endorsement_type]");
    var map = { EXTENSION: ["new_end_date"], CANCELLATION: [], TRANSFER: ["risk", "new_owner"], ENGINE: ["risk", "new_engine_no"],
                COLOR: ["risk", "new_color"], USAGE: ["risk", "new_usage"], ADDITIONS: ["risk", "accessories_value", "accessories_note"],
                REGNO: ["risk", "new_plate_code", "new_plate_number"],
                ADDDRIVER: ["risk", "driver_name", "driver_licence", "driver_dob", "driver_nationality", "driver_relationship"] };
    var sync = function () {
      var show = map[sel.value] || [];
      var conditional = [].concat.apply([], Object.keys(map).map(function (k) { return map[k]; }));
      endo.querySelectorAll("[data-endo-field]").forEach(function (f) {
        var n = f.dataset.endoField;
        if (conditional.indexOf(n) !== -1) f.hidden = show.indexOf(n) === -1;
      });
    };
    sel.addEventListener("change", sync); sync();
    endo.addEventListener("change", function () { updateMoneySymbols(); });
  }

  // generic: any page with a currency source + money inputs
  document.addEventListener("change", function (e) { if (e.target.matches && e.target.matches("[data-currency-source]")) updateMoneySymbols(); });

  // ---- confirm dialogs ---------------------------------------------------------------------------
  document.addEventListener("submit", function (e) {
    var msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  fixSymbols(); updateMoneySymbols();
})();
