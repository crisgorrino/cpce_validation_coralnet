let selectedFiles = [];
let datasetToken = "";
let lastReport = null;
let acceptedSuggestions = new Set();
let savedImageOverrides = {};
let savedLabelOverrides = {};
let activeFilter = "needs_action";

const fileInput = document.getElementById("fileInput");
const folderInput = document.getElementById("folderInput");
const fileSummary = document.getElementById("fileSummary");
const validateButton = document.getElementById("validateButton");
const revalidateButton = document.getElementById("revalidateButton");
const packageButton = document.getElementById("packageButton");
const approveAllButton = document.getElementById("approveAllButton");
const activity = document.getElementById("activity");

function updateSelection(files) {
  selectedFiles = Array.from(files);
  const counts = selectedFiles.reduce((acc, file) => {
    const ext = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "other";
    acc[ext] = (acc[ext] || 0) + 1;
    return acc;
  }, {});
  fileSummary.textContent = `${selectedFiles.length} file(s) selected: ` +
    Object.entries(counts).map(([ext, count]) => `${count} .${ext}`).join(", ");
  datasetToken = "";
  acceptedSuggestions = new Set();
  savedImageOverrides = {};
  savedLabelOverrides = {};
}

fileInput.addEventListener("change", event => updateSelection(event.target.files));
folderInput.addEventListener("change", event => updateSelection(event.target.files));

async function loadCapabilities() {
  const status = document.getElementById("jpegtranStatus");
  const checkbox = document.getElementById("optimizeJpegs");
  try {
    const response = await fetch("/api/capabilities");
    const data = await response.json();
    if (data.jpegtran_available) {
      status.textContent = `jpegtran is available${data.jpegtran_path ? ` at ${data.jpegtran_path}` : ""}.`;
      status.classList.add("capability-ok");
    } else {
      status.textContent = "jpegtran was not found. Validation will still work, but lossless optimization will preserve the original files.";
      status.classList.add("capability-warning");
      checkbox.disabled = true;
    }
  } catch (_error) {
    status.textContent = "Could not check jpegtran availability.";
  }
}
loadCapabilities();

function getLabelMode() {
  return document.querySelector('input[name="labelMode"]:checked').value;
}

function collectMappings() {
  document.querySelectorAll("[data-image-map]").forEach(select => {
    if (select.value) savedImageOverrides[select.dataset.imageMap] = select.value;
  });
  document.querySelectorAll("[data-label-map]").forEach(input => {
    const value = input.value.trim();
    if (value) savedLabelOverrides[input.dataset.labelMap] = value;
    else delete savedLabelOverrides[input.dataset.labelMap];
  });
  return { imageOverrides: savedImageOverrides, labelOverrides: savedLabelOverrides };
}

function collectPathRules() {
  const rules = [];
  const prefixText = document.getElementById("pathPrefixRules")?.value || "";
  prefixText.split(/\r?\n/).forEach((rawLine, index) => {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) return;
    const separator = line.indexOf("=>");
    if (separator < 0) throw new Error(`Bulk path rule line ${index + 1} must use OLD PATH => NEW PATH.`);
    const oldPrefix = line.slice(0, separator).trim();
    const newPrefix = line.slice(separator + 2).trim();
    if (!oldPrefix) throw new Error(`Bulk path rule line ${index + 1} is missing the old path prefix.`);
    rules.push({ type: "prefix_replace", from: oldPrefix, to: newPrefix, case_sensitive: false });
  });

  const keepLastValue = document.getElementById("keepLastCount")?.value.trim() || "";
  if (keepLastValue) {
    const count = Number.parseInt(keepLastValue, 10);
    if (!Number.isInteger(count) || count <= 0) throw new Error("Keep-last count must be a positive whole number.");
    rules.push({
      type: "keep_last",
      count,
      prepend: document.getElementById("keepLastPrepend")?.value.trim() || "",
    });
  }

  if (document.getElementById("useCpcFolderRule")?.checked) {
    rules.push({
      type: "cpc_folder",
      cpc_root: document.getElementById("cpcRoot")?.value.trim() || "cpc",
      image_root: document.getElementById("imageRoot")?.value.trim() || "images",
    });
  }
  return rules;
}

async function runValidation(reuseDataset = false) {
  if (!reuseDataset && selectedFiles.length === 0) {
    alert("Choose a dataset first.");
    return;
  }

  let pathRules;
  try {
    pathRules = collectPathRules();
  } catch (error) {
    alert(error.message);
    return;
  }

  const { imageOverrides, labelOverrides } = collectMappings();
  activity.textContent = document.getElementById("optimizeJpegs").checked
    ? "Validating and checking lossless JPEG optimization…"
    : "Validating…";
  validateButton.disabled = true;
  revalidateButton.disabled = true;
  approveAllButton.disabled = true;

  const form = new FormData();
  if (!reuseDataset) {
    selectedFiles.forEach(file => form.append("files", file, file.webkitRelativePath || file.name));
  } else {
    form.append("files", new Blob([""]), "placeholder.ignore");
  }
  form.append("dataset_token", reuseDataset ? datasetToken : "");
  form.append("label_codes", document.getElementById("labelCodes").value);
  form.append("label_mode", getLabelMode());
  form.append("image_overrides", JSON.stringify(imageOverrides));
  form.append("label_overrides", JSON.stringify(labelOverrides));
  form.append("path_rules", JSON.stringify(pathRules));
  form.append("accepted_suggestions", JSON.stringify(Array.from(acceptedSuggestions)));
  form.append("optimize_jpegs", String(document.getElementById("optimizeJpegs").checked));
  form.append("preannotation_enabled", String(document.getElementById("preannotationEnabled").checked));
  form.append("preannotation_max_dimension", document.getElementById("preannotationMax").value || "4096");
  form.append("preannotation_quality", document.getElementById("preannotationQuality").value || "95");

  try {
    const response = await fetch("/api/validate", { method: "POST", body: form });
    const raw = await response.text();
    let payload;
    try {
      payload = raw ? JSON.parse(raw) : {};
    } catch (_parseError) {
      throw new Error(`Server returned ${response.status}: ${raw || "empty response"}`);
    }
    if (!response.ok) throw new Error(payload.detail || `Validation failed (${response.status})`);
    datasetToken = payload.dataset_token;
    lastReport = payload;
    renderReport(payload);
    activity.textContent = "Validation complete.";
  } catch (error) {
    activity.textContent = "";
    alert(error.message);
  } finally {
    validateButton.disabled = false;
    revalidateButton.disabled = false;
    approveAllButton.disabled = false;
  }
}

validateButton.addEventListener("click", () => runValidation(false));
revalidateButton.addEventListener("click", () => runValidation(true));
packageButton.addEventListener("click", () => {
  if (datasetToken) window.location.href = `/api/package/${datasetToken}`;
});
approveAllButton.addEventListener("click", () => {
  if (!lastReport) return;
  lastReport.suggested_fixes.filter(fix => !fix.accepted).forEach(fix => acceptedSuggestions.add(fix.id));
  runValidation(true);
});

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderReport(report) {
  document.getElementById("summarySection").classList.remove("hidden");
  const items = [
    ["CPC files", report.summary.cpc_files],
    ["Images", report.summary.images],
    ["Ready", report.summary.ready],
    ["Automatic repair", report.summary.automatic_repairable],
    ["Needs review", report.summary.needs_review],
    ["Cannot safely repair", report.summary.cannot_safely_repair],
  ];
  document.getElementById("summaryCards").innerHTML = items.map(([label, value]) =>
    `<div class="summary-card"><strong>${value}</strong><span>${label}</span></div>`
  ).join("");

  renderGlobalIssues(report.global_issues, report.package_ready);
  renderSuggestedFixes(report);
  renderMappings(report);
  renderOptimization(report.optimization, report.pre_annotation);
  renderResults(report.cpc_results);
  packageButton.textContent = report.package_ready
    ? "Download CoralNet-ready package"
    : "Download package and issue reports";
}

function renderGlobalIssues(issues, ready) {
  const holder = document.getElementById("globalIssues");
  const banner = ready
    ? `<div class="issue-box ready"><strong>All CPC files are ready for packaging.</strong> Review any non-blocking warnings before importing.</div>`
    : `<div class="issue-box warning"><strong>Some files still require approval or review.</strong> The package will separate READY files from NEEDS_ATTENTION and EXCLUDED files.</div>`;
  holder.innerHTML = banner + issues.map(issue => issueHtml(issue)).join("");
}

function issueHtml(issue) {
  const details = Object.keys(issue.details || {}).length
    ? `<details><summary>Technical details</summary><code>${escapeHtml(JSON.stringify(issue.details, null, 2))}</code></details>`
    : "";
  return `<div class="issue-box ${issue.severity}">
    <strong>${escapeHtml(issue.message)}</strong>
    ${issue.suggestion ? `<div>${escapeHtml(issue.suggestion)}</div>` : ""}
    <div class="issue-code">${escapeHtml(issue.code)}</div>
    ${details}
  </div>`;
}

function renderSuggestedFixes(report) {
  const panel = document.getElementById("suggestionsPanel");
  const holder = document.getElementById("suggestedFixes");
  const pending = (report.suggested_fixes || []).filter(fix => !fix.accepted);
  if (!report.suggested_fixes?.length) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  approveAllButton.classList.toggle("hidden", pending.length === 0);
  holder.innerHTML = report.suggested_fixes.map(fix => `
    <div class="suggestion-card ${fix.accepted ? "accepted" : ""}">
      <div>
        <div class="suggestion-title"><strong>${escapeHtml(fix.title)}</strong><span class="confidence">${escapeHtml(fix.confidence)} confidence</span></div>
        <p>${escapeHtml(fix.description)}</p>
        <p class="muted"><strong>${fix.affected_count}</strong> CPC file(s)</p>
        <details><summary>Example</summary><code>${escapeHtml(fix.before_example)}\n→ ${escapeHtml(fix.after_example)}</code></details>
      </div>
      ${fix.accepted
        ? `<span class="status ready">Approved</span>`
        : `<button class="secondary button approve-fix" data-fix-id="${escapeHtml(fix.id)}">Approve fix</button>`}
    </div>`).join("");
  document.querySelectorAll(".approve-fix").forEach(button => {
    button.addEventListener("click", () => {
      acceptedSuggestions.add(button.dataset.fixId);
      runValidation(true);
    });
  });
}

function renderMappings(report) {
  const panel = document.getElementById("mappingPanel");
  const imageNeeds = report.cpc_results.filter(result => !result.matched_image);
  const labelNeeds = Object.entries(report.label_inventory || {}).filter(([, info]) => info.status === "unknown");
  if (imageNeeds.length === 0 && labelNeeds.length === 0) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");

  document.getElementById("labelMappings").innerHTML = labelNeeds.length ? `
    <div class="mapping-group"><h3>Dataset-level label mappings</h3>
    <p class="muted">Each mapping applies once across every CPC file that uses the code.</p>
    ${labelNeeds.map(([code, info]) => `<div class="mapping-row">
      <label><strong>${escapeHtml(code)}</strong><br><span class="muted">${info.point_count} point(s) in ${info.file_count} file(s). Suggestions: ${escapeHtml((info.suggestions || []).join(", ") || "none")}</span></label>
      <input type="text" list="label-code-list" data-label-map="${escapeHtml(code)}" value="${escapeHtml(savedLabelOverrides[code] || info.mapped_to || "")}" placeholder="Existing CoralNet short code">
    </div>`).join("")}
    <datalist id="label-code-list">${report.label_codes.map(code => `<option value="${escapeHtml(code)}"></option>`).join("")}</datalist>
    </div>` : "";

  document.getElementById("imageMappings").innerHTML = imageNeeds.length ? `
    <div class="mapping-group"><h3>Manual image mappings</h3>
    <p class="muted">These files remain missing or ambiguous after every safe automatic matching strategy.</p>
    ${imageNeeds.map(result => {
      const options = ["", ...report.available_images.map(image => image.path)];
      return `<div class="mapping-row">
        <label><strong>${escapeHtml(result.cpc_path)}</strong><br><span class="muted">Expected: ${escapeHtml(result.embedded_image_path || "unknown")}</span></label>
        <select data-image-map="${escapeHtml(result.cpc_path)}">
          ${options.map(path => `<option value="${escapeHtml(path)}" ${savedImageOverrides[result.cpc_path] === path ? "selected" : ""}>${path ? escapeHtml(path) : "Select an image…"}</option>`).join("")}
        </select>
      </div>`;
    }).join("")}</div>` : "";

  const applied = report.dataset.path_rules_applied_to_cpcs || 0;
  const matched = report.dataset.matched_after_path_rewrite || 0;
  document.getElementById("bulkRuleStats").innerHTML = applied
    ? `<strong>${applied}</strong> CPC path(s) rewritten; <strong>${matched}</strong> matched.`
    : "Automatic inference is attempted first. Use manual path rules only when needed.";
}

function renderOptimization(optimization, preAnnotation) {
  const holder = document.getElementById("optimizationSummary");
  const blocks = [];
  if (optimization?.enabled) {
    blocks.push(`<div class="feature-summary">
      <strong>CPCe-safe lossless JPEG optimization</strong>
      <span>${optimization.optimized || 0} optimized; ${optimization.kept_original_no_savings || 0} kept because no savings; ${optimization.rejected || 0} rejected; ${formatBytes(optimization.bytes_saved || 0)} saved (${optimization.percent_reduction || 0}%).</span>
    </div>`);
  }
  if (preAnnotation?.enabled) {
    blocks.push(`<div class="feature-summary warning-summary">
      <strong>Separate pre-annotation workflow</strong>
      <span>${preAnnotation.prepared || 0} unannotated image(s) prepared. These outputs must be used before new CPCe points are created.</span>
    </div>`);
  }
  holder.innerHTML = blocks.join("");
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function categoryLabel(category) {
  return {
    ready: "Ready",
    auto_fix: "Automatic repair",
    needs_review: "Needs review",
    cannot_repair: "Cannot safely repair",
  }[category] || category;
}

function resultMatchesFilter(result) {
  if (activeFilter === "all") return true;
  if (activeFilter === "needs_action") return result.action_category !== "ready";
  return result.action_category === activeFilter;
}

function renderResults(results) {
  const body = document.getElementById("resultsBody");
  const visible = results.filter(resultMatchesFilter);
  body.innerHTML = visible.length ? visible.map((result, index) => {
    const issueCodes = result.issues.map(issue => issue.code).join(", ") || "None";
    const scaleDetails = Object.keys(result.scale_diagnostics || {}).length
      ? `<div class="diagnostic-card"><strong>Image compatibility</strong><p>${escapeHtml(result.scale_diagnostics.plain_language || "")}</p><code>${escapeHtml(JSON.stringify(result.scale_diagnostics, null, 2))}</code></div>`
      : "";
    const optimization = result.optimization
      ? `<div class="diagnostic-card"><strong>JPEG optimization</strong><p>${escapeHtml(result.optimization.message)}</p><code>${escapeHtml(JSON.stringify({status: result.optimization.status, bytes_saved: result.optimization.bytes_saved, percent_reduction: result.optimization.percent_reduction, errors: result.optimization.errors}, null, 2))}</code></div>`
      : "";
    const matchDetails = result.matched_image
      ? `<div class="issue-box ready"><strong>Matched using ${escapeHtml(result.match_method || "validated match")}</strong><div>Confidence: ${escapeHtml(result.match_confidence || "—")}</div></div>`
      : "";
    const issueDetails = result.issues.map(issue => issueHtml(issue)).join("");
    return `<tr class="result-row" data-detail="detail-${index}">
      <td><span class="status ${result.action_category}">${escapeHtml(categoryLabel(result.action_category))}</span></td>
      <td>${escapeHtml(result.cpc_path)}</td>
      <td>${escapeHtml(result.embedded_image_name || "—")}</td>
      <td>${escapeHtml(result.matched_image || "—")}</td>
      <td>${escapeHtml(result.match_confidence || "—")}</td>
      <td>${result.point_count}</td>
      <td>${result.scale_factor || "—"}</td>
      <td>${escapeHtml(issueCodes)}</td>
    </tr>
    <tr id="detail-${index}" class="detail-row hidden"><td colspan="8"><div class="details">${matchDetails}${scaleDetails}${optimization}${issueDetails || "<div class='issue-box ready'>No issues found.</div>"}</div></td></tr>`;
  }).join("") : `<tr><td colspan="8" class="empty-state">No files match this filter.</td></tr>`;
  document.querySelectorAll(".result-row").forEach(row => {
    row.addEventListener("click", () => document.getElementById(row.dataset.detail).classList.toggle("hidden"));
  });
}

document.querySelectorAll(".filter").forEach(button => {
  button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach(item => item.classList.toggle("active", item === button));
    if (lastReport) renderResults(lastReport.cpc_results);
  });
});
