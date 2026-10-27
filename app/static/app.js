(function () {
  const STORAGE_KEY = "omnitennis.criteria.v2";
  const form = document.getElementById("criteria");
  const PERSISTED = ["min_minutes", "indoor"];
  let forceNext = false;

  function clubBoxes() {
    return Array.from(form.querySelectorAll('input[name="club"]'));
  }

  function readState() {
    const s = {};
    for (const name of PERSISTED) s[name] = form.elements[name].value;
    s.clubs_unchecked = clubBoxes().filter((b) => !b.checked).map((b) => b.value);
    return s;
  }

  function save() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(readState()));
    } catch (_) {}
  }

  function restore() {
    let s;
    try {
      s = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
    } catch (_) {
      s = null;
    }
    if (!s) return;
    for (const name of PERSISTED) {
      if (s[name] != null && form.elements[name]) form.elements[name].value = s[name];
    }
    const unchecked = new Set(s.clubs_unchecked || []);
    clubBoxes().forEach((b) => (b.checked = !unchecked.has(b.value)));
  }

  function applyDefaults() {
    const d = window.OMNI_DEFAULTS || {};
    for (const name of ["date_from", "date_to", ...PERSISTED]) {
      if (d[name] != null && form.elements[name]) form.elements[name].value = d[name];
    }
    clubBoxes().forEach((b) => (b.checked = true));
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (_) {}
  }

  // Turn the club checkboxes into a single "clubs" CSV param and persist criteria.
  form.addEventListener("htmx:configRequest", function (evt) {
    const p = evt.detail.parameters;
    const boxes = clubBoxes();
    const checked = boxes.filter((b) => b.checked).map((b) => b.value);
    delete p.club;
    if (checked.length === boxes.length) delete p.clubs;
    else p.clubs = checked.length ? checked.join(",") : "__none__";
    if (forceNext) {
      p.force = "1";
      forceNext = false;
    }
    save();
  });

  document.getElementById("refresh").addEventListener("click", function () {
    forceNext = true;
    form.requestSubmit();
  });

  document.getElementById("reset").addEventListener("click", function () {
    applyDefaults();
    form.requestSubmit();
  });

  // Auto-search on change of any field (debounced via htmx would be nicer; keep it simple).
  form.addEventListener("change", function () {
    if (form.checkValidity()) form.requestSubmit();
  });

  restore();
})();
