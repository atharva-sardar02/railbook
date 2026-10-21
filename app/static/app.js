// RailBook frontend: plain JavaScript, no framework, no external scripts.
// Every value from the API is shown with textContent (never innerHTML), so user-entered
// text like passenger names can't inject HTML or script (XSS).

const $ = (id) => document.getElementById(id);

// Messages for the API's status codes; 429 comes from Kong's rate limit, not the API.
const ERRORS = {
  402: "Payment was declined. No seats were taken.",
  404: "Not found.",
  409: null, // the API's own message ("only N seats left", "already cancelled") is clearer
  422: "Please check the form values.",
  429: "Too many booking attempts — Kong's rate limit. Wait a minute and try again.",
};

// fetch() wrapper: returns { ok, status, data, requestId }.
// requestId is the X-Request-ID set by Kong — the key to find this request in Splunk.
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  return { ok: res.ok, status: res.status, data, requestId: res.headers.get("X-Request-ID") };
}

function errorText(r) {
  const detail = r.data && typeof r.data.detail === "string" ? r.data.detail : null;
  return ERRORS[r.status] || detail || `Something went wrong (HTTP ${r.status}).`;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

function requestIdTag(id) {
  return id ? el("p", { class: "reqid" }, "Request ID ", el("code", {}, id)) : "";
}

let toastTimer;
function toast(message, kind = "info", requestId = null) {
  const t = $("toast");
  t.replaceChildren(el("span", {}, message), requestIdTag(requestId));
  t.className = `toast ${kind}`;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 7000);
}

const money = (v) => `₹${Number(v).toLocaleString("en-IN", { minimumFractionDigits: 0 })}`;
const hhmm = (t) => t.slice(0, 5);
const CLASS_NAMES = { SL: "Sleeper", "3A": "AC 3 Tier", "2A": "AC 2 Tier" };

// ---------- Stations + search ----------

async function loadStations() {
  const r = await api("/stations");
  if (!r.ok) return toast(errorText(r), "error", r.requestId);
  for (const select of [$("from"), $("to")]) {
    select.replaceChildren(...r.data.map((s) => el("option", { value: s.code }, `${s.name} (${s.code})`)));
  }
  $("from").value = "NDLS";
  $("to").value = "BCT";
}

let lastSearch = null;

async function search(event) {
  event?.preventDefault();
  const from = $("from").value, to = $("to").value, date = $("date").value;
  if (from === to) return toast("Pick two different stations.", "error");
  lastSearch = { from, to, date };

  const results = $("results");
  results.replaceChildren(el("p", { class: "muted pad" }, "Searching…"));
  const params = new URLSearchParams({ from, to, date });
  const r = await api(`/trains?${params}`);
  if (!r.ok) {
    results.replaceChildren();
    return toast(errorText(r), "error", r.requestId);
  }
  if (r.data.length === 0) {
    results.replaceChildren(el("div", { class: "card empty" },
      el("p", {}, "No direct trains on this route for that date."),
      el("p", { class: "muted" }, "Try New Delhi → Mumbai Central, or another date in the next two weeks.")));
    return;
  }
  results.replaceChildren(
    el("p", { class: "muted pad" }, `${r.data.length} train${r.data.length > 1 ? "s" : ""} found`),
    ...r.data.map(trainCard),
  );
}

function trainCard(train) {
  const classes = train.classes.map((c) => {
    const soldOut = c.available === 0;
    return el("button", {
      class: `class-btn${soldOut ? " sold-out" : ""}`,
      ...(soldOut ? { disabled: "" } : {}),
      onclick: () => openBooking(train, c),
    },
      el("span", { class: "class-code" }, c.class_code),
      el("span", { class: "class-name" }, CLASS_NAMES[c.class_code] || ""),
      el("span", { class: "fare" }, money(c.fare)),
      el("span", { class: `avail${c.available < 10 ? " low" : ""}` },
        soldOut ? "Sold out" : `${c.available} left`));
  });

  return el("article", { class: "card train" },
    el("div", { class: "train-head" },
      el("div", {},
        el("h3", {}, train.name),
        el("p", { class: "muted" }, `#${train.number}`)),
      el("div", { class: "timeline" },
        el("strong", {}, hhmm(train.departs_at)),
        el("span", { class: "muted" }, train.from_station),
        el("span", { class: "line", "aria-hidden": "true" }),
        el("span", { class: "muted" }, train.to_station),
        el("strong", {}, hhmm(train.arrives_at)))),
    el("div", { class: "classes" }, ...classes));
}

// ---------- Booking ----------

let pending = null;

function openBooking(train, cls) {
  pending = { train, cls };
  $("book-summary").textContent =
    `${train.name} #${train.number} · ${train.travel_date} · ${CLASS_NAMES[cls.class_code] || cls.class_code}`;
  $("seats").max = Math.min(6, cls.available);
  $("seats").value = 1;
  $("fail-payment").checked = false;
  updateTotal();
  $("book-dialog").showModal();
  $("passenger").focus();
}

function updateTotal() {
  if (!pending) return;
  const seats = Math.max(1, Number($("seats").value) || 1);
  $("book-total").textContent = `Total ${money(pending.cls.fare * seats)}`;
}

async function submitBooking(event) {
  event.preventDefault();
  const { train, cls } = pending;
  const button = $("book-submit");
  button.disabled = true;
  const r = await api("/bookings", {
    method: "POST",
    body: JSON.stringify({
      train_number: train.number,
      travel_date: train.travel_date,
      class_code: cls.class_code,
      passenger_name: $("passenger").value.trim(),
      seats: Number($("seats").value),
      simulate_payment_failure: $("fail-payment").checked,
    }),
  });
  button.disabled = false;

  if (!r.ok) return toast(errorText(r), "error", r.requestId);
  $("book-dialog").close();
  toast(`Booked! PNR ${r.data.pnr}`, "success", r.requestId);
  $("pnr").value = r.data.pnr;
  showBooking(r.data, r.requestId);
  if (lastSearch) search();
}

// ---------- PNR lookup + cancel ----------

function showBooking(b, requestId) {
  const cancelled = b.status === "CANCELLED";
  const actions = cancelled ? "" : el("button", { class: "danger", onclick: () => cancelBooking(b.pnr) }, "Cancel booking");
  $("pnr-result").replaceChildren(el("div", { class: "ticket" },
    el("div", { class: "ticket-head" },
      el("span", { class: "pnr" }, `PNR ${b.pnr}`),
      el("span", { class: `badge ${cancelled ? "badge-off" : "badge-ok"}` }, b.status)),
    el("dl", {},
      el("dt", {}, "Passenger"), el("dd", {}, b.passenger_name),
      el("dt", {}, "Train"), el("dd", {}, `#${b.train_number}`),
      el("dt", {}, "Date"), el("dd", {}, b.travel_date),
      el("dt", {}, "Class"), el("dd", {}, CLASS_NAMES[b.class_code] || b.class_code),
      el("dt", {}, "Seats"), el("dd", {}, b.seats),
      el("dt", {}, "Amount"), el("dd", {}, money(b.amount))),
    actions,
    requestIdTag(requestId)));
}

async function lookup(event) {
  event.preventDefault();
  const r = await api(`/bookings/${encodeURIComponent($("pnr").value.trim())}`);
  if (!r.ok) {
    $("pnr-result").replaceChildren();
    return toast(r.status === 404 ? "No booking with that PNR." : errorText(r), "error", r.requestId);
  }
  showBooking(r.data, r.requestId);
}

async function cancelBooking(pnr) {
  if (!confirm(`Cancel booking ${pnr}? The seats go back on sale.`)) return;
  const r = await api(`/bookings/${encodeURIComponent(pnr)}`, { method: "DELETE" });
  if (!r.ok) return toast(errorText(r), "error", r.requestId);
  toast(`Booking ${pnr} cancelled.`, "success", r.requestId);
  showBooking(r.data, r.requestId);
  if (lastSearch) search();
}

// ---------- Wire up ----------

document.addEventListener("DOMContentLoaded", async () => {
  const today = new Date();
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  $("date").value = iso(today);
  $("date").min = iso(today);

  $("search-form").addEventListener("submit", search);
  $("swap").addEventListener("click", () => {
    [$("from").value, $("to").value] = [$("to").value, $("from").value];
  });
  $("book-form").addEventListener("submit", submitBooking);
  $("book-cancel").addEventListener("click", () => $("book-dialog").close());
  $("seats").addEventListener("input", updateTotal);
  $("pnr-form").addEventListener("submit", lookup);

  await loadStations();
  search();
});
