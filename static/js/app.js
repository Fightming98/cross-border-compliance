/* 跨境商品合规检查工作台 — 原生 JavaScript，无外部依赖。
 * 所有来自 API 的文本都通过 textContent 写入 DOM，不拼接 HTML。
 */
(() => {
  "use strict";

  const PAGE_SIZE = 20;
  const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;
  const JOB_PATTERN = /^[0-9a-f]{32}$/;
  const RISK_CLASS = { "合规": "compliant", "待确认": "pending", "违规": "violation" };
  const RISK_ORDER = { "合规": 0, "待确认": 1, "违规": 2 };
  const DISPOSITION_CLASS = { "正常评估": "normal", "已下架": "deleted", "库中缺失": "missing" };
  const DISPOSITION_ORDER = { "正常评估": 0, "已下架": 1, "库中缺失": 2 };

  const state = {
    job: null,
    selectedFile: null,
    search: "",
    disposition: "",
    risk: "",
    category: "",
    sortKey: "index",
    sortDir: "asc",
    page: 1,
  };

  const $ = (id) => document.getElementById(id);
  const isIndexPage = Boolean($("upload-form"));

  function createElement(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svgIcon(paths, className = "") {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    if (className) svg.setAttribute("class", className);
    paths.forEach(([tag, attrs]) => {
      const node = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
      svg.appendChild(node);
    });
    return svg;
  }

  function riskIcon(level, className = "risk-icon") {
    const shapes = {
      "合规": [["circle", { cx: "12", cy: "12", r: "9" }], ["path", { d: "m8 12 2.5 2.5L16 9" }]],
      "待确认": [["path", { d: "M12 3 2.8 20h18.4L12 3Z" }], ["path", { d: "M12 9v5M12 17h.01" }]],
      "违规": [["circle", { cx: "12", cy: "12", r: "9" }], ["path", { d: "m9 9 6 6M15 9l-6 6" }]],
    };
    return svgIcon(shapes[level] || shapes["待确认"], className);
  }

  function riskBadge(level) {
    const badge = createElement("span", `badge risk-${RISK_CLASS[level] || "pending"}`);
    badge.appendChild(riskIcon(level, "risk-icon icon-small"));
    badge.appendChild(document.createTextNode(level));
    return badge;
  }

  function pct(part, total) {
    const denominator = Number(total);
    if (!denominator) return "0.0%";
    return `${(Number(part) / denominator * 100).toFixed(1)}%`;
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(Number(bytes))) return "—";
    const value = Number(bytes);
    if (value < 1024) return `${value} 字节`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(2)} MB`;
  }

  function formatDateTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    try {
      return new Intl.DateTimeFormat("zh-CN", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false,
      }).format(date);
    } catch (_error) {
      return String(value);
    }
  }

  function normalizeError(error) {
    if (error && error.name === "AppError") return error;
    return {
      name: "AppError",
      message: error && error.message === "Failed to fetch"
        ? "无法连接本地服务，请确认 Flask 应用仍在运行。"
        : "请求失败，请稍后重试。",
      errorId: "",
      status: 0,
    };
  }

  async function readApiResponse(response) {
    let payload = null;
    try {
      const raw = await response.text();
      payload = raw ? JSON.parse(raw) : null;
    } catch (_error) {
      payload = null;
    }
    if (!response.ok || (payload && payload.ok === false)) {
      const apiError = payload && payload.error ? payload.error : {};
      const error = new Error(apiError.message || `请求失败（HTTP ${response.status}）`);
      error.name = "AppError";
      error.status = response.status;
      error.errorId = apiError.error_id || "";
      throw error;
    }
    return payload;
  }

  /* ------------------------------ 上传 ------------------------------ */

  const uploadForm = $("upload-form");
  const fileInput = $("csv-file");
  const dropZone = $("drop-zone");
  const fileSummary = $("file-summary");
  const fileName = $("file-name");
  const fileMeta = $("file-meta");
  const clearFileButton = $("clear-file");
  const uploadButton = $("upload-button");
  const uploadStatus = $("upload-status");
  const uploadStatusText = $("upload-status-text");
  const uploadError = $("upload-error");
  const uploadErrorTitle = $("upload-error-title");
  const uploadErrorMessage = $("upload-error-message");
  const uploadErrorId = $("upload-error-id");

  function setUploadStatus(kind, text) {
    if (!uploadStatus) return;
    uploadStatus.classList.toggle("is-error", kind === "error");
    uploadStatus.classList.toggle("is-ok", kind === "ok");
    const dot = uploadStatus.querySelector(".status-dot");
    if (dot) {
      dot.classList.toggle("is-idle", kind === "idle");
      dot.classList.toggle("is-busy", kind === "busy");
    }
    if (uploadStatusText) uploadStatusText.textContent = text;
  }

  function hideUploadError() {
    if (uploadError) uploadError.hidden = true;
  }

  function showUploadError(title, message, errorId = "") {
    if (!uploadError) return;
    uploadErrorTitle.textContent = title;
    uploadErrorMessage.textContent = message;
    uploadErrorId.textContent = errorId ? `错误编号：${errorId}` : "";
    uploadErrorId.hidden = !errorId;
    uploadError.hidden = false;
    setUploadStatus("error", title);
  }

  function updateFileSummary(file) {
    if (!file) {
      fileSummary.hidden = true;
      fileName.textContent = "—";
      fileMeta.textContent = "—";
      uploadButton.disabled = true;
      return;
    }
    fileName.textContent = file.name;
    fileMeta.textContent = `${formatBytes(file.size)} · 本地文件`;
    fileSummary.hidden = false;
    uploadButton.disabled = false;
  }

  function clearSelectedFile({ keepStatus = false } = {}) {
    state.selectedFile = null;
    if (fileInput) fileInput.value = "";
    updateFileSummary(null);
    hideUploadError();
    if (!keepStatus) setUploadStatus("idle", "等待选择清单");
  }

  function validateFile(file) {
    if (!file) return "请选择要上传的 CSV 文件。";
    if (!/\.csv$/i.test(file.name || "")) return "仅支持扩展名为 .csv 的文件。";
    if (!file.size) return "所选文件为空，请检查后重新选择。";
    if (file.size > MAX_UPLOAD_BYTES) return "CSV 文件超过 2 MB 限制，请精简后重试。";
    return "";
  }

  function selectFile(file) {
    const message = validateFile(file);
    if (message) {
      clearSelectedFile();
      showUploadError("文件无法上传", message);
      return;
    }
    state.selectedFile = file;
    uploadButton.disabled = false;
    hideUploadError();
    updateFileSummary(file);
    setUploadStatus("idle", "已选择文件，可以开始检查");
    resetResultView();
  }

  function setUploading(isUploading) {
    if (!uploadForm) return;
    uploadForm.setAttribute("aria-busy", String(isUploading));
    uploadButton.disabled = isUploading || !state.selectedFile;
    uploadButton.classList.toggle("is-loading", isUploading);
    const label = uploadButton.querySelector(".button-label");
    if (label) label.textContent = isUploading ? "正在检查…" : "开始检查";
    if (fileInput) fileInput.disabled = isUploading;
    if (clearFileButton) clearFileButton.disabled = isUploading;
  }

  if (isIndexPage) {
    if (dropZone && fileInput) {
      dropZone.setAttribute("tabindex", "0");
      dropZone.setAttribute("role", "button");
      dropZone.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          fileInput.click();
        }
      });
      fileInput.addEventListener("change", () => {
        const file = fileInput.files && fileInput.files[0];
        if (file) selectFile(file);
        else clearSelectedFile();
      });

      let dragDepth = 0;
      dropZone.addEventListener("dragenter", (event) => {
        event.preventDefault();
        dragDepth += 1;
        dropZone.classList.add("is-dragging");
      });
      dropZone.addEventListener("dragover", (event) => {
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      });
      dropZone.addEventListener("dragleave", (event) => {
        event.preventDefault();
        dragDepth = Math.max(0, dragDepth - 1);
        if (!dragDepth) dropZone.classList.remove("is-dragging");
      });
      dropZone.addEventListener("drop", (event) => {
        event.preventDefault();
        dragDepth = 0;
        dropZone.classList.remove("is-dragging");
        const file = event.dataTransfer && event.dataTransfer.files
          ? event.dataTransfer.files[0]
          : null;
        if (file) selectFile(file);
        else showUploadError("文件无法上传", "未检测到可读取的文件。");
      });
    }

    if (clearFileButton) {
      clearFileButton.addEventListener("click", () => clearSelectedFile());
    }

    if (uploadForm) {
      uploadForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        hideUploadError();
        const file = state.selectedFile;
        const message = validateFile(file);
        if (message) {
          showUploadError("文件无法上传", message);
          return;
        }

        resetResultView({ keepUrl: true });
        setUploading(true);
        setUploadStatus("busy", "正在上传并执行合规检查，请稍候…");

        const formData = new FormData();
        formData.append("file", file, file.name);
        try {
          const response = await fetch("/api/reports", {
            method: "POST",
            body: formData,
            headers: { "Accept": "application/json" },
          });
          const payload = await readApiResponse(response);
          if (!payload || !payload.job_id) throw new Error("服务端未返回任务结果。");
          state.job = payload;
          renderDashboard(payload);
          updateUrlJob(payload.job_id);
          setUploadStatus("ok", "检查完成，结果已加载");
          const label = uploadButton.querySelector(".button-label");
          if (label) label.textContent = "重新检查";
          requestAnimationFrame(() => {
            $("results-panel").scrollIntoView({ behavior: "smooth", block: "start" });
          });
        } catch (rawError) {
          const error = normalizeError(rawError);
          const title = error.status === 413
            ? "文件超过大小限制"
            : error.status === 400
              ? "清单无法通过校验"
              : error.status === 500
                ? "服务端处理失败"
                : "检查未完成";
          showUploadError(title, error.message, error.errorId);
        } finally {
          setUploading(false);
          const label = uploadButton.querySelector(".button-label");
          if (label && state.job) label.textContent = "重新检查";
        }
      });
    }

    const initialJob = new URLSearchParams(window.location.search).get("job");
    if (initialJob) {
      if (JOB_PATTERN.test(initialJob)) {
        loadJob(initialJob, { scroll: true });
      } else {
        showUploadError("任务无法加载", "任务编号格式不正确。");
      }
    }
  }

  /* ---------------------------- 任务加载 ---------------------------- */

  function updateUrlJob(jobId) {
    try {
      const url = new URL(window.location.href);
      if (jobId) url.searchParams.set("job", jobId);
      else url.searchParams.delete("job");
      window.history.replaceState({}, "", url);
    } catch (_error) {
      /* 历史记录不可用时不影响主流程 */
    }
  }

  function setLoadBar(visible, message) {
    const bar = $("load-result-bar");
    if (!bar) return;
    bar.hidden = !visible;
    if (message) $("load-result-message").textContent = message;
  }

  async function loadJob(jobId, { scroll = false } = {}) {
    if (!isIndexPage) return;
    const results = $("results-panel");
    if (results) results.hidden = true;
    hideUploadError();
    setLoadBar(true, "正在加载任务结果…");
    try {
      const response = await fetch(`/api/reports/${encodeURIComponent(jobId)}`, {
        headers: { "Accept": "application/json" },
      });
      const payload = await readApiResponse(response);
      if (!payload || !payload.job_id) throw new Error("服务端未返回完整任务结果。");
      state.job = payload;
      renderDashboard(payload);
      setLoadBar(false);
      if (scroll) {
        requestAnimationFrame(() => {
          $("results-panel").scrollIntoView({ behavior: "smooth", block: "start" });
        });
      }
      return true;
    } catch (rawError) {
      const error = normalizeError(rawError);
      setLoadBar(false);
      showUploadError("任务加载失败", error.message, error.errorId);
      return false;
    }
  }

  function resetResultView({ keepUrl = false } = {}) {
    const results = $("results-panel");
    if (results) results.hidden = true;
    state.job = null;
    state.page = 1;
    state.search = "";
    state.disposition = "";
    state.risk = "";
    state.category = "";
    state.sortKey = "index";
    state.sortDir = "asc";
    if (!keepUrl) updateUrlJob(null);
  }

  /* ---------------------------- 结果总览 ---------------------------- */

  function renderDashboard(payload) {
    const results = $("results-panel");
    if (!results) return;
    const summary = payload.summary || {};
    const risks = summary.risk_counts || {};
    const dispositions = summary.disposition_counts || {};
    const details = Array.isArray(payload.details) ? payload.details : [];

    state.job = payload;
    state.page = 1;
    state.search = "";
    state.disposition = "";
    state.risk = "";
    state.category = "";
    state.sortKey = "index";
    state.sortDir = "asc";

    const source = payload.source || {};
    $("task-source").textContent = source.filename || "—";
    $("task-id").textContent = payload.job_id || "—";
    $("task-time").textContent = formatDateTime(payload.generated_at);

    const setText = (id, value) => {
      const node = $(id);
      if (node) node.textContent = String(value ?? "—");
    };
    setText("stat-input", summary.n_input);
    setText("stat-matched", summary.n_matched);
    setText("stat-evaluated", summary.n_evaluated);
    setText("stat-deleted", summary.n_deleted);
    setText("stat-missing", summary.n_missing);
    setText("stat-compliant", risks["合规"]);
    setText("stat-pending", risks["待确认"]);
    setText("stat-violation", risks["违规"]);
    setText("stat-compliant-note", `占评估数 ${pct(risks["合规"], summary.n_evaluated)}`);
    setText("stat-pending-note", `占评估数 ${pct(risks["待确认"], summary.n_evaluated)}`);
    setText("stat-violation-note", `占评估数 ${pct(risks["违规"], summary.n_evaluated)}`);
    const risky = Number(risks["待确认"] || 0) + Number(risks["违规"] || 0);
    setText("risk-ratio-note", `风险商品占比 ${pct(risky, summary.n_evaluated)}`);
    setText("disposition-total-note", `共 ${summary.n_input ?? 0} 条`);
    setText("storage-note", `当前任务 ${payload.job_id || "—"} 的原始上传文件已删除；结果可由本机任务编号重新加载。`);

    renderRiskChart(risks, summary.n_evaluated);
    renderDispositionChart(dispositions, summary.n_input);
    renderCategoryChart(Array.isArray(payload.top_categories) ? payload.top_categories : []);
    populateCategoryFilter(details);
    setDownloadLink($("download-markdown"), payload.links && payload.links.markdown);
    setDownloadLink($("download-html"), payload.links && payload.links.html);
    setDownloadLink($("download-csv"), payload.links && payload.links.csv);

    const search = $("search-input");
    const dispositionFilter = $("disposition-filter");
    const riskFilter = $("risk-filter");
    const categoryFilter = $("category-filter");
    if (search) search.value = "";
    if (dispositionFilter) dispositionFilter.value = "";
    if (riskFilter) riskFilter.value = "";
    if (categoryFilter) categoryFilter.value = "";

    syncSortHeaders();
    renderDetails();
    results.hidden = false;
  }

  function barRow(label, count, totalForPercentage, maxValue, fillClass, tooltip, iconLevel) {
    const row = createElement("div", "bar-row");
    if (tooltip) row.title = tooltip;
    const labelNode = createElement("span", "bar-label");
    if (iconLevel) labelNode.appendChild(riskIcon(iconLevel, "risk-icon icon-small"));
    else labelNode.appendChild(createElement("span", `legend-dot ${fillClass === "bar-normal" ? "dot-normal" : fillClass === "bar-deleted" ? "dot-deleted" : "dot-missing"}`));
    labelNode.appendChild(document.createTextNode(label));
    const area = createElement("div", "bar-area");
    const fill = createElement("div", `bar-fill ${fillClass}`);
    fill.style.width = maxValue > 0 ? pct(count, maxValue) : "0.0%";
    area.appendChild(fill);
    const value = createElement("span", "bar-value", String(count ?? 0));
    value.appendChild(createElement("small", "", pct(count, totalForPercentage)));
    row.appendChild(labelNode);
    row.appendChild(area);
    row.appendChild(value);
    return row;
  }

  function renderRiskChart(risks, evaluated) {
    const chart = $("risk-chart");
    const body = $("risk-data-body");
    if (!chart || !body) return;
    chart.replaceChildren();
    body.replaceChildren();
    const container = createElement("div", "bar-chart");
    const levels = [["合规", "bar-compliant"], ["待确认", "bar-pending"], ["违规", "bar-violation"]];
    const maxValue = Math.max(...levels.map(([level]) => Number(risks[level] || 0)), 0);
    const ariaParts = [];
    levels.forEach(([level, fillClass]) => {
      const count = Number(risks[level] || 0);
      container.appendChild(barRow(
        level, count, evaluated, maxValue, fillClass,
        `${level}：${count} 条，占评估数 ${pct(count, evaluated)}`,
        level,
      ));
      ariaParts.push(`${level} ${count} 条（${pct(count, evaluated)}）`);
      const tr = document.createElement("tr");
      [level, String(count), pct(count, evaluated)].forEach((value) => {
        tr.appendChild(createElement("td", "", value));
      });
      body.appendChild(tr);
    });
    chart.appendChild(container);
    chart.setAttribute("aria-label", `风险等级分布：${ariaParts.join("，")}。`);
  }

  function renderDispositionChart(dispositions, total) {
    const chart = $("disposition-chart");
    const body = $("disposition-data-body");
    if (!chart || !body) return;
    chart.replaceChildren();
    body.replaceChildren();
    const container = createElement("div", "bar-chart");
    const rows = [["正常评估", "bar-normal"], ["已下架", "bar-deleted"], ["库中缺失", "bar-missing"]];
    const maxValue = Math.max(...rows.map(([key]) => Number(dispositions[key] || 0)), 0);
    const ariaParts = [];
    rows.forEach(([label, fillClass]) => {
      const count = Number(dispositions[label] || 0);
      container.appendChild(barRow(
        label, count, total, maxValue, fillClass,
        `${label}：${count} 条，占清单数 ${pct(count, total)}`,
        null,
      ));
      ariaParts.push(`${label} ${count} 条（${pct(count, total)}）`);
      const tr = document.createElement("tr");
      [label, String(count), pct(count, total)].forEach((value) => tr.appendChild(createElement("td", "", value)));
      body.appendChild(tr);
    });
    chart.appendChild(container);
    chart.setAttribute("aria-label", `处置状态分布：${ariaParts.join("，")}。`);
  }

  function renderCategoryChart(categories) {
    const chart = $("category-chart");
    const body = $("category-data-body");
    if (!chart || !body) return;
    chart.replaceChildren();
    body.replaceChildren();
    if (!categories.length) {
      chart.appendChild(createElement("div", "empty-state", "本次没有风险类目记录。"));
      chart.setAttribute("aria-label", "本次没有风险类目记录。");
      const tr = document.createElement("tr");
      const td = createElement("td", "", "无记录");
      td.colSpan = 5;
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    const container = createElement("div", "category-chart");
    const maxTotal = Math.max(...categories.map((item) => Number(item.total || 0)), 1);
    const ariaParts = [];
    categories.forEach((item, index) => {
      const total = Number(item.total || 0);
      const violations = Number(item.violations || 0);
      const pending = Number(item.pending || 0);
      const row = createElement("div", "category-row");
      row.title = `${item.category || "未分类"}：违规 ${violations} 条，待确认 ${pending} 条，共 ${total} 条`;
      row.appendChild(createElement("span", "category-rank", String(index + 1)));
      row.appendChild(createElement("span", "category-label", item.category || "未分类"));
      const wrap = createElement("div", "category-track-wrap");
      const track = createElement("div", "stacked-track");
      track.style.width = pct(total, maxTotal);
      const violationSegment = createElement("span", "segment-violation");
      violationSegment.style.width = total ? pct(violations, total) : "0.0%";
      const pendingSegment = createElement("span", "segment-pending");
      pendingSegment.style.width = total ? pct(pending, total) : "0.0%";
      track.appendChild(violationSegment);
      track.appendChild(pendingSegment);
      wrap.appendChild(track);
      row.appendChild(wrap);
      const value = createElement("span", "bar-value", String(total));
      value.appendChild(createElement("small", "", `违规 ${violations} · 待确认 ${pending}`));
      row.appendChild(value);
      container.appendChild(row);

      ariaParts.push(`${item.category || "未分类"}：违规 ${violations}，待确认 ${pending}，共 ${total}`);
      const tr = document.createElement("tr");
      [String(index + 1), item.category || "未分类", String(violations), String(pending), String(total)]
        .forEach((valueText) => tr.appendChild(createElement("td", "", valueText)));
      body.appendChild(tr);
    });
    chart.appendChild(container);
    chart.setAttribute("aria-label", `Top 风险类目：${ariaParts.join("；")}。`);
  }

  function setDownloadLink(link, href) {
    if (!link) return;
    let safeHref = null;
    if (typeof href === "string" && href.startsWith("/api/reports/") && href.includes("/download/")) {
      try {
        const parsed = new URL(href, window.location.origin);
        if (parsed.origin === window.location.origin) safeHref = parsed.pathname + parsed.search;
      } catch (_error) {
        safeHref = null;
      }
    }
    if (safeHref) {
      link.href = safeHref;
      link.removeAttribute("aria-disabled");
    } else {
      link.href = "#";
      link.setAttribute("aria-disabled", "true");
    }
  }

  /* ---------------------------- 明细表 ---------------------------- */

  function populateCategoryFilter(details) {
    const select = $("category-filter");
    if (!select) return;
    select.replaceChildren();
    select.appendChild(new Option("全部类目", ""));
    const categories = [...new Set(details.map((item) => item.category).filter(Boolean))]
      .sort((a, b) => String(a).localeCompare(String(b), "zh-CN"));
    categories.forEach((category) => select.appendChild(new Option(category, category)));
  }

  function filteredDetails() {
    const job = state.job;
    if (!job || !Array.isArray(job.details)) return [];
    const search = state.search.trim().toLocaleLowerCase("zh-CN");
    return job.details.filter((item) => {
      if (search) {
        const sku = String(item.sku_ref || "").toLocaleLowerCase("zh-CN");
        const name = String(item.name || "").toLocaleLowerCase("zh-CN");
        if (!sku.includes(search) && !name.includes(search)) return false;
      }
      if (state.disposition && item.disposition !== state.disposition) return false;
      if (state.risk === "__none__" && item.risk) return false;
      if (state.risk && state.risk !== "__none__" && item.risk !== state.risk) return false;
      if (state.category && item.category !== state.category) return false;
      return true;
    });
  }

  function sortDetails(items) {
    const key = state.sortKey;
    const direction = state.sortDir === "asc" ? 1 : -1;
    const numericKeys = new Set(["index", "original_price", "cny_price"]);
    return [...items].sort((a, b) => {
      let av = a[key];
      let bv = b[key];
      const aMissing = av === null || av === undefined || av === "";
      const bMissing = bv === null || bv === undefined || bv === "";
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (numericKeys.has(key)) {
        const an = Number(av);
        const bn = Number(bv);
        if (Number.isFinite(an) && Number.isFinite(bn) && an !== bn) return (an - bn) * direction;
      }
      if (key === "risk") {
        const delta = (RISK_ORDER[av] ?? 99) - (RISK_ORDER[bv] ?? 99);
        if (delta) return delta * direction;
      }
      if (key === "disposition") {
        const delta = (DISPOSITION_ORDER[av] ?? 99) - (DISPOSITION_ORDER[bv] ?? 99);
        if (delta) return delta * direction;
      }
      return String(av).localeCompare(String(bv), "zh-CN", { numeric: true }) * direction;
    });
  }

  function syncSortHeaders() {
    document.querySelectorAll(".sort-button").forEach((button) => {
      const th = button.closest("th");
      if (!th) return;
      const active = button.dataset.sortKey === state.sortKey;
      th.setAttribute("aria-sort", active ? (state.sortDir === "asc" ? "ascending" : "descending") : "none");
      const indicator = button.querySelector(".sort-indicator");
      if (indicator) indicator.textContent = active ? (state.sortDir === "asc" ? "↑" : "↓") : "";
    });
  }

  function ruleButton(detail, ruleIds) {
    const button = createElement("button", "rule-button");
    button.type = "button";
    button.setAttribute("aria-expanded", "false");
    button.title = "查看命中规则详情";
    button.appendChild(document.createTextNode(ruleIds.length === 1 ? ruleIds[0] : `${ruleIds.length} 条规则`));
    button.appendChild(svgIcon([["path", { d: "m6 9 6 6 6-6" }]]));
    button.addEventListener("click", () => toggleRuleDetail(button, detail));
    return button;
  }

  function toggleRuleDetail(button, detail) {
    const row = button.closest("tr");
    if (!row) return;
    const next = row.nextElementSibling;
    if (next && next.classList.contains("rule-detail-row")) {
      next.remove();
      button.setAttribute("aria-expanded", "false");
      return;
    }
    const detailRow = createElement("tr", "rule-detail-row");
    const cell = document.createElement("td");
    cell.colSpan = 12;
    const box = createElement("div", "rule-detail-box");
    box.appendChild(createElement("strong", "", `SKU ${detail.sku_ref} · 批次 ${detail.batch_no} · 命中 ${detail.matched_rule_ids.length} 条规则`));
    const list = document.createElement("ul");
    const rules = new Map((state.job.rules || []).map((rule) => [rule.rule_id, rule]));
    detail.matched_rule_ids.forEach((ruleId) => {
      const item = document.createElement("li");
      const rule = rules.get(ruleId);
      const idCode = createElement("code", "", ruleId);
      item.appendChild(idCode);
      item.appendChild(document.createTextNode(rule ? ` ${rule.title}` : " 规则元数据未找到"));
      if (rule) {
        const meta = createElement("span", "rule-detail-meta");
        meta.appendChild(createElement("span", "", `严重程度：${rule.severity || "—"}`));
        if (rule.applies_to_category) meta.appendChild(createElement("span", "", `类目：${rule.applies_to_category}`));
        if (rule.material_keyword) meta.appendChild(createElement("span", "", `材质关键词：${rule.material_keyword}`));
        if (rule.origin_keyword) meta.appendChild(createElement("span", "", `产地关键词：${rule.origin_keyword}`));
        item.appendChild(document.createTextNode(" "));
        item.appendChild(meta);
      }
      list.appendChild(item);
    });
    box.appendChild(list);
    cell.appendChild(box);
    detailRow.appendChild(cell);
    row.after(detailRow);
    button.setAttribute("aria-expanded", "true");
  }

  function displayValue(value) {
    return value === null || value === undefined || value === "" ? "—" : String(value);
  }

  function renderDetails() {
    const body = $("details-body");
    const table = $("details-table");
    const empty = $("table-empty");
    if (!body || !state.job) return;

    const filtered = sortDetails(filteredDetails());
    const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
    state.page = Math.min(Math.max(1, state.page), totalPages);
    const start = (state.page - 1) * PAGE_SIZE;
    const pageItems = filtered.slice(start, start + PAGE_SIZE);
    const fragment = document.createDocumentFragment();

    pageItems.forEach((detail) => {
      const row = document.createElement("tr");
      row.dataset.index = String(detail.index);
      const cells = [];
      cells.push(createElement("td", "cell-index", detail.index));
      const sku = document.createElement("td");
      sku.appendChild(createElement("code", "", detail.sku_ref));
      cells.push(sku);
      const batch = document.createElement("td");
      batch.appendChild(createElement("code", "", detail.batch_no));
      cells.push(batch);
      const disposition = document.createElement("td");
      disposition.appendChild(createElement(
        "span",
        `disposition disposition-${DISPOSITION_CLASS[detail.disposition] || "missing"}`,
        detail.disposition,
      ));
      cells.push(disposition);
      cells.push(createElement("td", "cell-name", displayValue(detail.name)));
      cells.push(createElement("td", "", displayValue(detail.category)));
      cells.push(createElement("td", "", displayValue(detail.material)));
      cells.push(createElement("td", "", displayValue(detail.origin)));
      const original = detail.original_price === null || detail.original_price === undefined
        ? "—"
        : `${detail.original_price} ${displayValue(detail.currency)}`;
      cells.push(createElement("td", "align-right", original));
      const cny = detail.cny_price !== null && detail.cny_price !== undefined
        ? detail.cny_price
        : detail.disposition === "已下架" ? "—（不适用）" : "—";
      cells.push(createElement("td", "align-right", cny));
      const riskCell = document.createElement("td");
      if (detail.risk) riskCell.appendChild(riskBadge(detail.risk));
      else riskCell.appendChild(createElement("span", "muted-value", detail.disposition === "已下架" ? "—（不适用）" : "—"));
      cells.push(riskCell);
      const ruleCell = document.createElement("td");
      ruleCell.className = "cell-rules";
      if (Array.isArray(detail.matched_rule_ids) && detail.matched_rule_ids.length) {
        ruleCell.appendChild(ruleButton(detail, detail.matched_rule_ids));
      } else {
        ruleCell.appendChild(createElement("span", "muted-value", detail.disposition === "已下架" ? "—（不适用）" : "—"));
      }
      cells.push(ruleCell);
      cells.forEach((cell) => row.appendChild(cell));
      fragment.appendChild(row);
    });
    body.replaceChildren(fragment);

    if (table) table.hidden = filtered.length === 0;
    if (empty) empty.hidden = filtered.length !== 0;
    const count = $("result-count");
    if (count) {
      const rangeStart = filtered.length ? start + 1 : 0;
      const rangeEnd = Math.min(start + PAGE_SIZE, filtered.length);
      count.textContent = `当前显示 ${rangeStart}–${rangeEnd} / 筛选结果 ${filtered.length} 条（全部 ${state.job.details.length}）`;
    }
    renderPagination(state.page, totalPages, filtered.length);
  }

  function pageList(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_value, index) => index + 1);
    const pages = [...new Set([1, total, current - 1, current, current + 1])]
      .filter((page) => page >= 1 && page <= total)
      .sort((a, b) => a - b);
    const result = [];
    pages.forEach((page, index) => {
      if (index && page - pages[index - 1] > 1) result.push("…");
      result.push(page);
    });
    return result;
  }

  function renderPagination(current, totalPages, filteredCount) {
    const prev = $("prev-page");
    const next = $("next-page");
    const buttons = $("page-buttons");
    const summary = $("page-summary");
    if (prev) prev.disabled = current <= 1 || filteredCount === 0;
    if (next) next.disabled = current >= totalPages || filteredCount === 0;
    if (summary) summary.textContent = filteredCount ? `第 ${current} / ${totalPages} 页` : "无匹配记录";
    if (!buttons) return;
    buttons.replaceChildren();
    pageList(current, totalPages).forEach((page) => {
      if (page === "…") {
        buttons.appendChild(createElement("span", "page-button", "…"));
        return;
      }
      const button = createElement("button", `page-button${page === current ? " is-current" : ""}`, String(page));
      button.type = "button";
      button.setAttribute("aria-label", `第 ${page} 页`);
      if (page === current) button.setAttribute("aria-current", "page");
      button.addEventListener("click", () => {
        state.page = page;
        renderDetails();
      });
      buttons.appendChild(button);
    });
  }

  function bindFilterEvents() {
    if (!isIndexPage) return;
    const search = $("search-input");
    if (search) {
      search.addEventListener("input", () => {
        state.search = search.value;
        state.page = 1;
        renderDetails();
      });
    }
    const disposition = $("disposition-filter");
    if (disposition) {
      disposition.addEventListener("change", () => {
        state.disposition = disposition.value;
        state.page = 1;
        renderDetails();
      });
    }
    const risk = $("risk-filter");
    if (risk) {
      risk.addEventListener("change", () => {
        state.risk = risk.value;
        state.page = 1;
        renderDetails();
      });
    }
    const category = $("category-filter");
    if (category) {
      category.addEventListener("change", () => {
        state.category = category.value;
        state.page = 1;
        renderDetails();
      });
    }
    const reset = $("reset-filters");
    if (reset) {
      reset.addEventListener("click", () => {
        state.search = "";
        state.disposition = "";
        state.risk = "";
        state.category = "";
        state.sortKey = "index";
        state.sortDir = "asc";
        state.page = 1;
        if (search) search.value = "";
        if (disposition) disposition.value = "";
        if (risk) risk.value = "";
        if (category) category.value = "";
        syncSortHeaders();
        renderDetails();
      });
    }
    const prev = $("prev-page");
    if (prev) {
      prev.addEventListener("click", () => {
        if (state.page > 1) {
          state.page -= 1;
          renderDetails();
        }
      });
    }
    const next = $("next-page");
    if (next) {
      next.addEventListener("click", () => {
        state.page += 1;
        renderDetails();
      });
    }
    document.querySelectorAll(".sort-button").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.sortKey;
        if (!key) return;
        if (state.sortKey === key) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        else {
          state.sortKey = key;
          state.sortDir = "asc";
        }
        state.page = 1;
        syncSortHeaders();
        renderDetails();
      });
    });
  }

  const deleteButton = $("delete-job-button");
  if (deleteButton) {
    deleteButton.addEventListener("click", async () => {
      const job = state.job;
      if (!job || !job.job_id) return;
      const confirmed = window.confirm(
        `确定删除任务 ${job.job_id} 的全部结果文件吗？此操作不可恢复，之后无法再通过该任务编号下载报告。`,
      );
      if (!confirmed) return;
      const originalText = deleteButton.textContent;
      deleteButton.disabled = true;
      deleteButton.textContent = "正在删除…";
      try {
        const response = await fetch(`/api/reports/${encodeURIComponent(job.job_id)}`, {
          method: "DELETE",
          headers: { "Accept": "application/json" },
        });
        if (!response.ok && response.status !== 404) await readApiResponse(response);
        resetResultView();
        hideUploadError();
        setUploadStatus("idle", "任务结果已删除，可上传新清单");
        if (isIndexPage) {
          const upload = $("upload-form");
          if (upload) upload.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      } catch (rawError) {
        const error = normalizeError(rawError);
        showUploadError("删除任务失败", error.message, error.errorId);
      } finally {
        deleteButton.disabled = false;
        deleteButton.textContent = originalText;
      }
    });
  }

  const errorBack = $("error-back");
  if (errorBack) errorBack.addEventListener("click", () => window.history.back());

  bindFilterEvents();
})();
