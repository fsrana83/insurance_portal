/* Make/model suggestions for the datalists (popular in the Gulf market). */
(function () {
  var data = {
    Toyota: ["Corolla", "Camry", "Yaris", "Land Cruiser", "Prado", "Hilux", "Fortuner", "RAV4", "Hiace", "Innova"],
    Nissan: ["Sunny", "Altima", "Patrol", "X-Trail", "Kicks", "Navara", "Urvan"],
    Hyundai: ["Accent", "Elantra", "Sonata", "Tucson", "Santa Fe", "Creta", "H1"],
    Kia: ["Picanto", "Rio", "Cerato", "Sportage", "Sorento", "Carnival"],
    Mitsubishi: ["Lancer", "Pajero", "L200", "Outlander", "ASX"],
    Honda: ["Civic", "Accord", "CR-V", "City"],
    Ford: ["Explorer", "Edge", "F-150", "Ranger", "Mustang"],
    Chevrolet: ["Tahoe", "Suburban", "Silverado", "Captiva"],
    Isuzu: ["D-Max", "NPR", "NQR", "FVR"],
    Mercedes: ["C-Class", "E-Class", "S-Class", "GLE", "G-Class"],
    BMW: ["3 Series", "5 Series", "X5", "X3"],
    Suzuki: ["Swift", "Dzire", "Vitara", "Ertiga"],
    Mazda: ["3", "6", "CX-5", "CX-9"],
    Lexus: ["LX 570", "RX", "ES"],
    "Land Rover": ["Defender", "Range Rover", "Discovery"],
    Mack: ["Granite", "Anthem"], Hino: ["300", "500"], Tata: ["Ace", "Prima"], Yamaha: ["YZF", "NMAX"], Honda_M: []
  };
  var makes = document.getElementById("make-list"), models = document.getElementById("model-list");
  if (!makes) return;
  Object.keys(data).forEach(function (m) { if (!data[m].length) return; var o = document.createElement("option"); o.value = m; makes.appendChild(o); });
  function fillModels(make) {
    models.innerHTML = "";
    (data[make] || []).forEach(function (m) { var o = document.createElement("option"); o.value = m; models.appendChild(o); });
  }
  document.addEventListener("focusin", function (e) {
    if (e.target.name && /-model$/.test(e.target.name)) {
      var mk = document.querySelector('[name="' + e.target.name.replace("-model", "-make") + '"]');
      fillModels(mk ? mk.value : "");
    }
  });
})();

/* Release 10: live make / model catalogue with a + button, and customer-type dependent fields. */
(function () {
  var catUrl = "/api/vehicle-catalogue/";
  var data = {};
  var makes = document.getElementById("make-list"), models = document.getElementById("model-list");
  function csrf() { var t = document.querySelector('[name=csrfmiddlewaretoken]'); return t ? t.value : ""; }
  function opt(list, v) { var o = document.createElement("option"); o.value = v; list.appendChild(o); }
  function refresh() {
    if (!makes) return;
    makes.innerHTML = "";
    Object.keys(data).sort().forEach(function (m) { opt(makes, m); });
  }
  function fillModels(make) {
    if (!models) return;
    models.innerHTML = "";
    (data[make] || []).forEach(function (m) { opt(models, m); });
  }
  if (makes) {
    fetch(catUrl, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) { data = d; refresh(); }).catch(function () {});
    document.addEventListener("focusin", function (e) {
      if (e.target.name && /-model$/.test(e.target.name)) {
        var mk = document.querySelector('[name="' + e.target.name.replace("-model", "-make") + '"]');
        fillModels(mk ? mk.value : "");
      }
    });
    document.addEventListener("click", function (e) {
      var b = e.target.closest("[data-add-catalogue]");
      if (!b) return;
      var field = b.closest(".field"), input = field.querySelector("input");
      var isModel = /-model$/.test(input.name);
      var make = isModel ? (document.querySelector('[name="' + input.name.replace("-model", "-make") + '"]') || {}).value : "";
      var name = input.value.trim() || window.prompt(isModel ? "New model name for " + (make || "this maker") : "New maker name");
      if (!name) return;
      var body = new URLSearchParams(isModel ? { make: make, model: name } : { make: name });
      if (isModel && !make) { window.alert("Choose or type the maker first."); return; }
      fetch("/api/vehicle-catalogue/add/", { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": csrf() }, body: body })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (d.error) { window.alert(d.error); return; }
          data[d.make] = data[d.make] || [];
          if (d.model && data[d.make].indexOf(d.model) < 0) data[d.make].push(d.model);
          input.value = name; refresh(); fillModels(make || d.make);
        });
    });
    // add the + buttons to make / model fields
    function addButtons(root) {
      (root || document).querySelectorAll(".field.has-add").forEach(function (f) {
        if (f.querySelector("[data-add-catalogue]")) return;
        var b = document.createElement("button"); b.type = "button"; b.className = "btn ghost small"; b.setAttribute("data-add-catalogue", "1"); b.title = "Add to the list"; b.textContent = "+";
        f.appendChild(b);
      });
    }
    addButtons(document);
    new MutationObserver(function () { addButtons(document); }).observe(document.body, { childList: true, subtree: true });
  }
  // customer type: show ID card + date of birth for an individual, CR number + CR dates for a fleet / corporate customer
  var sel = document.querySelector('[name="new_customer_type"], [name="customer_type"]');
  if (sel) {
    var toggle = function () {
      document.querySelectorAll("[data-ct]").forEach(function (el) { el.hidden = el.getAttribute("data-ct") !== sel.value; });
    };
    sel.addEventListener("change", toggle); toggle();
  }
})();
