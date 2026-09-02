/* Sourcing agent front end.
 *
 * The dashboard computes nothing. Every number it shows is a value the engine
 * already stored on the run, which is why derived figures can be labelled as
 * derived and traced back to the quote they came from.
 */
const HIDDEN_COMPLIANCE = new Set(["TSCA"]);
const API = "/api";
const view = document.getElementById("view");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const eur = (v) => v == null ? "-" :
  Number(v).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const num = (v, dp = 2) => v == null ? "-" :
  Number(v).toLocaleString("en-GB", { minimumFractionDigits: dp, maximumFractionDigits: dp });

// A quoted unit price keeps whatever precision the supplier printed, but shows
// at least two decimals so it reads as money.
const price = (v) => v == null ? "-" :
  Number(v).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 4 });

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 3200);
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(API + path, options);
  } catch (err) {
    // fetch only rejects when no response arrived at all - the connection was
    // refused, dropped or timed out. The browser's own wording for that is
    // "Failed to fetch", which tells the reader nothing about which request
    // died or what to do next.
    throw new Error(
      `Could not reach the server on ${path}. The request may have taken too `
      + `long, or the service restarted. Try again — nothing was saved.`);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `${response.status} ${response.statusText}`);
  }
  return response.json();
}

/* ---------------------------------------------------------------- routing */

const routes = [
  [/^\/$/, home],
  [/^\/reference$/, reference],
  [/^\/comparison\/([^/]+)$/, comparison],
  [/^\/run\/([^/]+)$/, run],
  [/^\/run\/([^/]+)\/package$/, packageView],
];

function route() {
  clearTimeout(comparison.timer);
  const path = (location.hash || "#/").slice(1);
  for (const [pattern, handler] of routes) {
    const match = path.match(pattern);
    if (match) {
      handler(...match.slice(1)).catch((err) => {
        view.innerHTML = `<div class="banner bad">${esc(err.message)}</div>`;
      });
      return;
    }
  }
  view.innerHTML = `<div class="banner warn">Page not found.</div>`;
}

window.addEventListener("hashchange", route);
window.addEventListener("load", route);

/* ------------------------------------------------------------------- home */

async function home() {
  const comparisons = await api("/comparisons");
  view.innerHTML = `
    <div class="spread">
      <div>
        <h1>Quote comparisons</h1>
        <p class="muted">Each comparison is one batch of supplier quotes evaluated together.</p>
      </div>
      <div class="row">
        <input type="text" id="new-name" placeholder="Comparison name" value="Wet chemicals basket">
        <button class="primary" id="new-btn">New comparison</button>
      </div>
    </div>
    <div class="card">
      ${comparisons.length === 0
        ? `<p class="muted">Nothing yet. Create a comparison and upload the supplier quotes into it.</p>`
        : `<table><thead><tr><th>Name</th><th>Created</th><th class="num">Documents</th><th class="num"></th></tr></thead>
           <tbody>${comparisons.map((c) => `
             <tr>
               <td><a href="#/comparison/${esc(c.comparison_id)}">${esc(c.name)}</a></td>
               <td class="muted small">${esc((c.created_at || "").slice(0, 16).replace("T", " "))}</td>
               <td class="num">${c.document_count}</td>
               <td class="num nowrap">
                 <a href="#/comparison/${esc(c.comparison_id)}">Open</a>
                 <button class="link-danger" data-delete-comparison="${esc(c.comparison_id)}"
                   data-name="${esc(c.name)}" data-docs="${c.document_count}">Delete</button>
               </td>
             </tr>`).join("")}</tbody></table>`}
    </div>`;

  // Deleting takes the quotes and every evaluation run with it, so the count
  // goes in the prompt - "Delete Wet chemicals basket?" reads much smaller
  // than what it actually does.
  view.querySelectorAll("[data-delete-comparison]").forEach((button) => {
    button.onclick = async () => {
      const name = button.dataset.name;
      const docs = Number(button.dataset.docs || 0);
      const detail = docs
        ? `\n\nIts ${docs} uploaded document${docs === 1 ? "" : "s"}, the quotes `
          + `extracted from ${docs === 1 ? "it" : "them"} and every evaluation run `
          + `will be removed from the database.`
        : "\n\nIt has no documents yet.";
      if (!confirm(`Delete the comparison "${name}"?${detail}\n\nThis cannot be undone.`)) {
        return;
      }
      button.disabled = true;
      try {
        const result = await api(`/comparisons/${button.dataset.deleteComparison}`,
                                 { method: "DELETE" });
        toast(`Deleted "${name}" — ${result.documents} document(s), `
              + `${result.runs} run(s) removed`);
        route();
      } catch (err) {
        button.disabled = false;
        toast(err.message);
      }
    };
  });

  document.getElementById("new-btn").onclick = async () => {
    const name = document.getElementById("new-name").value.trim() || "New comparison";
    const created = await api("/comparisons", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    location.hash = `#/comparison/${created.comparison_id}`;
  };
}

/* ------------------------------------------------------- comparison / upload */

const STATUS_PILL = {
  UPLOADED: "plain", PROCESSING: "info", READY: "ok", FAILED: "bad",
};

async function comparison(id) {
  const data = await api(`/comparisons/${id}`);

  const rows = data.documents.map((d) => `
    <tr>
      <td>${esc(d.filename)}${d.source_url ? ` <a class="small" href="${esc(d.source_url)}" target="_blank" rel="noopener">source</a>` : ""}</td>
      <td>${esc(d.supplier_name || "-")}</td>
      <td><span class="pill ${STATUS_PILL[d.status] || "plain"}">${esc(d.status.toLowerCase())}</span>
          ${d.error_detail ? `<div class="small" style="color:var(--bad)">${esc(d.error_detail)}</div>` : ""}</td>
      <td class="num">${d.page_count ?? "-"}</td>
      <td class="num">
        ${d.quote_id ? `<button class="link" data-quote="${esc(d.quote_id)}">extracted</button>` : ""}
        <button class="link" data-reprocess="${esc(d.document_id)}">reprocess</button>
        <button class="link" data-delete="${esc(d.document_id)}">remove</button>
      </td>
    </tr>`).join("");

  view.innerHTML = `
    <div class="spread">
      <div><h1>${esc(data.name)}</h1>
        <p class="muted">Upload every supplier quote for this basket. Evaluation runs on the whole batch.</p></div>
      <a href="#/" class="muted small">All comparisons</a>
    </div>

    ${data.duplicate_suppliers.length ? `<div class="banner bad">
      Two quotes from the same supplier are in this batch:
      ${esc(data.duplicate_suppliers.join(", "))}. Every comparison assumes one quote
      per supplier, so remove the older document before evaluating.</div>` : ""}

    <div class="card">
      <div class="dropzone" id="drop">
        <p><strong>Drop quote PDFs here</strong> or <button class="link" id="pick">choose files</button></p>
        <p class="small">Extraction, normalization and the automated checks run in the background.</p>
        <input type="file" id="file" multiple accept="application/pdf" hidden>
      </div>
    </div>

    ${data.documents.length ? `<div class="card scroll">
      <table><thead><tr><th>File</th><th>Supplier</th><th>Status</th><th class="num">Pages</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : ""}

    <div class="row">
      <button class="primary" id="evaluate" ${data.can_evaluate ? "" : "disabled"}>Evaluate basket</button>
      <span class="muted small">${data.can_evaluate
        ? "All documents are ready."
        : "Enabled once every document reaches ready and no supplier appears twice."}</span>
    </div>

    ${data.runs.length ? `<h2>Previous runs</h2><div class="card">
      <table><thead><tr><th>Run</th><th>Created</th><th></th></tr></thead><tbody>
      ${data.runs.map((r) => `<tr>
        <td class="small">${esc(r.run_id.slice(0, 8))}</td>
        <td class="muted small">${esc((r.created_at || "").slice(0, 16).replace("T", " "))}</td>
        <td class="num"><a href="#/run/${esc(r.run_id)}">Open dashboard</a></td></tr>`).join("")}
      </tbody></table></div>` : ""}

    <div id="quote-detail"></div>`;

  wireUpload(id);

  view.querySelectorAll("[data-reprocess]").forEach((b) => {
    b.onclick = async () => {
      await api(`/documents/${b.dataset.reprocess}/reprocess`, { method: "POST" });
      toast("Reprocessing");
      route();
    };
  });
  view.querySelectorAll("[data-delete]").forEach((b) => {
    b.onclick = async () => {
      await api(`/documents/${b.dataset.delete}`, { method: "DELETE" });
      route();
    };
  });
  view.querySelectorAll("[data-quote]").forEach((b) => {
    b.onclick = () => showExtracted(b.dataset.quote);
  });

  document.getElementById("evaluate").onclick = async (event) => {
    event.target.disabled = true;
    event.target.textContent = "Evaluating…";
    try {
      const created = await api(`/comparisons/${id}/runs`, { method: "POST" });
      location.hash = `#/run/${created.run_id}`;
    } catch (err) {
      toast(err.message);
      event.target.disabled = false;
      event.target.textContent = "Evaluate basket";
    }
  };

  // Poll while anything is still being extracted.
  if (data.documents.some((d) => ["UPLOADED", "PROCESSING"].includes(d.status))) {
    clearTimeout(comparison.timer);
    comparison.timer = setTimeout(route, 2500);
  }
}

function wireUpload(id) {
  const drop = document.getElementById("drop");
  const input = document.getElementById("file");
  document.getElementById("pick").onclick = () => input.click();
  input.onchange = () => send(input.files);

  ["dragenter", "dragover"].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("hot"); }));
  ["dragleave", "drop"].forEach((e) =>
    drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("hot"); }));
  drop.addEventListener("drop", (ev) => send(ev.dataTransfer.files));

  async function send(files) {
    if (!files || !files.length) return;
    const body = new FormData();
    [...files].forEach((f) => body.append("files", f));
    drop.innerHTML = `<p class="muted">Uploading ${files.length} file(s)…</p>`;
    try {
      const result = await api(`/comparisons/${id}/documents`, { method: "POST", body });
      if (result.duplicates_ignored.length) {
        toast(`Already in this comparison, ignored: ${result.duplicates_ignored.join(", ")}`);
      }
    } catch (err) {
      toast(err.message);
    }
    route();
  }
}

async function showExtracted(quoteId) {
  const quote = await api(`/quotes/${quoteId}`);
  const target = document.getElementById("quote-detail");

  const flagPill = (flags) => (flags || []).map((f) =>
    `<span class="pill ${f.severity === "error" ? "bad" : f.severity === "warning" ? "warn" : "info"}"
      title="${esc(f.message)}">${esc(f.field)}</span>`).join(" ");

  target.innerHTML = `
    <h2>Extracted from ${esc(quote.supplier_name)}</h2>
    <div class="card">
      <div class="row small">
        <span><strong>Quote</strong> ${esc(quote.quote_no || "-")}</span>
        <span><strong>Quote date</strong> ${esc(quote.quote_date || "-")}</span>
        <span><strong>Valid until</strong> ${esc(quote.valid_until || "-")}${expiryPill(quote.valid_until)}</span>
        <span><strong>Currency</strong> ${esc(quote.currency || "-")}</span>
        <span><strong>Incoterm</strong> ${esc(quote.incoterm || "-")} ${esc(quote.incoterm_location || "")}</span>
        <span><strong>Payment</strong> ${quote.payment_terms_net_days ? "Net " + quote.payment_terms_net_days : "-"}</span>
        <span><strong>Lead time</strong> ${quote.lead_time_min_weeks ?? "-"}-${quote.lead_time_max_weeks ?? "-"} wks</span>
        ${quote.source_url ? `<a href="${esc(quote.source_url)}" target="_blank" rel="noopener">open PDF</a>` : ""}
      </div>
    </div>
    <div class="card scroll">
      <table><thead><tr>
        <th>#</th><th>Material</th><th>Supplier's description</th>
        <th class="num">Qty</th><th>UoM</th>
        <th class="num">Unit price<div class="unit">quote currency</div></th>
        <th class="num">Line total<div class="unit">quote currency</div></th><th>MOQ</th><th>Checks</th>
      </tr></thead><tbody>
      ${quote.lines.map((l) => `<tr>
        <td>${l.line_no}</td>
        <td>${l.material_name
              ? `${esc(l.material_name)}<div class="derived">CAS ${esc(l.cas_no)}</div>`
              : l.cas_no
                ? `<span class="muted">CAS ${esc(l.cas_no)}</span><div class="derived">not in the demand basket</div>`
                : `<span class="muted">no CAS resolved</span>`}</td>
        <td class="small">${esc(l.supplier_description || "-")}</td>
        <td class="num">${num(l.quantity, 0)}</td><td>${esc(l.uom || "-")}</td>
        <td class="num">${num(l.unit_price, 4)}</td>
        <td class="num">${eur(l.line_total_stated)}</td>
        <td class="small">${esc(l.moq_text || (l.moq_qty ? `${num(l.moq_qty, 0)} ${l.moq_uom || ""}` : "-"))}</td>
        <td>${flagPill(l.flags) || `<span class="pill ok">ok</span>`}</td>
      </tr>`).join("")}
      </tbody></table>
    </div>
    <div class="card">
      <h3>Compliance statements found</h3>
      ${Object.entries(quote.compliance || {}).filter(([code]) => !HIDDEN_COMPLIANCE.has(code)).map(([code, c]) => `
        <div class="small" style="margin-bottom:6px">
          <span class="pill ${c.claimed ? "ok" : "warn"}">${c.claimed ? "stated" : "gap"}</span>
          <strong>${esc(code)}</strong>
          ${c.evidence_text ? `<span class="muted"> — “${esc(c.evidence_text)}”${c.evidence_page ? ` (p.${c.evidence_page})` : ""}</span>` : ""}
        </div>`).join("") || `<p class="muted small">Nothing extracted.</p>`}
    </div>`;
  target.scrollIntoView({ behavior: "smooth" });
}

/* --------------------------------------------------------------- dashboard */

/* Validity on the extraction screen, where there is no run yet to measure
 * against. This one is against today on purpose: nothing has been evaluated,
 * so the only useful question is whether the quote is still good now. */
function expiryPill(validUntil) {
  if (!validUntil) return "";
  const days = Math.ceil(
    (new Date(validUntil + "T00:00:00Z") - new Date()) / 86400000);
  if (days < 0) return ` <span class="pill bad">expired</span>`;
  if (days <= 14) return ` <span class="pill warn">${days} days left</span>`;
  return "";
}

/* ------------------------------------------------------------- dashboard */
/*
 * Every figure below is a value the engine already stored on the run. The only
 * thing fetched separately is the prose, and it arrives after the page has
 * rendered - each slot carries data-note and is filled in place, so a slow or
 * unavailable model leaves blank captions rather than an empty dashboard.
 */

const NOTE_CACHE = {};

function noteSlot(key, cls) {
  return `<p class="note-slot ${cls || ""}" data-note="${esc(key)}"></p>`;
}

async function fillNotes(runId) {
  let payload = NOTE_CACHE[runId];
  if (!payload) {
    try {
      payload = await api(`/runs/${runId}/notes`);
      NOTE_CACHE[runId] = payload;
    } catch {
      return;                       // captions stay blank; nothing else changes
    }
  }
  const n = (payload && payload.notes) || {};
  const at = (path) => path.split(".").reduce((o, k) => (o || {})[k], n) || "";

  document.querySelectorAll("[data-note]").forEach((el) => {
    const key = el.dataset.note;
    let text = "";
    if (key.startsWith("supplier:")) {
      const id = key.slice(9);
      text = ((n.suppliers || []).find((s) => s.supplier_id === id) || {}).note || "";
    } else if (key.startsWith("nego:")) {
      const cas = key.slice(5);
      text = ((n.negotiation || []).find((x) => x.cas_no === cas) || {}).note || "";
    } else {
      text = at(key);
    }
    if (text) { el.textContent = text; el.classList.add("filled"); }
  });

  const align = document.getElementById("alignment-list");
  if (align && (n.alignment || []).length) {
    align.innerHTML = n.alignment.map((a) => `
      <div class="align-item">
        <span class="align-tick">&check;</span>
        <div><strong>${esc(a.title)}</strong>
        <p class="small muted">${esc(a.detail || "")}</p></div>
      </div>`).join("");
  }
}

/* --- small derivations, all from stored values ---------------------------- */

const isAwarded = (s) => s.award_status === "PRIMARY" || s.award_status === "SECONDARY";

function gapToCheapest(r) {
  const primary = r.suppliers.find((s) => s.final_rank === 1);
  const cheapest = r.suppliers.reduce((a, b) =>
    Number(a.total_landed_cost_eur) <= Number(b.total_landed_cost_eur) ? a : b, r.suppliers[0]);
  if (!primary || !cheapest || primary.supplier_id === cheapest.supplier_id) return null;
  const low = Number(cheapest.total_landed_cost_eur);
  return { pct: ((Number(primary.total_landed_cost_eur) - low) / low * 100).toFixed(1),
           primary, cheapest };
}

const aboveCount = (r, s) => r.lines.filter(
  (l) => l.supplier_id === s.supplier_id && l.above_ceiling).length;
const quotedCount = (r, s) => r.lines.filter((l) => l.supplier_id === s.supplier_id).length;

const STATUS_LABEL = {
  PRIMARY: "Primary award", SECONDARY: "Secondary / dual-source",
  NOT_RECOMMENDED: "Not recommended", EXCLUDED: "Not recommended",
};

/* --- sections ------------------------------------------------------------- */

function kpiRow(r) {
  const k = r.kpis;
  const gap = gapToCheapest(r);
  const primary = r.suppliers.find((s) => s.final_rank === 1);
  return `
    <div class="kpis">
      <div class="kpi feature">
        <div class="label">Recommended supplier</div>
        <div class="value">${esc(k.recommended_supplier || "none")}</div>
        ${noteSlot("headline.recommendation", "lead")}
        ${noteSlot("headline.why")}
      </div>
      <div class="kpi">
        <div class="label">Gap to the cheapest bid</div>
        <div class="value">${gap ? gap.pct + "%" : "—"}</div>
        <div class="sub">${gap
          ? esc(gap.primary.supplier_name) + " vs " + esc(gap.cheapest.supplier_name)
          : "the recommended supplier is also the cheapest"}</div>
        ${noteSlot("kpi_notes.gap")}
      </div>
      <div class="kpi">
        <div class="label">Where it sits vs. ceiling price</div>
        <div class="value">${primary ? esc(primary.ceiling_equivalent_variance_pct) + "%" : "—"}</div>
        <div class="sub">against a ceiling-equivalent basket of
          <span class="unit">EUR</span> ${eur(k.ceiling_equivalent_total_eur)}</div>
        ${noteSlot("kpi_notes.target")}
      </div>
      <div class="kpi">
        <div class="label">Savings on the table</div>
        <div class="value"><span class="unit">EUR</span> ${eur(k.spread_eur)}</div>
        <div class="sub">cheapest quote against the most expensive</div>
        ${noteSlot("kpi_notes.savings")}
      </div>
    </div>`;
}

function rankingCards(r) {
  // Ranked suppliers keep the engine's number. The rest follow, ordered by
  // cost - a display position only, which is why it is worked out here and
  // never written back onto the run.
  const ranked = r.suppliers.filter((s) => s.final_rank)
    .sort((a, b) => a.final_rank - b.final_rank);
  const unranked = r.suppliers.filter((s) => !s.final_rank)
    .sort((a, b) => Number(a.total_landed_cost_eur) - Number(b.total_landed_cost_eur));
  const ordered = [...ranked, ...unranked];
  const position = new Map(ordered.map((s, i) => [s.supplier_id, i + 1]));
  return `
    <h2>Supplier ranking</h2>
    <div class="rank-grid">
      ${ordered.map((s) => `
        <div class="rank-card ${s.award_status === "PRIMARY" ? "is-primary" : ""}">
          <div class="rank-head">
            <span class="rank-no">Rank ${position.get(s.supplier_id)}</span>
            <span class="pill ${s.award_status === "PRIMARY" ? "ok"
              : s.award_status === "SECONDARY" ? "info" : "warn"}">${
              esc(STATUS_LABEL[s.award_status] || s.award_status || "")}</span>
          </div>
          <h3>${esc(s.supplier_name)}</h3>
          <div class="rank-meta">${esc(s.incoterm || "—")} ·
            ${s.payment_terms_net_days ? "Net " + s.payment_terms_net_days : "terms not stated"} ·
            ${s.lead_time_min_weeks ?? "—"}–${s.lead_time_max_weeks ?? "—"} wks</div>
          <div class="rank-total"><span class="unit">EUR</span> ${eur(s.total_landed_cost_eur)}</div>
          <div class="rank-sub">total landed cost · ${aboveCount(r, s)} of ${quotedCount(r, s)} items above ceiling</div>
          ${noteSlot("supplier:" + s.supplier_id)}
        </div>`).join("")}
    </div>`;
}

function methodology(r) {
  const gateOf = (n) => Object.values(r.gates).map((g) => g[n - 1]);
  const g1 = gateOf(1), g3 = gateOf(3);
  const names = Object.fromEntries(r.suppliers.map((s) => [s.supplier_id, s.supplier_name]));
  const by = Object.fromEntries(r.suppliers.map((s) => [s.supplier_id, s]));

  // Each pill names the supplier and the figure that decided it, so the step
  // can be checked without reading the tables further down.
  const failed1 = Object.entries(r.gates)
    .filter(([, g]) => !g[0].passed)
    .map(([id, g]) => {
      const gaps = ((g[0].detail || {}).gaps || [])
        .filter((c) => !HIDDEN_COMPLIANCE.has(c));
      return `${names[id]} fails${gaps.length ? " (" + gaps.join(", ") + ")" : ""}`;
    });
  const failed3 = Object.entries(r.gates)
    .filter(([, g]) => !g[2].passed)
    .map(([id]) => `${names[id]} fails (${by[id].ceiling_equivalent_variance_pct}%)`);
  const byCost = [...r.suppliers].filter((s) => s.base_rank)
    .sort((a, b) => a.base_rank - b.base_rank)
    .map((s) => `${s.supplier_name} (EUR ${eur(s.total_landed_cost_eur)})`);
  const promo = (r.promotions || []).find((p) => p.promoted);

  const step = (n, title, noteKey, pill, tone) => `
    <div class="step">
      <div class="step-no">Step ${n}</div>
      <h3>${title}</h3>
      ${noteSlot(noteKey)}
      <span class="pill ${tone}">${esc(pill)}</span>
    </div>`;

  return `
    <h2>Ranking methodology</h2>
    <div class="steps-grid">
      ${step(1, "Compliance check", "method.compliance",
        failed1.length ? failed1.join("; ") : `all ${g1.length} pass`,
        failed1.length ? "warn" : "ok")}
      ${step(2, "Ceiling-price check", "method.target_price",
        failed3.length ? failed3.join("; ") : `all ${g3.length} within threshold`,
        failed3.length ? "warn" : "ok")}
      ${step(3, "Rank by cost", "method.rank",
        byCost.length ? byCost.join(", then ") : "no supplier ranked", "info")}
      ${step(4, "Can a pricier supplier move up?", "method.promotion",
        promo ? `${names[promo.candidate_supplier_id] || ""} moves to Rank 1`
                + ` (${promo.cost_gap_pct}% gap)`
              : "no promotion", promo ? "ok" : "plain")}
    </div>`;
}

function promotionPanel(r) {
  if (!(r.promotions || []).length) return "";
  return `
    <h2>Promotion rule</h2>
    <p class="muted small">A supplier may only move above a cheaper one when all four conditions hold.</p>
    ${r.promotions.map((p) => `
      <div class="card">
        <div class="spread">
          <strong>${esc(p.candidate_supplier_id)} against ${esc(p.cheaper_supplier_id)}</strong>
          <span class="pill ${p.promoted ? "ok" : "plain"}">${p.promoted ? "promoted" : "not promoted"}</span>
        </div>
        ${[["Cost gap", p.cost_condition_met, p.detail.cost],
           ["Compliance", p.compliance_condition_met, p.detail.compliance],
           ["Payment terms", p.payment_condition_met, p.detail.payment_terms],
           ["Lead time", p.lead_time_condition_met, p.detail.lead_time]]
          .map(([label, met, note]) => `
          <div class="cond">
            <div>${esc(label)}</div>
            <div><span class="pill ${met ? "ok" : "bad"}">${met ? "met" : "not met"}</span></div>
            <div class="muted">${esc(note || "")}</div>
          </div>`).join("")}
      </div>`).join("")}`;
}


function materialTable(r) {
  const suppliers = r.suppliers.map((s) => s.supplier_id);
  const names = Object.fromEntries(r.suppliers.map((s) => [s.supplier_id, s.supplier_name]));
  const materials = [...new Map(r.lines.map((l) => [l.cas_no, l])).values()];
  return `
    <h2>Price comparison by material</h2>
    <div class="card">
      <div class="scroll"><table><thead><tr>
        <th>Material</th><th class="num">Ceiling (EUR/L)</th>
        ${suppliers.map((id) => `<th class="num">${esc(names[id])}</th>`).join("")}
      </tr></thead><tbody>
      ${materials.map((m) => `<tr>
        <td>${esc(m.material_name)}<div class="derived">CAS ${esc(m.cas_no)}</div></td>
        <td class="num">${num(m.ceiling_price_eur_l, 2)}</td>
        ${suppliers.map((id) => {
          const line = r.lines.find((l) => l.cas_no === m.cas_no && l.supplier_id === id);
          if (!line) return `<td class="num muted">not quoted</td>`;
          return `<td class="num ${line.above_ceiling ? "above" : ""} ${line.is_cheapest_for_material ? "best" : ""}">
            ${num(line.landed_price_per_l_eur, 2)}
            ${line.above_ceiling ? `<div class="derived above">above ceiling</div>` : ""}</td>`;
        }).join("")}
      </tr>`).join("")}
      <tr class="total-row"><td>Items above ceiling</td><td class="num">—</td>
        ${r.suppliers.map((s) => `<td class="num">${aboveCount(r, s)} of ${quotedCount(r, s)}</td>`).join("")}
      </tr>
      </tbody></table></div>
      <p class="small muted" style="margin-top:10px">Landed price is the quoted price converted to
      EUR per litre and adjusted by the Incoterm's fixed freight percentage.</p>
    </div>`;
}

function costBreakdown(r) {
  return `
    <h2>Cost breakdown</h2>
    <div class="card"><div class="scroll">
      <table><thead><tr><th>Supplier</th><th class="num">Goods subtotal (EUR)</th>
        <th class="num">Discount</th><th>Freight</th>
        <th class="num">Total landed (EUR)</th></tr></thead><tbody>
      ${r.suppliers.map((s) => `<tr>
        <td>${esc(s.supplier_name)}</td>
        <td class="num">${eur(s.goods_subtotal_eur)}</td>
        <td class="num">${s.discount_amount_eur !== "0.00"
          ? "−" + esc(s.discount_pct_applied) + "%" : "none offered"}</td>
        <td>${s.freight_policy_matched === false
          ? `<span class="above">no policy for ${esc(s.incoterm || "missing Incoterm")}</span>`
          : (Number(s.freight_adj_pct) === 0 ? "included (delivered price)"
             : "+" + esc(s.freight_adj_pct) + "% (estimate)")}</td>
        <td class="num"><strong>${eur(s.total_landed_cost_eur)}</strong></td>
      </tr>`).join("")}
      </tbody></table>
    </div></div>`;
}

/* One table, four views. Each row is [label, valueFor(supplier)]. */
function comparisonTabs(r) {
  const gate1 = (s) => (r.gates[s.supplier_id] || [{}])[0];
  const moq = (s) => [...new Set((s.moq_terms || [])
    .map((m) => m.text || `${m.qty || ""} ${m.uom || ""}`.trim()).filter(Boolean))];
  // Follows whatever checklist is in force rather than a hard-coded list, so a
  // strategy upload that adds or renames a requirement shows up here too.
  const visible = (codes) => (codes || []).filter((c) => !HIDDEN_COMPLIANCE.has(c));
  const checklistRows = (r.compliance_matrix || [])
    .filter((row) => !HIDDEN_COMPLIANCE.has(row.code))
    .map((row) => [row.label || row.code,
      (s) => (row.suppliers[s.supplier_id] || {}).claimed ? "Yes" : "Not stated"]);

  const TABS = {
    commercial: ["Commercial", [
      ["Total landed cost", (s) => "EUR " + eur(s.total_landed_cost_eur)],
      ["Payment terms", (s) => s.payment_terms_net_days ? "Net " + s.payment_terms_net_days : "—"],
      ["Discount offered", (s) => (s.discount_structure || []).length
        ? s.discount_structure.map((d) => `${esc(d.discount_pct)}% ${esc((d.condition_type || "").replace("_", " ").toLowerCase())}`).join("<br>")
        : "None"],
      ["Quote valid until", (s) => (s.valid_until || "—") + (s.is_expired ? " (expired)" : "")],
      ["Currency", (s) => esc(s.currency || "—")],
    ]],
    compliance: ["Compliance", [
      ["Mandatory items", (s) => gate1(s).passed ? "All stated"
        : "Gaps: " + visible((gate1(s).detail || {}).gaps).join(", ")],
      ...checklistRows,
      ["Open advisory items", (s) => visible(s.advisory_gaps).length
        ? String(visible(s.advisory_gaps).length) : "None"],
    ]],
    logistics: ["Logistics", [
      ["Incoterm", (s) => esc(s.incoterm || "—")],
      ["Lead time", (s) => `${s.lead_time_min_weeks ?? "—"}–${s.lead_time_max_weeks ?? "—"} weeks`],
      ["MOQ", (s) => moq(s).length ? moq(s).join("<br>") : "not stated"],
      ["Freight basis", (s) => esc(s.freight_basis || "—")],
    ]],
    risk: ["Risk", [
      ["FX exposure", (s) => s.currency === "EUR" ? "None — quoted in EUR" : esc(s.currency || "—")],
      ["Landed-cost certainty", (s) => s.freight_policy_matched === false ? "Unknown — no freight policy"
        : Number(s.freight_adj_pct) === 0 ? "High — delivered price" : "Medium — freight estimated"],
      // Compared against the other awarded suppliers rather than a fixed
      // number of weeks. An invented cut-off would be a policy decision made
      // in the front end, and the label would be wrong anyway - a supplier
      // over the line is not necessarily the longest.
      ["Sole-source risk", (s) => {
        if (!isAwarded(s)) return "Not applicable — not recommended";
        const mids = r.suppliers.filter(isAwarded)
          .map((x) => Number(x.lead_time_midpoint_weeks))
          .filter((n) => !Number.isNaN(n));
        if (mids.length < 2) return "Single awarded supplier";
        const mine = Number(s.lead_time_midpoint_weeks);
        return mine === Math.max(...mids)
          ? "Higher — longest lead time of the awarded suppliers" : "Low";
      }],
      ["Already supplying", (s) => s.is_incumbent
        ? `Yes — ${esc(s.historical_share_pct)}% of past spend` : "No prior spend"],
    ]],
  };

  const body = (rows) => `
    <table><thead><tr><th></th>
      ${r.suppliers.map((s) => `<th>${esc(s.supplier_name)}</th>`).join("")}
    </tr></thead><tbody>
    ${rows.map(([label, fn]) => `<tr><td class="row-label">${label}</td>
      ${r.suppliers.map((s) => `<td>${fn(s)}</td>`).join("")}</tr>`).join("")}
    </tbody></table>`;

  return `
    <h2>Comparison details</h2>
    <div class="card">
      <div class="tabs" id="cmp-tabs">
        ${Object.entries(TABS).map(([key, [label]], i) =>
          `<button class="tab ${i === 0 ? "on" : ""}" data-tab="${key}">${label}</button>`).join("")}
      </div>
      ${Object.entries(TABS).map(([key, [, rows]], i) =>
        `<div class="tab-body scroll" data-panel="${key}" ${i ? "hidden" : ""}>${body(rows)}</div>`).join("")}
    </div>`;
}

function alignmentAndNegotiation(r) {
  const awarded = new Set(r.suppliers.filter(isAwarded).map((s) => s.supplier_id));
  const rows = (r.renegotiation || []).filter((c) => awarded.has(c.supplier_id));
  return `
    <div class="two-col">
      <div>
        <h2>Category strategy alignment</h2>
        <div class="card" id="alignment-list">
          <p class="muted small">No alignment notes available for this run.</p>
        </div>
      </div>
      <div>
        <h2>Negotiation opportunities</h2>
        <div class="card">
          ${rows.length ? `<div class="scroll"><table><thead><tr>
            <th>Item</th><th>Supplier</th><th class="num">Gap</th></tr></thead><tbody>
            ${rows.map((c) => `<tr>
              <td>${esc(c.material_name)}
                ${noteSlot("nego:" + c.cas_no, "tight")}</td>
              <td>${esc(c.supplier_name)}</td>
              <td class="num above">${esc(c.gap_pct)}%
                <div class="derived">EUR ${eur(c.annual_impact_eur)}/yr</div></td>
            </tr>`).join("")}
            </tbody></table></div>`
            : `<p class="muted small">No line item from an awarded supplier is priced
               above its ceiling. Nothing to reprice before award.</p>`}
        </div>
      </div>
    </div>`;
}

function footerNotes(r) {
  return `
    <div class="foot-grid">
      <div><span class="foot-label">What this covers</span>${noteSlot("footer.covers")}</div>
      <div><span class="foot-label">How prices were made comparable</span>${noteSlot("footer.comparable")}</div>
      <div><span class="foot-label">What to double check</span>${noteSlot("footer.double_check")}</div>
    </div>`;
}

/* --- the view -------------------------------------------------------------- */

async function run(runId) {
  const data = await api(`/runs/${runId}`);
  const r = data.result;
  const k = r.kpis;

  view.innerHTML = `
    <div class="dash-bar">
      <div>
        <h1>Quote comparison</h1>
        <p class="muted small">${k.supplier_count} suppliers · ${k.material_count} materials ·
        engine ${esc(r.engine_version)} · ${esc((r.generated_at || "").slice(0, 16).replace("T", " "))}</p>
      </div>
      <div class="row">
        <a href="#/comparison/${esc(data.comparison_id)}" class="btn">Back to batch</a>
        <button class="primary" id="package-btn">Approval package</button>
      </div>
    </div>

    ${(k.unapproved_suppliers || []).length ? `<div class="banner bad">
      Not on the approved supplier list: ${esc(k.unapproved_suppliers.join(", "))}.</div>` : ""}
    ${(r.warnings || []).length ? `<div class="banner warn">
      ${r.warnings.map(esc).join("<br>")}</div>` : ""}

    ${kpiRow(r)}
    ${rankingCards(r)}
    ${methodology(r)}
    ${materialTable(r)}
    ${costBreakdown(r)}
    ${comparisonTabs(r)}
    ${promotionPanel(r)}
    ${alignmentAndNegotiation(r)}
    ${r.data_quality.length ? `<h2>Data quality</h2><div class="card"><div class="scroll">
      <table><thead><tr><th>Supplier</th><th>Material</th><th>Issue</th>
        <th class="num">Stated (EUR)</th><th class="num">Recomputed (EUR)</th></tr></thead><tbody>
      ${r.data_quality.map((d) => `<tr><td>${esc(d.supplier_name)}</td>
        <td>${esc(d.material_name)}</td><td class="small">${esc(d.note || d.flag)}</td>
        <td class="num">${eur(d.line_total_stated)}</td>
        <td class="num">${eur(d.line_total_recomputed)}</td></tr>`).join("")}
      </tbody></table></div></div>` : ""}

    <h2>Ask about this recommendation</h2>
    <div class="card chat">
      <p class="chat-intro">The agent answers from this run only. It reads the stored
      figures and the gate trail — it cannot recalculate or consider other quotes.</p>
      <div class="chips" id="suggested"></div>
      <div class="ask-row">
        <input type="text" id="question" autocomplete="off"
          placeholder="Ask anything about this comparison…">
        <button class="primary" id="ask-btn">Ask</button>
      </div>
      <div id="answer"></div>
    </div>

    ${footerNotes(r)}`;

  wireAgent(runId, r);
  document.getElementById("package-btn").onclick = () => {
    location.hash = `#/run/${runId}/package`;
  };
  const tabs = document.getElementById("cmp-tabs");
  if (tabs) {
    tabs.onclick = (e) => {
      const btn = e.target.closest("[data-tab]");
      if (!btn) return;
      tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t === btn));
      document.querySelectorAll("[data-panel]").forEach((p) => {
        p.hidden = p.dataset.panel !== btn.dataset.tab;
      });
    };
  }

  fillNotes(runId);          // prose arrives after the numbers; never blocks
}

function wireAgent(runId, r) {
  const primary = r.suppliers.find((s) => s.final_rank === 1);
  const cheaper = r.suppliers.find((s) => s.base_rank === 1);
  const excluded = r.suppliers.find((s) => !s.eligible);

  const suggestions = [];
  if (primary && cheaper && primary.supplier_id !== cheaper.supplier_id) {
    suggestions.push(`Why is ${primary.supplier_name} ranked above ${cheaper.supplier_name} when ${cheaper.supplier_name} is cheaper?`);
  }
  if (excluded) suggestions.push(`Why was ${excluded.supplier_name} excluded?`);
  suggestions.push("Which line items should I prioritise in negotiation?");
  suggestions.push("What compliance gaps exist across the suppliers?");

  document.getElementById("suggested").innerHTML =
    suggestions.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("");

  const $ = (id) => document.getElementById(id);
  let busy = false;

  async function ask(question) {
    if (busy || !question.trim()) return;
    busy = true;
    $("ask-btn").disabled = true;
    $("question").value = question;

    // The question is echoed above the answer so a long reply still says what
    // it is replying to, and the wait has something to sit under.
    $("answer").innerHTML = `
      <div class="qa">
        <p class="qa-q">${esc(question)}</p>
        <p class="qa-thinking">Reading the stored run<span class="dots"></span></p>
      </div>`;

    try {
      const response = await api(`/runs/${runId}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      $("answer").innerHTML = `
        <div class="qa">
          <p class="qa-q">${esc(question)}</p>
          <div class="answer">${markdown(response.answer)}</div>
        </div>`;
    } catch (err) {
      $("answer").innerHTML = `
        <div class="qa">
          <p class="qa-q">${esc(question)}</p>
          <div class="banner bad">${esc(err.message)}</div>
        </div>`;
    } finally {
      busy = false;
      $("ask-btn").disabled = false;
    }
  }

  $("ask-btn").onclick = () => ask($("question").value);
  $("question").onkeydown = (e) => { if (e.key === "Enter") ask(e.target.value); };
  document.querySelectorAll("[data-q]").forEach((b) => {
    b.onclick = () => ask(b.dataset.q);
  });
}

/* --------------------------------------------------------- approval package */

async function packageView(runId) {
  view.innerHTML = `<h1>Approval package</h1><p class="muted">Drafting from the stored run…</p>`;

  // A package is stored once drafted, so the first visit creates it and later
  // visits show what was drafted then. Regenerate replaces it from the run.
  async function load(regenerate) {
    if (!regenerate) {
      try {
        return (await api(`/runs/${runId}/package`)).summary_md;
      } catch { /* nothing drafted yet - fall through and draft it */ }
    }
    return (await api(`/runs/${runId}/package`, { method: "POST" })).summary_md;
  }

  function render(summary) {
    view.innerHTML = `
      <div class="spread no-print">
        <h1>Approval package</h1>
        <div class="row">
          <a href="#/run/${esc(runId)}" class="muted small">Back to dashboard</a>
          <button id="regen">Regenerate</button>
          <button class="primary" id="print">Print or save as PDF</button>
        </div>
      </div>
      <div class="memo" id="memo">${markdown(summary)}</div>`;

    document.getElementById("print").onclick = () => window.print();
    document.getElementById("regen").onclick = async () => {
      const button = document.getElementById("regen");
      button.disabled = true;
      button.textContent = "Regenerating…";
      try {
        render(await load(true));
        toast("Approval package redrafted from the stored run");
      } catch (err) {
        button.disabled = false;
        button.textContent = "Regenerate";
        toast(err.message);
      }
    };
  }

  render(await load(false));
}

/* ------------------------------------------------------------- reference */

function appliedSummary(summary) {
  if (!summary) return "";
  const rows = [
    ["Prices set", (summary.materials_updated || []).map((m) =>
      `${m.cas_no} ${m.target ? "target " + m.target : ""} ceiling ${m.ceiling || "-"}`)],
    ["Checklist set", (summary.requirements_updated || []).map((r) =>
      `${r.code} ${r.tier.toLowerCase()}`)],
    ["Thresholds set", (summary.thresholds_updated || []).map((t) =>
      `${t.key} = ${t.value}`)],
    ["Approved suppliers", summary.approved_suppliers || []],
    ["Not applied", [...(summary.materials_unknown || []), ...(summary.ignored || [])]],
  ].filter(([, items]) => items.length);

  if (!rows.length) return "";
  return `<table style="margin-top:12px"><tbody>
    ${rows.map(([label, items]) => `<tr>
      <td style="width:170px"><strong>${esc(label)}</strong></td>
      <td class="small">${items.map(esc).join("<br>")}</td></tr>`).join("")}
  </tbody></table>
  <p class="small muted" style="margin-top:8px">Anything the document did not state
  keeps its previous value. "Not applied" is reported rather than guessed at.</p>`;
}

async function reference() {
  const [data, strategy] = await Promise.all([
    api("/reference"), api("/reference/strategy"),
  ]);
  const active = strategy.active;

  view.innerHTML = `
    <h1>Policy in force</h1>
    <p class="muted">Every rule threshold and constant the engine used. Changing a value here changes future runs, never past ones.</p>

    <h2>Category strategy</h2>
    <div class="card">
      <div class="row">
        <button id="strategy-pick" class="primary">Upload category strategy</button>
        <input type="file" id="strategy-file" accept=".pdf,.docx,.txt,.md" hidden>
        <span class="muted small">One-time setup. Sets target and ceiling prices,
        the compliance checklist, the concentration threshold and the approved
        supplier list. PDF or Word.</span>
      </div>
      <div id="strategy-result"></div>
      ${active ? `<div class="banner ok" style="margin-top:14px">
          In force: <strong>${esc(active.source_filename)}</strong>
          ${active.version ? " · version " + esc(active.version) : ""}
          ${active.effective_date ? " · effective " + esc(active.effective_date) : ""}
          · uploaded ${esc((active.uploaded_at || "").slice(0, 10))}
        </div>
        ${appliedSummary(active.applied_summary)}`
        : `<p class="muted small" style="margin-top:12px">No document uploaded.
           The seeded defaults below are in force, and every quoting supplier is
           treated as approved until an approved list exists.</p>`}
    </div>

    <h2>Rule thresholds and constants</h2>
    <div class="card scroll"><table><thead><tr><th>Key</th><th class="num">Value</th><th>Unit</th><th>Meaning</th><th>Section</th></tr></thead><tbody>
      ${data.policy.map((p) => `<tr><td><code>${esc(p.key)}</code></td>
        <td class="num">${p.value}</td><td>${esc(p.unit || "")}</td>
        <td class="small">${esc(p.description)}</td>
        <td class="muted small">${esc(p.section_ref || "")}</td></tr>`).join("")}
    </tbody></table></div>

    <h2>Materials, prices and required volumes</h2>
    <div class="card scroll"><table><thead><tr><th>CAS</th><th>Material</th>
      <th class="num">Density kg/L</th><th class="num">Target EUR/L</th>
      <th class="num">Ceiling EUR/L</th><th class="num">Required L</th></tr></thead><tbody>
      ${data.materials.map((m) => {
        const b = data.benchmarks.find((x) => x.cas_no === m.cas_no) || {};
        const d = data.demand.find((x) => x.cas_no === m.cas_no) || {};
        return `<tr><td>${esc(m.cas_no)}</td><td>${esc(m.name)}</td>
          <td class="num">${num(m.density_kg_per_l, 3)}</td>
          <td class="num">${b.target_price_eur_l == null
            ? "<span class='muted'>not set</span>" : num(b.target_price_eur_l, 2)}</td>
          <td class="num">${num(b.ceiling_price_eur_l, 2)}</td>
          <td class="num">${num(d.required_qty_l, 0)}</td></tr>`;
      }).join("")}
    </tbody></table>
    <p class="small muted" style="margin-top:10px">Target prices come from the
    category strategy document; the ceiling drives outlier flagging and Gate 3.</p></div>

    <h2>Approved suppliers</h2>
    <div class="card">
      ${data.approved_suppliers.length
        ? `<table><thead><tr><th>Supplier</th><th>Match key</th></tr></thead><tbody>
           ${data.approved_suppliers.map((a) => `<tr><td>${esc(a.legal_name)}</td>
             <td class="muted small"><code>${esc(a.supplier_key)}</code></td></tr>`).join("")}
           </tbody></table>
           <p class="small muted" style="margin-top:10px">A quoting supplier outside
           this list is flagged on the dashboard, not excluded.</p>`
        : `<p class="muted small">No list set. Upload a category strategy document to
           populate it; until then every quoting supplier is treated as approved.</p>`}
    </div>

    <h2>Freight adjustment by Incoterm</h2>
    <div class="card scroll"><table><thead><tr><th>Incoterm</th><th class="num">Uplift (%)</th><th>Basis</th><th>Type</th></tr></thead><tbody>
      ${data.freight_policy.map((f) => `<tr><td>${esc(f.incoterm)}</td>
        <td class="num">${f.freight_adj_pct}%</td><td class="small">${esc(f.basis_note)}</td>
        <td><span class="pill ${f.is_estimate ? "warn" : "ok"}">${f.is_estimate ? "estimate" : "quoted"}</span></td></tr>`).join("")}
    </tbody></table></div>

    <h2>Historical purchase prices</h2>
    <div class="card">
      <div class="row">
        <button id="hist-pick">Upload SAP BW extract</button>
        <input type="file" id="hist-file" accept=".xlsx,.xlsm,.csv" hidden>
        ${data.historical.length ? `<button class="link" id="hist-clear">clear</button>` : ""}
        <span class="muted small">Excel with both sheets, or a single-sheet CSV.
        Price Summary feeds the price variance beside the ceiling; PO Price History
        feeds the concentration check. Matched on CAS number.</span>
      </div>
      <div id="hist-result"></div>
      ${data.historical.length ? `<div class="scroll" style="margin-top:14px">
        <table><thead><tr><th>CAS</th><th>Material no.</th><th class="num">Avg (EUR/L)</th>
          <th class="num">Min (EUR/L)</th><th class="num">Max (EUR/L)</th>
          <th class="num">Last invoiced (EUR/L)</th>
          <th class="num">PO lines</th><th>Period</th></tr></thead><tbody>
        ${data.historical.map((h) => `<tr><td>${esc(h.cas_no)}</td>
          <td class="small">${esc(h.material_number || "-")}</td>
          <td class="num">${num(h.avg_price_eur_l, 2)}</td>
          <td class="num">${num(h.min_price_eur_l, 2)}</td>
          <td class="num">${num(h.max_price_eur_l, 2)}</td>
          <td class="num">${num(h.last_invoiced_price_eur_l, 2)}</td>
          <td class="num">${h.po_line_count ?? "-"}</td>
          <td class="muted small">${h.period_from
            ? esc(h.period_from) + " – " + esc(h.period_to || "") : "-"}</td></tr>`).join("")}
        </tbody></table>
        ${data.vendor_spend.length ? `<h3>Spend by vendor</h3>
          <table><thead><tr><th>Vendor</th><th class="num">Spend EUR</th>
            <th class="num">Share (%)</th></tr></thead><tbody>
          ${data.vendor_spend.map((v) => `<tr><td>${esc(v.vendor_name)}</td>
            <td class="num">${eur(v.spend_eur)}</td>
            <td class="num">${esc(v.share_pct || "-")}%</td></tr>`).join("")}
          </tbody></table>
          <p class="small muted" style="margin-top:10px">From the PO Price History sheet.
          This is the measured input to the concentration check.</p>` : ""}
        </div>`
        : `<p class="muted small" style="margin-top:12px">No extract loaded yet.</p>`}
    </div>

    <h2>Compliance checklist</h2>
    <div class="card"><table><thead><tr><th>Code</th><th>Requirement</th><th>Tier</th></tr></thead><tbody>
            ${data.compliance_requirements.filter((c) => !HIDDEN_COMPLIANCE.has(c.code)).map((c) => `<tr><td><code>${esc(c.code)}</code></td>
        <td>${esc(c.label)}</td>
        <td><span class="pill ${c.tier === "MANDATORY" ? "bad" : "info"}">${esc(c.tier.toLowerCase())}</span></td></tr>`).join("")}
    </tbody></table>
    <p class="small muted" style="margin-top:10px">Mandatory items drive Gate 1 exclusion. Advisory items drive the
    compliance condition of the promotion rule.</p></div>`;

  const strategyFile = document.getElementById("strategy-file");
  document.getElementById("strategy-pick").onclick = () => strategyFile.click();
  strategyFile.onchange = async () => {
    if (!strategyFile.files.length) return;
    const body = new FormData();
    body.append("file", strategyFile.files[0]);
    const target = document.getElementById("strategy-result");
    target.innerHTML = `<p class="muted small" style="margin-top:12px">Reading the document…</p>`;
    try {
      const summary = await api("/reference/strategy", { method: "POST", body });
      target.innerHTML = `<div class="banner ok" style="margin-top:12px">
        Applied.</div>${appliedSummary(summary)}`;
      setTimeout(route, 1600);
    } catch (err) {
      target.innerHTML = `<div class="banner bad" style="margin-top:12px">${esc(err.message)}</div>`;
    }
  };

  const histFile = document.getElementById("hist-file");
  document.getElementById("hist-pick").onclick = () => histFile.click();
  histFile.onchange = async () => {
    if (!histFile.files.length) return;
    const body = new FormData();
    body.append("file", histFile.files[0]);
    const target = document.getElementById("hist-result");
    target.innerHTML = `<p class="muted small">Loading…</p>`;
    try {
      const summary = await api("/reference/historical", { method: "POST", body });
      const derived = (summary.derived_from_po_lines || []).length
        ? `<br>Benchmark derived from PO lines for ${summary.derived_from_po_lines.join(", ")}.`
        : "";
      target.innerHTML = `<div class="banner ${summary.errors.length ? "warn" : "ok"}"
        style="margin-top:12px">${summary.summary_rows} price summary rows,
        ${summary.po_lines} PO lines, ${summary.skipped} skipped.${derived}
        ${summary.errors.length ? "<br>" + summary.errors.map(esc).join("<br>") : ""}</div>`;
      setTimeout(route, 1200);
    } catch (err) {
      target.innerHTML = `<div class="banner bad" style="margin-top:12px">${esc(err.message)}</div>`;
    }
  };

  const clear = document.getElementById("hist-clear");
  if (clear) clear.onclick = async () => {
    await api("/reference/historical", { method: "DELETE" });
    route();
  };
}

/* ------------------------------------------------------- markdown rendering */

function markdown(text) {
  const lines = String(text || "").split("\n");
  const out = [];
  let inTable = false;

  const inline = (s) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w])_([^_]+)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/`(.+?)`/g, "<code>$1</code>");

  for (const raw of lines) {
    const line = raw.trimEnd();
    const isRow = /^\|.*\|$/.test(line);

    if (isRow) {
      const cells = line.slice(1, -1).split("|").map((c) => c.trim());
      if (/^[-: ]+$/.test(cells.join(""))) continue;
      if (!inTable) { out.push("<table><tbody>"); inTable = true; }
      out.push("<tr>" + cells.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>");
      continue;
    }
    if (inTable) { out.push("</tbody></table>"); inTable = false; }

    if (/^### /.test(line)) out.push(`<h3>${inline(line.slice(4))}</h3>`);
    else if (/^## /.test(line)) out.push(`<h2>${inline(line.slice(3))}</h2>`);
    else if (/^# /.test(line)) out.push(`<h1>${inline(line.slice(2))}</h1>`);
    else if (/^- /.test(line)) out.push(`<div style="margin-left:14px">• ${inline(line.slice(2))}</div>`);
    else if (line === "") out.push("<div style='height:8px'></div>");
    else out.push(`<p>${inline(line)}</p>`);
  }
  if (inTable) out.push("</tbody></table>");
  return out.join("");
}
