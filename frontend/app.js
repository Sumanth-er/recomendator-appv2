/* Sourcing agent front end.
 *
 * The dashboard computes nothing. Every number it shows is a value the engine
 * already stored on the run, which is why derived figures can be labelled as
 * derived and traced back to the quote they came from.
 */

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
  const response = await fetch(API + path, options);
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
        : `<table><thead><tr><th>Name</th><th>Created</th><th class="num">Documents</th><th></th></tr></thead>
           <tbody>${comparisons.map((c) => `
             <tr>
               <td><a href="#/comparison/${esc(c.comparison_id)}">${esc(c.name)}</a></td>
               <td class="muted small">${esc((c.created_at || "").slice(0, 16).replace("T", " "))}</td>
               <td class="num">${c.document_count}</td>
               <td class="num"><a href="#/comparison/${esc(c.comparison_id)}">Open</a></td>
             </tr>`).join("")}</tbody></table>`}
    </div>`;

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
        <span><strong>Currency</strong> ${esc(quote.currency || "-")}</span>
        <span><strong>Incoterm</strong> ${esc(quote.incoterm || "-")} ${esc(quote.incoterm_location || "")}</span>
        <span><strong>Payment</strong> ${quote.payment_terms_net_days ? "Net " + quote.payment_terms_net_days : "-"}</span>
        <span><strong>Lead time</strong> ${quote.lead_time_min_weeks ?? "-"}-${quote.lead_time_max_weeks ?? "-"} wks</span>
        ${quote.source_url ? `<a href="${esc(quote.source_url)}" target="_blank" rel="noopener">open PDF</a>` : ""}
      </div>
    </div>
    <div class="card scroll">
      <table><thead><tr>
        <th>#</th><th>CAS</th><th>Description</th><th class="num">Qty</th><th>UoM</th>
        <th class="num">Unit price</th><th class="num">Line total</th><th>MOQ</th><th>Checks</th>
      </tr></thead><tbody>
      ${quote.lines.map((l) => `<tr>
        <td>${l.line_no}</td><td>${esc(l.cas_no || "-")}</td>
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
      ${Object.entries(quote.compliance || {}).map(([code, c]) => `
        <div class="small" style="margin-bottom:6px">
          <span class="pill ${c.claimed ? "ok" : "warn"}">${c.claimed ? "stated" : "gap"}</span>
          <strong>${esc(code)}</strong>
          ${c.evidence_text ? `<span class="muted"> — “${esc(c.evidence_text)}”${c.evidence_page ? ` (p.${c.evidence_page})` : ""}</span>` : ""}
        </div>`).join("") || `<p class="muted small">Nothing extracted.</p>`}
    </div>`;
  target.scrollIntoView({ behavior: "smooth" });
}

/* --------------------------------------------------------------- dashboard */

async function run(runId) {
  const data = await api(`/runs/${runId}`);
  const r = data.result;
  const k = r.kpis;

  view.innerHTML = `
    <div class="spread">
      <div><h1>Quote comparison dashboard</h1>
        <p class="muted small">Engine ${esc(r.engine_version)} · generated ${esc((r.generated_at || "").slice(0, 16).replace("T", " "))} ·
        run ${esc(runId.slice(0, 8))}</p></div>
      <div class="row">
        <a href="#/comparison/${esc(data.comparison_id)}" class="muted small">Back to batch</a>
        <button class="primary" id="package-btn">Approval package</button>
      </div>
    </div>

    ${(k.unapproved_suppliers || []).length ? `<div class="banner bad">
      Not on the approved supplier list for this category:
      ${esc(k.unapproved_suppliers.join(", "))}.</div>` : ""}

    ${(r.warnings || []).length ? `<div class="banner warn">
      ${r.warnings.map(esc).join("<br>")}</div>` : ""}

    <div class="kpis">
      <div class="kpi"><div class="label">Recommended</div>
        <div class="value">${esc(k.recommended_supplier || "none")}</div>
        <div class="note">EUR ${eur(k.recommended_total_eur)} landed</div></div>
      <div class="kpi"><div class="label">Spread across suppliers</div>
        <div class="value">${eur(k.spread_eur)}</div>
        <div class="note">${esc(k.spread_pct)}% of basket value</div></div>
      <div class="kpi"><div class="label">Ceiling-equivalent basket</div>
        <div class="value">${eur(k.ceiling_equivalent_total_eur)}</div>
        <div class="note">every item at its ceiling price</div></div>
      <div class="kpi"><div class="label">Renegotiation candidates</div>
        <div class="value">${k.renegotiation_count}</div>
        <div class="note">line items above ceiling</div></div>
      <div class="kpi"><div class="label">Data quality flags</div>
        <div class="value">${k.data_quality_issues}</div>
        <div class="note">flagged, not corrected</div></div>
    </div>

    <h2>Supplier summary</h2>
    <div class="card scroll">
      <table><thead><tr>
        <th>Rank</th><th>Supplier</th><th class="num">Goods</th><th class="num">Freight</th>
        <th class="num">Discount</th><th class="num">Total landed</th>
        <th>Incoterm</th><th>Payment</th><th>Lead time</th><th>Status</th>
      </tr></thead><tbody>
      ${r.suppliers.map((s) => `<tr>
        <td>${s.final_rank ?? "-"}${s.base_rank && s.base_rank !== s.final_rank
          ? `<span class="derived" title="base rank by cost"> (was ${s.base_rank})</span>` : ""}</td>
        <td><strong>${esc(s.supplier_name)}</strong></td>
        <td class="num">${eur(s.goods_subtotal_eur)}</td>
        <td class="num">${eur(s.freight_amount_eur)}<div class="derived ${s.freight_policy_matched === false ? "above" : ""}"
          title="${esc(s.freight_basis || "")}">${s.freight_policy_matched === false
            ? "no policy for " + esc(s.incoterm || "missing Incoterm")
            : esc(s.freight_adj_pct) + "% fixed"}</div></td>
        <td class="num">${s.discount_amount_eur !== "0.00" ? "-" + eur(s.discount_amount_eur) : "-"}
          ${s.discount_condition_met ? `<div class="derived">${esc(s.discount_pct_applied)}%</div>` : ""}</td>
        <td class="num"><strong>${eur(s.total_landed_cost_eur)}</strong></td>
        <td>${esc(s.incoterm || "-")}</td>
        <td>${s.payment_terms_net_days ? "Net " + s.payment_terms_net_days : "-"}</td>
        <td>${s.lead_time_min_weeks ?? "-"}-${s.lead_time_max_weeks ?? "-"} wks</td>
        <td><span class="pill ${s.award_status === "PRIMARY" ? "ok"
          : s.award_status === "SECONDARY" ? "info" : "warn"}">${esc((s.award_status || "").toLowerCase().replace("_", " "))}</span></td>
      </tr>`).join("")}
      </tbody></table>
      <p class="small muted" style="margin-top:10px">Freight is a fixed adjustment selected by Incoterm, not a quoted figure.
      Totals are extended from landed prices using required volumes.</p>
    </div>

    ${lineMatrix(r)}
    ${commercialTermsPanel(r)}
    ${compliancePanel(r)}
    ${concentrationPanel(r)}
    ${gateTrail(r)}
    ${promotionPanel(r)}
    ${renegotiationPanel(r)}
    ${dataQualityPanel(r)}

    <h2>Ask about this recommendation</h2>
    <div class="card">
      <div class="row">
        <input type="text" id="question" style="flex:1;min-width:320px"
          placeholder="Why is the recommended supplier ranked above the cheaper one?">
        <button id="ask-btn">Ask</button>
      </div>
      <div class="row small" style="margin-top:10px" id="suggested"></div>
      <div id="answer" style="margin-top:14px"></div>
    </div>`;

  wireAgent(runId, r);
  document.getElementById("package-btn").onclick = () => {
    location.hash = `#/run/${runId}/package`;
  };
}

function lineMatrix(r) {
  const suppliers = r.suppliers.map((s) => s.supplier_id);
  const names = Object.fromEntries(r.suppliers.map((s) => [s.supplier_id, s.supplier_name]));
  const materials = [...new Map(r.lines.map((l) => [l.cas_no, l])).values()];

  const hasHistorical = r.lines.some((l) => l.historical_avg_eur_l != null);

  const cell = (line) => {
    if (!line) return `<td class="num muted">-</td>`;
    const classes = [line.is_cheapest_for_material ? "cheapest" : "",
                     line.above_ceiling ? "above" : ""].join(" ");
    const variance = line.historical_variance_pct;
    const drift = variance == null ? "" :
      `<div class="derived ${Number(variance) > 0 ? "above" : ""}">${Number(variance) > 0 ? "+" : ""}${esc(variance)}% vs hist.</div>`;
    return `<td class="num ${classes}" title="${esc(line.quoted_unit_price)} ${esc(line.quoted_currency)}/${esc(line.quoted_uom)} → ${esc(line.price_per_l_eur)} EUR/L → +${esc(line.freight_adj_pct)}% freight">
      ${num(line.landed_price_per_l_eur, 2)}
      <div class="derived">${price(line.quoted_unit_price)} ${esc(line.quoted_currency)}/${esc(line.quoted_uom)}</div>
      ${drift}
    </td>`;
  };

  return `
    <h2>Landed price per litre, by material</h2>
    <div class="card scroll">
      <table><thead><tr>
        <th>Material</th><th class="num">Required (L)</th><th class="num">Ceiling</th>
        ${hasHistorical ? `<th class="num">Historical avg</th>` : ""}
        ${suppliers.map((s) => `<th class="num">${esc(names[s])}</th>`).join("")}
      </tr></thead><tbody>
      ${materials.map((m) => `<tr>
        <td>${esc(m.material_name)}<div class="derived">CAS ${esc(m.cas_no)}</div></td>
        <td class="num">${num(m.required_qty_l, 0)}</td>
        <td class="num">${num(m.ceiling_price_eur_l, 2)}</td>
        ${hasHistorical ? `<td class="num" title="${m.historical_po_line_count
            ? m.historical_po_line_count + " PO lines" : ""}">
          ${num(m.historical_avg_eur_l, 2)}
          ${m.historical_last_invoiced_eur_l
            ? `<div class="derived">last ${num(m.historical_last_invoiced_eur_l, 2)}</div>` : ""}
          ${m.historical_min_eur_l
            ? `<div class="derived">${num(m.historical_min_eur_l, 2)}–${num(m.historical_max_eur_l, 2)}</div>` : ""}</td>` : ""}
        ${suppliers.map((s) => cell(r.lines.find((l) =>
          l.supplier_id === s && l.cas_no === m.cas_no))).join("")}
      </tr>`).join("")}
      </tbody></table>
      <p class="small muted" style="margin-top:10px">
        <span class="pill ok">shaded</span> cheapest for that material ·
        <span class="above">red</span> above the category strategy ceiling ·
        small text is the original quoted price and the variance against the historical
        average. Hover a cell for the full derivation.
        ${hasHistorical ? "" : "<br>No historical extract loaded — upload one under Policy in force to see price variance."}
      </p>
    </div>`;
}

function gateTrail(r) {
  return `
    <h2>Ranking trail</h2>
    <p class="muted small">Gates run in order. A supplier failing an earlier gate is excluded before cost is considered.</p>
    ${r.suppliers.map((s) => `
      <div class="card">
        <div class="spread">
          <strong>${esc(s.supplier_name)}</strong>
          <span class="pill ${s.eligible ? "ok" : "bad"}">${s.eligible
            ? "cleared all gates" : "failed gate " + s.failed_gate}</span>
        </div>
        ${(r.gates[s.supplier_id] || []).map((g) => `
          <div class="gate ${g.passed ? "pass" : "fail"}">
            <div class="head">Gate ${g.gate_no} · ${esc(g.gate_name)} ·
              <span class="pill ${g.passed ? "ok" : "bad"}">${g.passed ? "pass" : "fail"}</span></div>
            <div class="small muted">${esc(g.detail.explanation || "")}</div>
            ${g.measured_value != null ? `<div class="derived">measured ${esc(g.measured_value)} against threshold ${esc(g.threshold_value)}</div>` : ""}
          </div>`).join("")}
        <div class="small">
          Base rank by cost: <strong>${s.base_rank ?? "not ranked"}</strong> ·
          Final rank: <strong>${s.final_rank ?? "not ranked"}</strong>
        </div>
        <div class="small muted" style="margin-top:6px">${esc(s.primary_reason || "")}</div>
      </div>`).join("")}`;
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

function renegotiationPanel(r) {
  if (!(r.renegotiation || []).length) return "";
  return `
    <h2>Renegotiation candidates</h2>
    <div class="card scroll">
      <table><thead><tr><th>Supplier</th><th>Material</th>
        <th class="num">Landed EUR/L</th><th class="num">Ceiling</th>
        <th class="num">Gap</th><th class="num">Annual impact (EUR)</th></tr></thead><tbody>
      ${r.renegotiation.map((c) => `<tr>
        <td>${esc(c.supplier_name)}</td><td>${esc(c.material_name)}</td>
        <td class="num above">${num(c.landed_price_eur_l, 2)}</td>
        <td class="num">${num(c.ceiling_price_eur_l, 2)}</td>
        <td class="num">${esc(c.gap_pct)}%</td>
        <td class="num">${eur(c.annual_impact_eur)}</td>
      </tr>`).join("")}
      </tbody></table>
    </div>`;
}

function concentrationPanel(r) {
  const hc = r.historical_context || {};
  const vendors = hc.incumbent_vendors || [];
  const allocation = r.allocation || [];
  if (!allocation.length && !vendors.length) return "";
  const names = Object.fromEntries(r.suppliers.map((s) => [s.supplier_id, s.supplier_name]));

  return `
    <h2>Dual sourcing and concentration</h2>
    <div class="card scroll">
      <table><thead><tr><th>Supplier</th><th class="num">Share today</th>
        <th class="num">Proposed share</th><th class="num">Proposed spend</th>
        <th>Against the ${esc(hc.concentration_threshold_pct || "60")}% threshold</th></tr></thead>
      <tbody>${allocation.map((a) => {
        const today = vendors.find((v) => v.supplier_id === a.supplier_id);
        const breachToday = today && today.exceeds_threshold_today;
        return `<tr>
          <td>${esc(names[a.supplier_id] || a.supplier_id)}
            ${today ? "" : `<div class="derived">no prior spend</div>`}</td>
          <td class="num ${breachToday ? "above" : ""}">${a.historical_share_pct == null
            ? "<span class='muted'>-</span>" : esc(a.historical_share_pct) + "%"}</td>
          <td class="num">${esc(a.allocation_pct)}%</td>
          <td class="num">${eur(a.allocated_spend_eur)}</td>
          <td>${a.exceeds_concentration_threshold
            ? `<span class="pill bad">award breaches</span>`
            : breachToday
              ? `<span class="pill warn">reduces a standing breach</span>`
              : `<span class="pill ok">within policy</span>`}</td>
        </tr>`;
      }).join("")}</tbody></table>
      ${vendors.filter((v) => !v.is_quoting).length ? `<p class="small muted" style="margin-top:10px">
        Incumbent vendors not quoting:
        ${vendors.filter((v) => !v.is_quoting).map((v) =>
          `${esc(v.supplier_id)} (${esc(v.share_pct)}%)`).join(", ")}.</p>` : ""}
      <p class="small muted" style="margin-top:10px">Share today is measured from the
      PO history. The category strategy does not state a split between primary and
      secondary source, so the proposed share is a configured assumption — shown
      beside what each supplier holds now so the award reads as a shift.</p>
    </div>`;
}

function commercialTermsPanel(r) {
  const rows = r.suppliers.map((s) => {
    const moq = (s.moq_terms || []).map((m) => esc(m.text || `${m.qty || ""} ${m.uom || ""}`.trim()));
    const unique = [...new Set(moq)];
    const discounts = (s.discount_structure || []).length
      ? s.discount_structure.map((d) => {
          const condition = (d.condition_text || "").replace(/^\s*\d+(\.\d+)?\s*%\s*/, "");
          return `${esc(d.discount_pct)}% <span class="muted">${esc(condition)}</span>`;
        }).join("<br>")
      : `<span class="muted">none quoted</span>`;
    return `<tr>
      <td><strong>${esc(s.supplier_name)}</strong></td>
      <td>${esc(s.incoterm || "-")}</td>
      <td>${s.payment_terms_net_days ? "Net " + s.payment_terms_net_days : "-"}</td>
      <td>${s.lead_time_min_weeks ?? "-"}-${s.lead_time_max_weeks ?? "-"} wks
        <div class="derived">midpoint ${esc(s.lead_time_midpoint_weeks ?? "-")}</div></td>
      <td class="small">${unique.length ? unique.join("<br>") : "<span class='muted'>not stated</span>"}</td>
      <td class="small">${discounts}</td>
    </tr>`;
  }).join("");

  return `
    <h2>Commercial terms</h2>
    <div class="card scroll">
      <table><thead><tr><th>Supplier</th><th>Incoterm</th><th>Payment terms</th>
        <th>Lead time</th><th>MOQ terms</th><th>Discount structure</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <p class="small muted" style="margin-top:10px">Payment terms and lead time also feed
      the promotion rule. MOQ terms feed Gate 2.</p>
    </div>`;
}

function compliancePanel(r) {
  const matrix = r.compliance_matrix || [];
  if (!matrix.length) return "";
  const suppliers = r.suppliers.map((s) => s.supplier_id);
  const names = Object.fromEntries(r.suppliers.map((s) => [s.supplier_id, s.supplier_name]));

  const mark = (claim) => claim && claim.claimed
    ? `<span class="pill ok" title="${esc(claim.evidence_text || "")}${claim.evidence_page ? ` (p.${claim.evidence_page})` : ""}">met</span>`
    : `<span class="pill warn">gap</span>`;

  return `
    <h2>Quality and compliance checklist</h2>
    <div class="card scroll">
      <table><thead><tr><th>Requirement</th><th>Tier</th>
        ${suppliers.map((s) => `<th>${esc(names[s])}</th>`).join("")}</tr></thead>
      <tbody>${matrix.map((row) => `<tr>
        <td>${esc(row.label)}<div class="derived">${esc(row.code)}</div></td>
        <td><span class="pill ${row.tier === "MANDATORY" ? "bad" : "info"}">${esc(row.tier.toLowerCase())}</span></td>
        ${suppliers.map((s) => `<td>${mark(row.suppliers[s])}</td>`).join("")}
      </tr>`).join("")}</tbody></table>
      <p class="small muted" style="margin-top:10px">Mandatory gaps exclude a supplier at Gate 1.
      Advisory gaps feed the compliance condition of the promotion rule. A requirement the quote
      does not mention is a gap, never an assumed pass — hover a met badge for the quoted evidence.</p>
    </div>`;
}

function dataQualityPanel(r) {
  const issues = r.data_quality || [];
  if (!issues.length) return "";
  return `
    <h2>Data quality</h2>
    <div class="card scroll">
      <table><thead><tr><th>Supplier</th><th>Material</th><th>Issue</th>
        <th class="num">Stated</th><th class="num">Recomputed</th><th class="num">Difference</th></tr></thead>
      <tbody>${issues.map((d) => `<tr>
        <td>${esc(d.supplier_name)}</td><td>${esc(d.material_name)}</td>
        <td><span class="pill warn">${esc((d.flag || "").toLowerCase().replace(/_/g, " "))}</span>
          <div class="small muted">${esc(d.note || "")}</div></td>
        <td class="num">${eur(d.line_total_stated)}</td>
        <td class="num">${eur(d.line_total_recomputed)}</td>
        <td class="num">${eur(d.line_total_delta)}</td>
      </tr>`).join("")}</tbody></table>
      <p class="small muted" style="margin-top:10px">Flagged, never corrected. The totals above
      still use the quoted unit price.</p>
    </div>`;
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
    suggestions.map((q) => `<button class="link" data-q="${esc(q)}">${esc(q)}</button>`).join("");

  const askBox = document.getElementById("question");
  const answerBox = document.getElementById("answer");

  async function ask(question) {
    if (!question.trim()) return;
    answerBox.innerHTML = `<p class="muted small">Thinking…</p>`;
    try {
      const response = await api(`/runs/${runId}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      answerBox.innerHTML = `<div class="answer">${markdown(response.answer)}</div>`;
    } catch (err) {
      answerBox.innerHTML = `<div class="banner bad">${esc(err.message)}</div>`;
    }
  }

  document.getElementById("ask-btn").onclick = () => ask(askBox.value);
  askBox.onkeydown = (e) => { if (e.key === "Enter") ask(askBox.value); };
  document.querySelectorAll("[data-q]").forEach((b) => {
    b.onclick = () => { askBox.value = b.dataset.q; ask(b.dataset.q); };
  });
}

/* --------------------------------------------------------- approval package */

async function packageView(runId) {
  view.innerHTML = `<h1>Approval package</h1><p class="muted">Drafting from the stored run…</p>`;
  let summary;
  try {
    summary = (await api(`/runs/${runId}/package`)).summary_md;
  } catch {
    summary = (await api(`/runs/${runId}/package`, { method: "POST" })).summary_md;
  }
  view.innerHTML = `
    <div class="spread">
      <h1>Approval package</h1>
      <div class="row">
        <a href="#/run/${esc(runId)}" class="muted small">Back to dashboard</a>
        <button id="print">Print or save as PDF</button>
      </div>
    </div>
    <div class="memo" id="memo">${markdown(summary)}</div>`;
  document.getElementById("print").onclick = () => window.print();
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
    <div class="card scroll"><table><thead><tr><th>Incoterm</th><th class="num">Uplift</th><th>Basis</th><th>Type</th></tr></thead><tbody>
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
        <table><thead><tr><th>CAS</th><th>Material no.</th><th class="num">Avg</th>
          <th class="num">Min</th><th class="num">Max</th><th class="num">Last invoiced</th>
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
            <th class="num">Share</th></tr></thead><tbody>
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
      ${data.compliance_requirements.map((c) => `<tr><td><code>${esc(c.code)}</code></td>
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
