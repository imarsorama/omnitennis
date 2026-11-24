/* Static viewer for availability.json (GitHub Pages build of OmniTennis). */
(function () {
  "use strict";

  const STORAGE_KEY = "omnitennis.criteria.v2";
  const PERSISTED = ["min_minutes", "indoor"];
  const UNIT = 30; // minutes; blocks are built from 30-min units
  const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

  const form = document.getElementById("criteria");
  const results = document.getElementById("results");
  const info = document.getElementById("snapshot-info");
  let data = null;
  let defaults = {};

  // ----------------------------------------------------------------- utils

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function hm(min) {
    const m = ((min % 1440) + 1440) % 1440;
    return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
  }

  function parseDay(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  }

  function weekday(iso) {
    return (parseDay(iso).getUTCDay() + 6) % 7; // Monday = 0
  }

  function fmtDayLong(iso) {
    const d = parseDay(iso);
    return `${DAY_NAMES[weekday(iso)]}, ${String(d.getUTCDate()).padStart(2, "0")} ${MONTHS_LONG[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  }

  function fmtDayShort(iso) {
    const d = parseDay(iso);
    return `${DAY_NAMES[weekday(iso)].slice(0, 3)} ${String(d.getUTCDate()).padStart(2, "0")} ${MONTHS[d.getUTCMonth()]}`;
  }

  /** Current wall-clock in the snapshot's time zone as {day: "YYYY-MM-DD", min: minutesSinceMidnight}. */
  function nowIn(tz) {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).formatToParts(new Date());
    const get = (t) => parts.find((p) => p.type === t).value;
    return { day: `${get("year")}-${get("month")}-${get("day")}`, min: Number(get("hour")) * 60 + Number(get("minute")) };
  }

  function clubBoxes() {
    return Array.from(form.querySelectorAll('input[name="club"]'));
  }

  // ------------------------------------------------------------ persistence

  function save() {
    const s = {};
    for (const name of PERSISTED) s[name] = form.elements[name].value;
    s.clubs_unchecked = clubBoxes().filter((b) => !b.checked).map((b) => b.value);
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(s)); } catch (_) {}
  }

  function restore() {
    let s = null;
    try { s = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); } catch (_) {}
    if (!s) return;
    for (const name of PERSISTED) if (s[name] != null && form.elements[name]) form.elements[name].value = s[name];
    const unchecked = new Set(s.clubs_unchecked || []);
    clubBoxes().forEach((b) => (b.checked = !unchecked.has(b.value)));
  }

  function applyDefaults() {
    for (const name of ["date_from", "date_to", ...PERSISTED]) {
      if (defaults[name] != null && form.elements[name]) form.elements[name].value = defaults[name];
    }
    clubBoxes().forEach((b) => (b.checked = true));
    try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
  }

  // --------------------------------------------------------------- filtering

  function readCriteria() {
    const boxes = clubBoxes();
    const checked = boxes.filter((b) => b.checked).map((b) => b.value);
    return {
      date_from: form.elements.date_from.value,
      date_to: form.elements.date_to.value,
      min_minutes: Number(form.elements.min_minutes.value) || 120,
      indoor: form.elements.indoor.value,
      clubs: checked.length === boxes.length ? null : new Set(checked),
    };
  }

  /** Mirror app/criteria.py: drop past units, keep whole blocks >= min_minutes, apply indoor/club/date filters. */
  function filterBlocks(blocks, c, now) {
    const out = [];
    for (const b0 of blocks) {
      if (b0.day < c.date_from || b0.day > c.date_to) continue;
      if (c.clubs && !c.clubs.has(b0.club)) continue;
      if (c.indoor === "indoor" && b0.indoor !== true) continue;
      if (c.indoor === "outdoor" && b0.indoor !== false) continue;
      let b = b0;
      if (b.day < now.day) continue;
      if (b.day === now.day && b.start_min < now.min) {
        const start = Math.ceil(now.min / UNIT) * UNIT; // first unit that has not started yet
        if (start >= b.end_min) continue;
        b = Object.assign({}, b, { start_min: start, minutes: b.end_min - start });
      }
      if (b.minutes < c.min_minutes) continue;
      out.push(b);
    }
    return out;
  }

  // --------------------------------------------------------------- rendering

  function groupBlocks(blocks, clubsByKey) {
    const days = new Map();
    for (const b of blocks) {
      if (!days.has(b.day)) days.set(b.day, new Map());
      const clubs = days.get(b.day);
      if (!clubs.has(b.club)) clubs.set(b.club, new Map());
      const courts = clubs.get(b.club);
      if (!courts.has(b.court)) courts.set(b.court, { court: b, blocks: [] });
      courts.get(b.court).blocks.push(b);
    }
    const clubName = (k) => (clubsByKey[k] ? clubsByKey[k].name : k);
    return Array.from(days.keys()).sort().map((day) => {
      const clubs = Array.from(days.get(day).entries())
        .sort((a, b) => clubName(a[0]).localeCompare(clubName(b[0])))
        .map(([key, courts]) => {
          const list = Array.from(courts.values())
            .sort((a, b) => a.court.court_name.localeCompare(b.court.court_name))
            .map((c) => ({ court: c.court, blocks: c.blocks.slice().sort((x, y) => x.start_min - y.start_min) }));
          return { club: clubsByKey[key] || { key, name: key, url: "#" }, courts: list, count: list.reduce((n, c) => n + c.blocks.length, 0) };
        });
      return { day, clubs, count: clubs.reduce((n, c) => n + c.count, 0) };
    });
  }

  function axisFor(days) {
    let lo = 24 * 60, hi = 0;
    for (const d of days) for (const c of d.clubs) for (const ct of c.courts) for (const b of ct.blocks) {
      lo = Math.min(lo, b.start_min);
      hi = Math.max(hi, b.end_min);
    }
    if (hi <= lo) { lo = 7 * 60; hi = 23 * 60; }
    lo = Math.floor(lo / 60) * 60;
    hi = Math.min(24 * 60, Math.ceil(hi / 60) * 60);
    const span = hi - lo;
    const hours = [];
    for (let h = lo; h <= hi; h += 60) hours.push({ label: String(Math.floor(h / 60)).padStart(2, "0"), left: ((h - lo) / span) * 100 });
    return {
      hours,
      pos(s, e) {
        s = Math.max(s, lo); e = Math.min(e, hi);
        return { left: ((s - lo) / span) * 100, width: (Math.max(e - s, 0) / span) * 100 };
      },
    };
  }

  function renderStatus(c, total, providers, now) {
    const badges = providers.map((p) => {
      const title = p.error ? p.error : p.fetched_at ? `fetched ${p.fetched_at.slice(11, 19)}${p.elapsed_ms ? ` in ${p.elapsed_ms} ms` : ""}` : "";
      const name = p.club ? p.club.name : p.provider;
      return `<span class="badge badge-${esc(p.status)}" title="${esc(title)}">${esc(name)}: ${esc(p.status)}${p.status !== "error" ? ` (${p.blocks})` : ""}</span>`;
    }).join("");
    const errors = providers.filter((p) => p.status === "error")
      .map((p) => `<div class="error">${esc(p.club ? p.club.name : p.provider)} failed: ${esc(p.error || "")}</div>`).join("");
    return `<div class="status-bar">
      <span class="total"><strong>${total}</strong> free block${total === 1 ? "" : "s"}
        from ${esc(fmtDayShort(c.date_from))} to ${esc(fmtDayShort(c.date_to))}, min ${c.min_minutes} min</span>
      <span class="providers">${badges}</span>
      <span class="legend"><span class="lg lg-free"></span> free (click to book)</span>
    </div>${errors}`;
  }

  function renderDays(days, axis) {
    if (!days.length) return '<p class="empty">No free blocks match the current criteria.</p>';
    const head = `<div class="tl-row tl-head"><div class="tl-label"></div><div class="tl-track">${
      axis.hours.map((h) => `<span class="tl-hour" style="left:${h.left.toFixed(3)}%">${h.label}</span>`).join("")}</div></div>`;
    const grid = axis.hours.map((h) => `<i class="tl-grid" style="left:${h.left.toFixed(3)}%"></i>`).join("");
    return days.map((d) => {
      const rows = d.clubs.map((c) => {
        const clubRow = `<div class="tl-club"><a href="${esc(c.club.url)}" target="_blank" rel="noopener">${esc(c.club.name)}</a>${
          c.club.address ? `<small>${esc(c.club.address)}</small>` : ""}</div>`;
        const courts = c.courts.map((ct) => {
          const tags = (ct.court.indoor === true ? '<span class="tag">indoor</span>' : ct.court.indoor === false ? '<span class="tag">outdoor</span>' : "")
            + (ct.court.surface ? `<span class="tag">${esc(ct.court.surface)}</span>` : "");
          const blocks = ct.blocks.map((b) => {
            const p = axis.pos(b.start_min, b.end_min);
            const dur = `${Math.floor(b.minutes / 60)}h${b.minutes % 60 ? String(b.minutes % 60).padStart(2, "0") : ""}`;
            const label = `${hm(b.start_min)}–${hm(b.end_min)}`;
            return `<a class="blk" href="${esc(b.booking_url)}" target="_blank" rel="noopener" style="left:${p.left.toFixed(3)}%;width:${p.width.toFixed(3)}%" title="${esc(ct.court.court_name)}: ${label} (${dur}) – click to book"><span>${label}</span></a>`;
          }).join("");
          return `<div class="tl-row"><div class="tl-label"><span class="name">${esc(ct.court.court_name)}</span><span class="meta">${tags}</span></div><div class="tl-track">${grid}${blocks}</div></div>`;
        }).join("");
        return clubRow + courts;
      }).join("");
      return `<section class="day ${weekday(d.day) >= 5 ? "weekend" : "weekday"}"><h2>${esc(fmtDayLong(d.day))} <small>${d.count}</small></h2><div class="timeline">${head}${rows}</div></section>`;
    }).join("");
  }

  function render() {
    if (!data) return;
    const c = readCriteria();
    if (!c.date_from || !c.date_to || c.date_to < c.date_from) return;
    const now = nowIn(data.timezone || "Europe/Zagreb");
    const clubsByKey = {};
    for (const p of data.providers) if (p.club) clubsByKey[p.club.key] = p.club;
    const blocks = filterBlocks(data.blocks, c, now);
    const days = groupBlocks(blocks, clubsByKey);
    const providers = data.providers.filter((p) => !c.clubs || (p.club && c.clubs.has(p.club.key)));
    results.innerHTML = renderStatus(c, blocks.length, providers, now) + renderDays(days, axisFor(days));
    save();
  }

  function describeSnapshot() {
    const gen = new Date(data.generated_at);
    const ageMin = Math.max(0, Math.round((Date.now() - gen.getTime()) / 60000));
    const when = gen.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
    const age = ageMin < 1 ? "just now" : ageMin < 60 ? `${ageMin} min ago` : `${Math.floor(ageMin / 60)} h ${ageMin % 60} min ago`;
    info.textContent = `Snapshot taken ${when} (${age}); refreshed automatically a few times per hour.`;
    if (ageMin > 180) info.innerHTML += ' <strong style="color:var(--error)">The snapshot is stale – the scheduled refresh may have stopped.</strong>';
  }

  // ------------------------------------------------------------------ boot

  async function boot() {
    try {
      const r = await fetch("./availability.json", { cache: "no-cache" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      data = await r.json();
    } catch (err) {
      info.textContent = "";
      results.innerHTML = `<div class="error">Could not load availability.json (${esc(err.message)}).</div>`;
      return;
    }
    defaults = { date_from: data.date_from, date_to: data.date_to, min_minutes: "120", indoor: "any" };
    const clubs = data.providers.filter((p) => p.club).map((p) => p.club);
    document.getElementById("clubs").innerHTML = clubs.map((c) =>
      `<label class="chk"><input type="checkbox" name="club" value="${esc(c.key)}" checked> ${esc(c.name)}</label>`).join("");
    form.elements.date_from.min = data.date_from;
    form.elements.date_to.max = data.date_to;
    applyDefaults();
    restore();
    describeSnapshot();
    render();
    form.addEventListener("change", render);
    document.getElementById("reset").addEventListener("click", () => { applyDefaults(); render(); });
    setInterval(() => { describeSnapshot(); render(); }, 60000); // keep "past" cutoff and age label current
  }

  boot();
})();
