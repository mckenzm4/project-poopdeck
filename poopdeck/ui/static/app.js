/* Project Poopdeck — Zero-Node Offline Command Center Client */
"use strict";

(() => {
  // Resolve W3C SVG namespace natively from the HTML5 foreign-content <svg> element
  const SVG_NS =
    typeof document !== "undefined"
      ? document.querySelector("svg")?.namespaceURI || null
      : null;

  const EVENT_ATTR_RE = /^on/i;
  const URL_ATTRS = new Set(["href", "src", "action", "formaction", "xlink:href"]);

  const DOMAIN_COLORS = {
    ops_supply_chain: "#38bdf8",
    retail_cpg: "#34d399",
    finance_risk: "#fbbf24",
    b2b_saas: "#818cf8",
    manufacturing: "#fb923c",
    generic: "#94a3b8",
  };

  const READINESS_COLORS = {
    1: "#ef4444",
    2: "#f97316",
    3: "#eab308",
    4: "#22c55e",
    5: "#10b981",
  };

  const CONFIDENCE_RADII = {
    hypothesis: 12,
    directional: 17,
    finance_verified: 22,
  };

  const LINEAGE_COLUMNS = [
    { key: "external_data", title: "1. External Data", x: 24 },
    { key: "dataset", title: "2. Lakehouse Datasets", x: 244 },
    { key: "opportunity", title: "3. DS Opportunities", x: 464 },
    { key: "report", title: "4. Power BI Reports", x: 684 },
    { key: "stakeholder", title: "5. Stakeholders", x: 904 },
  ];

  const state = {
    activeTab: "portfolio",
    health: null,
    capacityWeeks: 12.0,
    domainFilter: "all",
    colorMode: "domain",
    includeArchived: false,
    portfolio: null,
    selectedOppId: null,
    stagingItems: [],
    selectedStagingId: null,
    lineage: { nodes: [], edges: [] },
    lineageIncludeStaging: true,
    selectedLineageNodeId: "",
    questionsData: null,
    qCategory: "all",
    qStatus: "all",
    currentBriefMarkdown: "",
  };

  let lastCopyAnnouncement = "";
  const copyTimers = new WeakMap();
  let toastTimer = null;

  const MAX_PLOT_MAG = 1e300;

  function finiteNum(val, fallback = 0) {
    const n = typeof val === "number" ? val : Number(val);
    return Number.isFinite(n) ? n : fallback;
  }

  function clampPlotNum(val, fallback = 0) {
    const n = typeof val === "number" ? val : Number(val);
    if (!Number.isFinite(n)) {
      if (n === Infinity) return MAX_PLOT_MAG;
      if (n === -Infinity) return -MAX_PLOT_MAG;
      return fallback;
    }
    if (n > MAX_PLOT_MAG) return MAX_PLOT_MAG;
    if (n < -MAX_PLOT_MAG) return -MAX_PLOT_MAG;
    return n;
  }

  function formatCompactNum(val, decimals = 1) {
    const n = finiteNum(val, 0);
    if (Math.abs(n) >= 1e9) {
      return n.toExponential(2);
    }
    return n.toFixed(decimals);
  }

  function formatWeeks(val, decimals = 1) {
    return `${formatCompactNum(val, decimals)}w`;
  }

  function formatUsd(val) {
    if (val === null || val === undefined || val === "") return "$0";
    const raw = typeof val === "number" ? val : Number(val);
    if (Number.isNaN(raw)) return "$0";
    const n = Number.isFinite(raw) ? raw : clampPlotNum(raw, 0);
    const abs = Math.abs(n);
    const sign = n < 0 ? "-" : "";
    if (abs >= 1e12) {
      return `${sign}$${abs.toExponential(2)}`;
    }
    if (abs >= 1_000_000) {
      return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
    }
    if (abs >= 1_000) {
      return `${sign}$${Math.round(abs).toLocaleString("en-US")}`;
    }
    return `${sign}$${abs.toFixed(0)}`;
  }

  function isSafeRelativeUrl(raw) {
    const s = String(raw ?? "").trim();
    return s.startsWith("#") || (s.startsWith("/") && !s.startsWith("//"));
  }

  function appendSafeChildren(parent, children) {
    if (children === null || children === undefined || children === false) return;
    if (Array.isArray(children)) {
      for (const item of children) appendSafeChildren(parent, item);
      return;
    }
    if (children instanceof Node) {
      parent.appendChild(children);
    } else {
      parent.appendChild(document.createTextNode(String(children)));
    }
  }

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, val] of Object.entries(attrs || {})) {
      if (val === null || val === undefined || val === false) continue;
      if (key === "className") {
        node.className = String(val);
      } else if (key === "dataset" && typeof val === "object") {
        for (const [dk, dv] of Object.entries(val)) {
          if (dv !== null && dv !== undefined) node.dataset[dk] = String(dv);
        }
      } else if (typeof val === "function" && EVENT_ATTR_RE.test(key)) {
        node.addEventListener(key.slice(2).toLowerCase(), val);
      } else if (EVENT_ATTR_RE.test(key)) {
        continue;
      } else if (
        key === "checked" ||
        key === "disabled" ||
        key === "selected" ||
        key === "hidden" ||
        key === "readOnly" ||
        key === "required"
      ) {
        node[key] = Boolean(val);
      } else if (
        key === "value" &&
        (tag === "input" || tag === "textarea" || tag === "select" || tag === "option")
      ) {
        node.value = String(val);
      } else if (URL_ATTRS.has(key.toLowerCase())) {
        if (isSafeRelativeUrl(val)) node.setAttribute(key, String(val));
      } else if (val === true) {
        node.setAttribute(key, "");
      } else {
        node.setAttribute(key, String(val));
      }
    }
    appendSafeChildren(node, children);
    return node;
  }

  function svgEl(tag, attrs = {}, children = []) {
    const ns = SVG_NS || document.querySelector("svg")?.namespaceURI;
    const node = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
    for (const [key, val] of Object.entries(attrs || {})) {
      if (val === null || val === undefined || val === false) continue;
      if (typeof val === "function" && EVENT_ATTR_RE.test(key)) {
        node.addEventListener(key.slice(2).toLowerCase(), val);
        continue;
      }
      if (EVENT_ATTR_RE.test(key) || URL_ATTRS.has(key.toLowerCase())) continue;
      if (typeof val === "number" && !Number.isFinite(val)) continue;
      node.setAttribute(key, String(val));
    }
    appendSafeChildren(node, children);
    return node;
  }

  function clearNode(node) {
    if (node) node.replaceChildren();
  }

  function showToast(message) {
    const live = document.getElementById("toast-live");
    if (!live) return;
    clearTimeout(toastTimer);
    live.textContent = String(message || "");
    toastTimer = setTimeout(() => {
      live.textContent = "";
    }, 2800);
  }

  function showError(message) {
    const banner = document.getElementById("global-error");
    if (!banner) return;
    if (!message) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    banner.hidden = false;
    banner.textContent = String(message);
  }

  async function apiJson(path, options = {}) {
    if (!String(path).startsWith("/api/")) {
      throw new Error("Blocked non-local API request");
    }
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    let payload = null;
    try {
      payload = await resp.json();
    } catch {
      payload = null;
    }
    if (!resp.ok) {
      const detail =
        (payload && (payload.detail || payload.message)) ||
        `HTTP ${resp.status} on ${path}`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    showError("");
    return payload;
  }

  function legacyTextareaCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;inset-block-start:0;opacity:0;pointer-events:none;";
    document.body.appendChild(ta);
    ta.select();
    try {
      return Boolean(document.execCommand("copy"));
    } catch {
      return false;
    } finally {
      ta.remove();
    }
  }

  async function copyTextWithFallback(text, buttonEl, statusEl) {
    if (!text) return false;
    let ok = false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(text);
        ok = true;
      } else {
        ok = legacyTextareaCopy(text);
      }
    } catch {
      ok = legacyTextareaCopy(text);
    }

    if (buttonEl) {
      clearTimeout(copyTimers.get(buttonEl));
      buttonEl.dataset.copyState = ok ? "copied" : "failed";
      copyTimers.set(
        buttonEl,
        setTimeout(() => {
          delete buttonEl.dataset.copyState;
          if (statusEl) statusEl.textContent = "";
        }, 2200)
      );
    }
    const msg = ok ? "Copied to clipboard" : "Copy failed — select raw Markdown";
    let announcement = msg;
    if (announcement === lastCopyAnnouncement) announcement += "\u00A0";
    lastCopyAnnouncement = announcement;
    if (statusEl) statusEl.textContent = msg;
    showToast(announcement);
    return ok;
  }

  // Safe inline & block Markdown-to-DOM renderer (zero HTML string parsing)
  function renderInlineTokens(rawText) {
    const str = String(rawText ?? "");
    const tokenRe = /(\[VERIFY WITH STAKEHOLDER\]|\[\[[^\[\]\n]+\]\]|`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
    const out = [];
    let lastIndex = 0;
    let match;
    while ((match = tokenRe.exec(str)) !== null) {
      if (match.index > lastIndex) {
        out.push(document.createTextNode(str.slice(lastIndex, match.index)));
      }
      const tok = match[0];
      if (tok === "[VERIFY WITH STAKEHOLDER]") {
        out.push(el("span", { className: "badge badge-verify" }, tok));
      } else if (tok.startsWith("[[") && tok.endsWith("]]")) {
        out.push(el("span", { className: "wikilink-pill" }, tok));
      } else if (tok.startsWith("`") && tok.endsWith("`")) {
        out.push(el("code", { className: "inline-code" }, tok.slice(1, -1)));
      } else if (tok.startsWith("**") && tok.endsWith("**")) {
        out.push(el("strong", {}, tok.slice(2, -2)));
      } else {
        out.push(document.createTextNode(tok));
      }
      lastIndex = tokenRe.lastIndex;
    }
    if (lastIndex < str.length) {
      out.push(document.createTextNode(str.slice(lastIndex)));
    }
    return out;
  }

  function splitPipeRow(line) {
    const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return trimmed.split("|").map((c) => c.trim());
  }

  function renderSafeMarkdown(text) {
    const wrapper = el("div", { className: "md-rendered" });
    const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];
      const trimmed = line.trim();

      if (!trimmed) {
        i += 1;
        continue;
      }

      if (trimmed.startsWith("```")) {
        const codeLines = [];
        i += 1;
        while (i < lines.length && !lines[i].trim().startsWith("```")) {
          codeLines.push(lines[i]);
          i += 1;
        }
        i += 1;
        wrapper.appendChild(
          el("pre", { className: "md-pre", tabIndex: 0 }, [
            el("code", {}, codeLines.join("\n")),
          ])
        );
        continue;
      }

      if (
        trimmed.startsWith("|") &&
        i + 1 < lines.length &&
        /^\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1].trim())
      ) {
        const headers = splitPipeRow(lines[i]);
        i += 2;
        const bodyRows = [];
        while (i < lines.length && lines[i].trim().startsWith("|")) {
          bodyRows.push(splitPipeRow(lines[i]));
          i += 1;
        }
        const thead = el("thead", {}, [
          el(
            "tr",
            {},
            headers.map((h) => el("th", { scope: "col" }, renderInlineTokens(h)))
          ),
        ]);
        const tbody = el(
          "tbody",
          {},
          bodyRows.map((row) =>
            el(
              "tr",
              {},
              row.map((cell) => el("td", {}, renderInlineTokens(cell)))
            )
          )
        );
        wrapper.appendChild(
          el(
            "div",
            {
              className: "table-wrapper",
              tabIndex: 0,
              role: "region",
              "aria-label": "Rendered Markdown table",
            },
            [el("table", { className: "data-table" }, [thead, tbody])]
          )
        );
        continue;
      }

      const hMatch = /^(#{1,4})\s+(.*)$/.exec(trimmed);
      if (hMatch) {
        const level = Math.min(5, hMatch[1].length + 2);
        wrapper.appendChild(
          el(`h${level}`, { className: "md-h" }, renderInlineTokens(hMatch[2]))
        );
        i += 1;
        continue;
      }

      if (trimmed.startsWith(">")) {
        const quoteText = trimmed.replace(/^>\s?/, "");
        wrapper.appendChild(
          el("blockquote", { className: "md-quote" }, renderInlineTokens(quoteText))
        );
        i += 1;
        continue;
      }

      if (/^[-*]\s+/.test(trimmed)) {
        const ul = el("ul", {});
        while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
          const itemText = lines[i].trim().replace(/^[-*]\s+/, "");
          ul.appendChild(el("li", {}, renderInlineTokens(itemText)));
          i += 1;
        }
        wrapper.appendChild(ul);
        continue;
      }

      if (/^\d+\.\s+/.test(trimmed)) {
        const ol = el("ol", {});
        while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
          const itemText = lines[i].trim().replace(/^\d+\.\s+/, "");
          ol.appendChild(el("li", {}, renderInlineTokens(itemText)));
          i += 1;
        }
        wrapper.appendChild(ol);
        continue;
      }

      wrapper.appendChild(el("p", {}, renderInlineTokens(trimmed)));
      i += 1;
    }

    return wrapper;
  }

  // Theme & Tabs
  function initThemeToggle() {
    const btn = document.getElementById("btn-theme-toggle");
    const metaScheme = document.querySelector('meta[name="color-scheme"]');
    if (!btn) return;
    btn.addEventListener("click", () => {
      const root = document.documentElement;
      const next = root.dataset.theme === "light" ? "dark" : "light";
      root.dataset.theme = next;
      btn.setAttribute("aria-pressed", String(next === "light"));
      btn.textContent = next === "light" ? "Dark Mode" : "Light Mode";
      if (metaScheme) {
        metaScheme.setAttribute("content", next === "light" ? "light dark" : "dark light");
      }
    });
  }

  function switchTab(tabKey, focusTab = false) {
    state.activeTab = tabKey;
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    for (const tab of tabs) {
      const targetPanelId = tab.getAttribute("aria-controls");
      const isMatch = tab.id === `tab-${tabKey}`;
      tab.setAttribute("aria-selected", String(isMatch));
      tab.tabIndex = isMatch ? 0 : -1;
      if (isMatch && focusTab) tab.focus();
      const panel = targetPanelId ? document.getElementById(targetPanelId) : null;
      if (panel) panel.hidden = !isMatch;
    }
  }

  function initTabs() {
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
    tabs.forEach((tab, idx) => {
      tab.addEventListener("click", () => {
        const key = tab.id.replace(/^tab-/, "");
        switchTab(key, false);
      });
      tab.addEventListener("keydown", (ev) => {
        let nextIdx = null;
        if (ev.key === "ArrowRight") nextIdx = (idx + 1) % tabs.length;
        else if (ev.key === "ArrowLeft") nextIdx = (idx - 1 + tabs.length) % tabs.length;
        else if (ev.key === "Home") nextIdx = 0;
        else if (ev.key === "End") nextIdx = tabs.length - 1;
        if (nextIdx !== null) {
          ev.preventDefault();
          const nextKey = tabs[nextIdx].id.replace(/^tab-/, "");
          switchTab(nextKey, true);
        }
      });
    });
  }

  function updateHeaderTelemetry() {
    const vaultPill = document.getElementById("pill-vault-dir");
    const capPill = document.getElementById("pill-capacity");
    const stagingPill = document.getElementById("pill-staging-count");
    const tabStaging = document.getElementById("tab-badge-staging");
    const openQPill = document.getElementById("pill-open-questions");

    if (vaultPill && state.health) {
      const statusSuffix = state.health.vault_exists ? "" : " (uninitialized)";
      vaultPill.textContent = `Vault: ${state.health.vault_dir}${statusSuffix}`;
    }
    if (capPill && state.portfolio) {
      const used = formatCompactNum(state.portfolio.used_capacity_weeks, 1);
      const cap = formatCompactNum(
        finiteNum(state.portfolio.capacity_weeks, state.capacityWeeks),
        1
      );
      capPill.textContent = `Capacity: ${used} / ${cap}w`;
    }
    const stCount =
      state.stagingItems.length ||
      finiteNum(state.portfolio && state.portfolio.staging_count, 0);
    if (stagingPill) stagingPill.textContent = `Staging: ${stCount}`;
    if (tabStaging) tabStaging.textContent = String(stCount);

    if (openQPill && state.questionsData && state.questionsData.summary) {
      const openCount = finiteNum(state.questionsData.summary.open, 0);
      openQPill.textContent = `Open Qs: ${openCount}`;
    }
  }

  // =========================================================================
  // VIEW 1: ROI Bubble Matrix & Boss-Pin Quarterly Capacity Simulator
  // =========================================================================
  function getPortfolioItems() {
    if (!state.portfolio) return [];
    return state.portfolio.opportunities || state.portfolio.items || [];
  }

  function getBubbleFill(item) {
    if (state.colorMode === "readiness") {
      const r = Math.min(
        5,
        Math.max(1, Math.round(finiteNum(item && item.data_readiness_score, 3)))
      );
      return Object.hasOwn(READINESS_COLORS, r) ? READINESS_COLORS[r] : "#38bdf8";
    }
    const pack = item && typeof item.domain_pack === "string" ? item.domain_pack : "";
    return Object.hasOwn(DOMAIN_COLORS, pack) ? DOMAIN_COLORS[pack] : "#38bdf8";
  }

  function syncDomainSelectOptions(availableDomains) {
    const sel = document.getElementById("domain-filter");
    if (!sel || !Array.isArray(availableDomains)) return;
    const existing = new Set(Array.from(sel.options).map((o) => o.value));
    for (const dom of availableDomains) {
      if (dom && !existing.has(dom)) {
        sel.appendChild(el("option", { value: dom }, dom));
        existing.add(dom);
      }
    }
  }

  function renderCapacitySimulator(data) {
    const summaryEl = document.getElementById("cap-metrics-summary");
    const segMandate = document.getElementById("seg-mandate");
    const segFunded = document.getElementById("seg-funded");
    const segRem = document.getElementById("seg-remaining");
    const cutMarker = document.getElementById("cutline-marker");
    const callout = document.getElementById("displacement-callout");
    const heading = document.getElementById("displacement-heading");
    const listEl = document.getElementById("displacement-list");

    const C = Math.max(0, clampPlotNum(data.capacity_weeks, state.capacityWeeks));
    const M = Math.max(0, clampPlotNum(data.mandate_weeks, 0));
    const U = Math.max(0, clampPlotNum(data.used_capacity_weeks, 0));
    const R = Math.max(0, clampPlotNum(data.remaining_capacity_weeks, 0));
    const overflow = Math.max(0, clampPlotNum(data.overflow_weeks, 0));
    const over = Boolean(data.over_capacity);

    const scheduleItems =
      (data.schedule && data.schedule.items) ||
      getPortfolioItems().filter((i) => i.status === "backlog" || i.status === "active");
    const fundedNonMandate = Math.max(
      0,
      scheduleItems
        .filter((i) => i.above_cutline && !i.boss_mandate)
        .reduce(
          (acc, i) => clampPlotNum(acc + Math.max(0, clampPlotNum(i.effort_weeks, 0)), 0),
          0
        )
    );

    const totalAllocated = clampPlotNum(M + fundedNonMandate, 0);
    const scaleMax = Math.max(C, U, totalAllocated, 0.5);
    const clampPct = (v) => Math.min(100, Math.max(0, finiteNum(v, 0)));
    const mandatePct = clampPct((M / scaleMax) * 100);
    const fundedPct = Math.min(100 - mandatePct, clampPct((fundedNonMandate / scaleMax) * 100));
    const remPct = over ? 0 : clampPct((Math.max(0, C - totalAllocated) / scaleMax) * 100);
    const cutPct = clampPct((C / scaleMax) * 100);

    if (segMandate) {
      segMandate.style.width = `${mandatePct.toFixed(2)}%`;
      segMandate.textContent = M > 0 ? `P0 ${formatWeeks(M, 1)}` : "";
    }
    if (segFunded) {
      segFunded.style.width = `${fundedPct.toFixed(2)}%`;
      segFunded.textContent = fundedNonMandate > 0 ? `ROI ${formatWeeks(fundedNonMandate, 1)}` : "";
    }
    if (segRem) {
      segRem.style.width = `${remPct.toFixed(2)}%`;
      segRem.textContent = !over && R >= 1.0 ? `${formatWeeks(R, 1)} free` : "";
    }
    if (cutMarker) {
      cutMarker.style.insetInlineStart = `${cutPct.toFixed(2)}%`;
    }

    if (summaryEl) {
      const overText = over ? ` | OVER CAPACITY BY +${formatWeeks(overflow, 1)}` : "";
      summaryEl.textContent = `Allocated ${formatWeeks(U, 1)} of ${formatWeeks(
        C,
        1
      )} (${formatWeeks(M, 1)} P0 Mandate, ${formatWeeks(R, 1)} unallocated)${overText}`;
    }

    const displacedItems = scheduleItems.filter((i) => i.displaced_by_mandate);
    if (callout && listEl) {
      clearNode(listEl);
      if (displacedItems.length === 0 && !over) {
        callout.hidden = true;
      } else {
        callout.hidden = false;
        if (heading) {
          heading.textContent = over
            ? `OVER CAPACITY (+${formatWeeks(overflow, 1)}): P0 Boss Mandates Exceed Quarterly Budget`
            : `Tradeoff Alert: ${displacedItems.length} ROI-Funded Opportunity Shifted Past Quarterly Cutline`;
        }
        for (const d of displacedItems) {
          listEl.appendChild(
            el("li", {}, [
              el("strong", {}, `${d.id}: ${d.title}`),
              ` (${formatWeeks(d.effort_weeks, 1)}, Score ${formatCompactNum(
                d.computed_priority_score,
                1
              )}, Base ${formatUsd(d.annual_benefit_base_usd)}) — shifted from Baseline Rank #${
                d.baseline_rank
              } above cutline to Rank #${d.rank} below cutline.`,
            ])
          );
        }
      }
    }
  }

  function renderRoiBubbleMatrix(restoreFocusOppId = null) {
    const svg = document.getElementById("roi-matrix-svg");
    const legend = document.getElementById("matrix-legend");
    if (!svg) return;
    clearNode(svg);
    svg.appendChild(svgEl("title", { id: "roi-svg-title" }, "Value versus Effort ROI Bubble Matrix"));

    const items = getPortfolioItems();
    const W = 860;
    const H = 440;
    const padL = 82;
    const padR = 34;
    const padT = 26;
    const padB = 52;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    svg.appendChild(
      svgEl("rect", {
        x: padL,
        y: padT,
        width: plotW,
        height: plotH,
        fill: "none",
        stroke: "var(--border-subtle)",
      })
    );

    if (items.length === 0) {
      svg.appendChild(
        svgEl(
          "text",
          {
            x: W / 2,
            y: H / 2,
            "text-anchor": "middle",
            fill: "var(--text-muted)",
            "font-size": 14,
          },
          "No active or backlog opportunities in vault for this filter."
        )
      );
      if (legend) clearNode(legend);
      return;
    }

    const xMin = 0;
    const maxEffort = Math.max(
      ...items.map((i) => Math.max(0, clampPlotNum(i.effort_weeks, 1))),
      4
    );
    let xSpan = Math.max(maxEffort * 1.18, 1.0);
    if (!Number.isFinite(xSpan) || xSpan <= 0) xSpan = Math.max(maxEffort, 1.0);

    const yMin = Math.min(0, ...items.map((i) => clampPlotNum(i.annual_benefit_low_usd, 0)));
    const maxHigh = Math.max(
      ...items.map((i) =>
        Math.max(
          clampPlotNum(i.annual_benefit_high_usd, 0),
          clampPlotNum(i.annual_benefit_base_usd, 0)
        )
      ),
      10000
    );
    const rawYDiff = Math.max(0, clampPlotNum(maxHigh - yMin, 10000));
    let ySpan = Math.max(rawYDiff * 1.15, 10000);
    if (!Number.isFinite(ySpan) || ySpan <= 0) ySpan = Math.max(rawYDiff, 10000);

    const toX = (eff) => {
      const clamped = Math.max(xMin, clampPlotNum(eff, 0));
      const ratio = Math.min(1, Math.max(0, finiteNum((clamped - xMin) / xSpan, 0)));
      return padL + ratio * plotW;
    };
    const toY = (usd) => {
      const clamped = clampPlotNum(usd, 0);
      const ratio = Math.min(1, Math.max(0, finiteNum((clamped - yMin) / ySpan, 0)));
      return padT + plotH - ratio * plotH;
    };

    for (let step = 0; step <= 4; step += 1) {
      const yVal = yMin + (ySpan * step) / 4;
      const yPx = toY(yVal);
      svg.appendChild(
        svgEl("line", {
          x1: padL,
          y1: yPx,
          x2: padL + plotW,
          y2: yPx,
          stroke: "var(--border-subtle)",
          "stroke-dasharray": "3 3",
        })
      );
      svg.appendChild(
        svgEl(
          "text",
          {
            x: padL - 10,
            y: yPx + 4,
            "text-anchor": "end",
            fill: "var(--text-muted)",
            "font-size": 11,
          },
          formatUsd(yVal)
        )
      );

      const xVal = xMin + (xSpan * step) / 4;
      const xPx = toX(xVal);
      svg.appendChild(
        svgEl("line", {
          x1: xPx,
          y1: padT,
          x2: xPx,
          y2: padT + plotH,
          stroke: "var(--border-subtle)",
          "stroke-dasharray": "3 3",
        })
      );
      svg.appendChild(
        svgEl(
          "text",
          {
            x: xPx,
            y: padT + plotH + 18,
            "text-anchor": "middle",
            fill: "var(--text-muted)",
            "font-size": 11,
          },
          formatWeeks(xVal, 1)
        )
      );
    }

    svg.appendChild(
      svgEl(
        "text",
        {
          x: padL + plotW / 2,
          y: H - 10,
          "text-anchor": "middle",
          fill: "var(--text-secondary)",
          "font-size": 12,
          "font-weight": 600,
        },
        "Estimated Delivery Effort (person-weeks)"
      )
    );

    svg.appendChild(
      svgEl(
        "text",
        {
          x: 18,
          y: padT + plotH / 2,
          transform: `rotate(-90 18 ${padT + plotH / 2})`,
          "text-anchor": "middle",
          fill: "var(--text-secondary)",
          "font-size": 12,
          "font-weight": 600,
        },
        "Annualized Benefit Range (USD)"
      )
    );

    let focusTarget = null;
    for (const item of items) {
      const cx = toX(item.effort_weeks);
      const cy = toY(item.annual_benefit_base_usd);
      const yLow = toY(item.annual_benefit_low_usd);
      const yHigh = toY(item.annual_benefit_high_usd);
      const tier =
        item && typeof item.confidence_tier === "string" ? item.confidence_tier : "";
      const radius = Object.hasOwn(CONFIDENCE_RADII, tier) ? CONFIDENCE_RADII[tier] : 15;
      const fill = getBubbleFill(item);
      const isSelected = state.selectedOppId === item.id;
      const opacity = item.above_cutline ? 0.9 : 0.45;

      const selectHandler = (fromKeyboard = false) => {
        state.selectedOppId = item.id;
        renderRoiBubbleMatrix(fromKeyboard === true ? item.id : null);
        renderOpportunityDrawer();
      };

      const g = svgEl(
        "g",
        {
          class: "bubble-group",
          tabindex: 0,
          role: "button",
          "aria-label": `${item.id} ${item.title}, Base ${formatUsd(
            item.annual_benefit_base_usd
          )}, Effort ${formatWeeks(item.effort_weeks, 1)}`,
          onClick: () => selectHandler(false),
          onKeydown: (ev) => {
            if (ev.key === "Enter" || ev.key === " ") {
              ev.preventDefault();
              selectHandler(true);
            }
          },
        },
        [
          svgEl(
            "title",
            {},
            `${item.id}: ${item.title} | Base ${formatUsd(item.annual_benefit_base_usd)} (${formatUsd(
              item.annual_benefit_low_usd
            )} - ${formatUsd(item.annual_benefit_high_usd)}) | Effort ${formatWeeks(
              item.effort_weeks,
              1
            )}`
          ),
          svgEl("line", {
            x1: cx,
            y1: yHigh,
            x2: cx,
            y2: yLow,
            stroke: "var(--text-secondary)",
            "stroke-width": 1.6,
          }),
          svgEl("line", {
            x1: cx - 5,
            y1: yHigh,
            x2: cx + 5,
            y2: yHigh,
            stroke: "var(--text-secondary)",
            "stroke-width": 1.6,
          }),
          svgEl("line", {
            x1: cx - 5,
            y1: yLow,
            x2: cx + 5,
            y2: yLow,
            stroke: "var(--text-secondary)",
            "stroke-width": 1.6,
          }),
        ]
      );

      if (item.boss_mandate) {
        g.appendChild(
          svgEl("circle", {
            cx,
            cy,
            r: radius + 5,
            fill: "none",
            stroke: "var(--status-mandate)",
            "stroke-width": 2.6,
          })
        );
      } else if (item.displaced_by_mandate) {
        g.appendChild(
          svgEl("circle", {
            cx,
            cy,
            r: radius + 5,
            fill: "none",
            stroke: "var(--status-displaced)",
            "stroke-width": 2.2,
            "stroke-dasharray": "4 3",
          })
        );
      }

      g.appendChild(
        svgEl("circle", {
          class: "bubble-main",
          cx,
          cy,
          r: radius,
          fill,
          "fill-opacity": opacity,
          stroke: isSelected ? "var(--text-primary)" : "var(--border-strong)",
          "stroke-width": isSelected ? 2.8 : 1.4,
        })
      );

      g.appendChild(
        svgEl(
          "text",
          {
            x: cx,
            y: cy - radius - 7,
            "text-anchor": "middle",
            fill: "var(--text-primary)",
            "font-size": 11,
            "font-weight": 700,
          },
          item.id
        )
      );

      if (restoreFocusOppId && item.id === restoreFocusOppId) {
        focusTarget = g;
      }
      svg.appendChild(g);
    }
    if (focusTarget && typeof focusTarget.focus === "function") {
      focusTarget.focus();
    }

    if (legend) {
      clearNode(legend);
      const entries =
        state.colorMode === "readiness"
          ? Object.entries(READINESS_COLORS).map(([k, col]) => [`Readiness ${k}/5`, col])
          : Object.entries(DOMAIN_COLORS);
      for (const [label, color] of entries) {
        legend.appendChild(
          el("span", { className: "legend-dot" }, [
            el("span", { className: "legend-swatch", style: `background:${color}` }),
            label,
          ])
        );
      }
    }
  }

  async function toggleBossMandate(oppId, explicitBool = null) {
    try {
      const payload = {
        id: oppId,
        boss_mandate: explicitBool,
        capacity_weeks: state.capacityWeeks,
      };
      const res = await apiJson("/api/boss-pin", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.selectedOppId = oppId;
      const isFiltered =
        Boolean(state.domainFilter && state.domainFilter !== "all") ||
        Boolean(state.includeArchived);
      if (!isFiltered && res && res.portfolio) {
        state.portfolio = res.portfolio;
        renderPortfolioAll();
        updateHeaderTelemetry();
      } else {
        await loadPortfolio();
      }
      showToast(
        `${oppId} Boss Mandate ${res.boss_mandate ? "PINNED (P0)" : "unpinned"}`
      );
    } catch (err) {
      showError(err.message);
    }
  }

  function renderOpportunityDrawer() {
    const drawer = document.getElementById("opp-drawer");
    if (!drawer) return;
    clearNode(drawer);

    const items = getPortfolioItems();
    const selected =
      items.find((i) => i.id === state.selectedOppId) || items[0] || null;
    if (!selected) {
      drawer.appendChild(
        el("p", { className: "muted-text" }, "No opportunity available to inspect.")
      );
      return;
    }
    state.selectedOppId = selected.id;

    const val = selected.valuation || {};
    const prompts =
      val.verification_prompts || selected.verification_prompts || [];

    const badges = el("div", { className: "pill-wrap" }, [
      el("span", { className: "badge" }, selected.status),
      el("span", { className: "badge" }, selected.domain_pack),
      el("span", { className: "badge" }, `Confidence: ${selected.confidence_tier}`),
      selected.boss_mandate
        ? el("span", { className: "badge badge-mandate" }, "P0 BOSS MANDATE")
        : null,
      selected.displaced_by_mandate
        ? el("span", { className: "badge badge-displaced" }, "DISPLACED BY MANDATE")
        : selected.above_cutline
        ? el("span", { className: "badge badge-funded" }, "FUNDED ABOVE CUTLINE")
        : null,
    ]);

    const actions = el("div", { className: "callout-actions", style: "margin-top:10px" }, [
      el(
        "button",
        {
          type: "button",
          className: selected.boss_mandate ? "btn btn-mandate" : "btn btn-primary",
          onClick: () => toggleBossMandate(selected.id, !selected.boss_mandate),
        },
        selected.boss_mandate ? "Unpin Boss Mandate" : "Pin as Boss Mandate (P0)"
      ),
      el(
        "button",
        {
          type: "button",
          className: "btn btn-secondary",
          onClick: async () => {
            const briefOpp = document.getElementById("brief-opp-select");
            const briefType = document.getElementById("brief-type-select");
            const briefFormat = document.getElementById("brief-format-select");
            if (briefType) briefType.value = "opportunity";
            if (briefFormat) briefFormat.value = "exec";
            syncBriefOpportunitySelect();
            if (briefOpp) {
              if (!Array.from(briefOpp.options).some((opt) => opt.value === selected.id)) {
                briefOpp.appendChild(
                  el(
                    "option",
                    { value: selected.id },
                    `${selected.id}: ${selected.title} (${selected.status})`
                  )
                );
              }
              briefOpp.value = selected.id;
            }
            syncBriefControls();
            switchTab("briefs", false);
            await generateBrief(false);
          },
        },
        "Generate Exec Brief"
      ),
    ]);

    drawer.appendChild(el("h3", { className: "section-title" }, `${selected.id}: ${selected.title}`));
    drawer.appendChild(badges);
    drawer.appendChild(actions);

    drawer.appendChild(
      el("div", { className: "metric-tiles" }, [
        el("div", { className: "metric-tile" }, [
          el("span", {}, "Low / Base / High"),
          el(
            "strong",
            {},
            `${formatUsd(selected.annual_benefit_low_usd)} / ${formatUsd(
              selected.annual_benefit_base_usd
            )} / ${formatUsd(selected.annual_benefit_high_usd)}`
          ),
        ]),
        el("div", { className: "metric-tile" }, [
          el("span", {}, "Effort & Score"),
          el(
            "strong",
            {},
            `${formatWeeks(selected.effort_weeks, 1)} | ${formatCompactNum(
              selected.computed_priority_score,
              1
            )}/100`
          ),
        ]),
      ])
    );

    drawer.appendChild(el("p", { className: "muted-text" }, "Auditable Driver Formula:"));
    drawer.appendChild(el("div", { className: "formula-box" }, val.driver_formula || "N/A"));

    if (prompts.length > 0) {
      drawer.appendChild(
        el("div", { className: "verify-panel" }, [
          el("strong", {}, `[VERIFY WITH STAKEHOLDER] (${prompts.length})`),
          el(
            "ul",
            {},
            prompts.map((p) => el("li", {}, p))
          ),
        ])
      );
    }
  }

  function renderPortfolioTable() {
    const tbody = document.getElementById("portfolio-tbody");
    const warnEl = document.getElementById("portfolio-warnings");
    if (!tbody) return;
    clearNode(tbody);

    if (warnEl) {
      const warns = (state.portfolio && state.portfolio.warnings) || [];
      warnEl.textContent = warns.length ? `${warns.length} card warning(s)` : "";
    }

    const items = getPortfolioItems();
    if (items.length === 0) {
      tbody.appendChild(
        el("tr", {}, [
          el("td", { colSpan: 12, className: "muted-text" }, "No opportunities loaded."),
        ])
      );
      return;
    }

    for (const item of items) {
      let stateBadge;
      if (item.boss_mandate) {
        stateBadge = el("span", { className: "badge badge-mandate" }, "P0 Mandate");
      } else if (item.displaced_by_mandate) {
        stateBadge = el("span", { className: "badge badge-displaced" }, "Displaced");
      } else if (item.above_cutline) {
        stateBadge = el("span", { className: "badge badge-funded" }, "Funded");
      } else {
        stateBadge = el("span", { className: "badge" }, "Below Cutline");
      }

      const tr = el(
        "tr",
        {
          className: item.displaced_by_mandate ? "row-displaced" : "",
          onClick: () => {
            state.selectedOppId = item.id;
            renderRoiBubbleMatrix();
            renderOpportunityDrawer();
          },
        },
        [
          el("td", { className: "mono" }, item.rank !== null && item.rank !== undefined ? `#${item.rank}` : "-"),
          el(
            "td",
            { className: "mono" },
            item.baseline_rank !== null && item.baseline_rank !== undefined
              ? `#${item.baseline_rank}`
              : "-"
          ),
          el("td", {}, [el("strong", {}, item.id), ` — ${item.title}`]),
          el("td", {}, el("span", { className: "badge" }, item.status)),
          el("td", { className: "mono" }, item.domain_pack),
          el("td", { className: "mono" }, formatWeeks(item.effort_weeks, 1)),
          el(
            "td",
            { className: "mono" },
            item.cumulative_effort_weeks !== null && item.cumulative_effort_weeks !== undefined
              ? formatWeeks(item.cumulative_effort_weeks, 1)
              : "-"
          ),
          el(
            "td",
            { className: "mono" },
            `${formatUsd(item.annual_benefit_base_usd)} (${formatUsd(
              item.annual_benefit_low_usd
            )}–${formatUsd(item.annual_benefit_high_usd)})`
          ),
          el("td", { className: "mono" }, item.confidence_tier),
          el("td", { className: "mono" }, formatCompactNum(item.computed_priority_score, 1)),
          el("td", {}, stateBadge),
          el("td", {}, [
            el(
              "button",
              {
                type: "button",
                className: item.boss_mandate
                  ? "btn btn-small btn-mandate"
                  : "btn btn-small",
                onClick: (ev) => {
                  ev.stopPropagation();
                  toggleBossMandate(item.id, !item.boss_mandate);
                },
              },
            item.boss_mandate ? "Unpin P0" : "Pin Mandate"
            ),
          ]),
        ]
      );
      tbody.appendChild(tr);
    }
  }

  function renderPortfolioAll() {
    if (!state.portfolio) return;
    const items = getPortfolioItems();
    if (items.length === 0) {
      state.selectedOppId = null;
    } else if (!items.some((i) => i.id === state.selectedOppId)) {
      state.selectedOppId = items[0].id;
    }
    syncDomainSelectOptions(state.portfolio.available_domains || []);
    renderCapacitySimulator(state.portfolio);
    renderRoiBubbleMatrix();
    renderOpportunityDrawer();
    renderPortfolioTable();
    syncBriefOpportunitySelect();
  }

  async function loadPortfolio() {
    const qs = new URLSearchParams({
      capacity_weeks: String(state.capacityWeeks),
      domain: state.domainFilter,
      include_archived: String(state.includeArchived),
    });
    const data = await apiJson(`/api/portfolio?${qs.toString()}`);
    state.portfolio = data;
    renderPortfolioAll();
    updateHeaderTelemetry();
  }

  // =========================================================================
  // VIEW 2: Staging Gate & Skeptical CFO Review Inbox
  // =========================================================================
  function renderStagingView() {
    const queueEl = document.getElementById("staging-queue");
    const inspEl = document.getElementById("staging-inspector");
    const pillEl = document.getElementById("staging-summary-pill");
    if (!queueEl || !inspEl) return;
    clearNode(queueEl);
    clearNode(inspEl);

    const items = state.stagingItems || [];
    if (pillEl) {
      pillEl.textContent = `${items.length} Staged Candidate${items.length === 1 ? "" : "s"}`;
    }

    if (items.length === 0) {
      queueEl.appendChild(
        el(
          "div",
          { className: "section-card muted-text" },
          "Staging inbox is empty — all candidate opportunities have been reviewed."
        )
      );
      inspEl.appendChild(
        el(
          "p",
          { className: "muted-text" },
          "Run /discover-opportunities in Claude Code or poopdeck demo to populate candidate specs in vault/staging/opportunities/."
        )
      );
      return;
    }

    const active =
      items.find((i) => i.id === state.selectedStagingId) || items[0];
    state.selectedStagingId = active.id;

    for (const item of items) {
      const verdict = item.cfo_critique_verdict || "PENDING_CFO_GATE";
      const unverified = finiteNum(item.unverified_drivers_count, 0);
      const cardBtn = el(
        "button",
        {
          type: "button",
          className: `queue-card${item.id === active.id ? " is-active" : ""}`,
          onClick: () => {
            state.selectedStagingId = item.id;
            renderStagingView();
          },
        },
        [
          el("div", { className: "card-header-row", style: "margin:0" }, [
            el("strong", {}, `${item.id}: ${item.title}`),
            el("span", { className: "badge badge-staging" }, verdict),
          ]),
          el("div", { className: "pill-wrap" }, [
            el("span", { className: "badge" }, `Base ${formatUsd(item.annual_benefit_base_usd)}`),
            el("span", { className: "badge" }, formatWeeks(item.effort_weeks, 1)),
            unverified > 0
              ? el("span", { className: "badge badge-verify" }, `${unverified} [VERIFY]`)
              : el("span", { className: "badge badge-funded" }, "All Drivers Verified"),
          ]),
        ]
      );
      queueEl.appendChild(cardBtn);
    }

    const reasonInput = el("input", {
      type: "text",
      placeholder: "Optional archive rationale (e.g. DAX measure covers 80%)",
      "aria-label": "Archive reason",
      style: "min-width:240px",
    });

    const actionRow = el("div", { className: "toolbar-inline" }, [
      el("div", {}, [
        el("h3", { className: "section-title" }, `${active.id}: ${active.title}`),
        el(
          "p",
          { className: "muted-text" },
          `Verdict: ${active.cfo_critique_verdict || "PENDING_CFO_GATE"} | Confidence: ${
            active.confidence_tier
          } | Effort: ${formatWeeks(active.effort_weeks, 1)}`
        ),
      ]),
      el("div", { className: "filter-cluster" }, [
        el(
          "button",
          {
            type: "button",
            className: "btn btn-primary",
            onClick: async () => {
              try {
                await apiJson("/api/promote", {
                  method: "POST",
                  body: JSON.stringify({ id: active.id, target_status: "backlog" }),
                });
                showToast(`Promoted ${active.id} to portfolio backlog`);
                await Promise.all([loadStaging(), loadPortfolio(), loadLineage()]);
              } catch (err) {
                showError(err.message);
              }
            },
          },
          "Promote to Backlog"
        ),
        reasonInput,
        el(
          "button",
          {
            type: "button",
            className: "btn btn-secondary",
            onClick: async () => {
              try {
                const reason = reasonInput.value.trim() || null;
                await apiJson("/api/archive", {
                  method: "POST",
                  body: JSON.stringify({ id: active.id, reason }),
                });
                showToast(`Archived ${active.id}`);
                await Promise.all([loadStaging(), loadPortfolio(), loadLineage()]);
              } catch (err) {
                showError(err.message);
              }
            },
          },
          "Archive"
        ),
      ]),
    ]);
    inspEl.appendChild(actionRow);

    const val = active.valuation || {};
    const prompts = val.verification_prompts || active.verify_prompts || [];
    if (prompts.length > 0) {
      inspEl.appendChild(
        el("div", { className: "verify-panel" }, [
          el("strong", {}, `Unverified Economic Assumptions (${prompts.length})`),
          el(
            "ul",
            {},
            prompts.map((p) => el("li", {}, p))
          ),
        ])
      );
    }

    inspEl.appendChild(
      el("div", { className: "metric-tiles" }, [
        el("div", { className: "metric-tile" }, [
          el("span", {}, "Low Estimate"),
          el("strong", {}, formatUsd(active.annual_benefit_low_usd)),
        ]),
        el("div", { className: "metric-tile" }, [
          el("span", {}, "Base Estimate"),
          el("strong", {}, formatUsd(active.annual_benefit_base_usd)),
        ]),
        el("div", { className: "metric-tile" }, [
          el("span", {}, "High Estimate"),
          el("strong", {}, formatUsd(active.annual_benefit_high_usd)),
        ]),
      ])
    );

    inspEl.appendChild(el("h4", { className: "md-h" }, "Driver-Tree Formula & Inputs"));
    inspEl.appendChild(el("div", { className: "formula-box" }, val.driver_formula || "N/A"));

    const drivers = Array.isArray(val.drivers) ? val.drivers : [];
    if (drivers.length > 0) {
      const drvTable = el("table", { className: "data-table" }, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { scope: "col" }, "Driver"),
            el("th", { scope: "col" }, "Value / Range"),
            el("th", { scope: "col" }, "Verified"),
            el("th", { scope: "col" }, "Source"),
          ]),
        ]),
        el(
          "tbody",
          {},
          drivers.map((d) =>
            el("tr", {}, [
              el("td", { className: "mono" }, d.name),
              el(
                "td",
                { className: "mono" },
                Array.isArray(d.range) ? `[${d.range.join(", ")}]` : String(d.value ?? "-")
              ),
              el(
                "td",
                {},
                d.verified
                  ? el("span", { className: "badge badge-funded" }, "Verified")
                  : el("span", { className: "badge badge-verify" }, "UNVERIFIED")
              ),
              el("td", {}, d.source || "-"),
            ])
          )
        ),
      ]);
      inspEl.appendChild(
        el(
          "div",
          {
            className: "table-wrapper",
            tabIndex: 0,
            role: "region",
            "aria-label": "Valuation drivers",
            style: "margin-top:10px",
          },
          [drvTable]
        )
      );
    }

    const critiqueText = String(active.cfo_critique || "").trim();
    const bodyText = String(active.body || "").trim();
    if (critiqueText && (!bodyText || !bodyText.includes(critiqueText))) {
      inspEl.appendChild(el("h4", { className: "md-h" }, "Skeptical CFO Critique"));
      inspEl.appendChild(renderSafeMarkdown(critiqueText));
    }
    if (bodyText) {
      inspEl.appendChild(
        el(
          "h4",
          { className: "md-h" },
          critiqueText && bodyText.includes(critiqueText)
            ? "Skeptical CFO Critique & Spec Body"
            : "Candidate Spec Body"
        )
      );
      inspEl.appendChild(renderSafeMarkdown(bodyText));
    } else if (!critiqueText) {
      inspEl.appendChild(el("h4", { className: "md-h" }, "Skeptical CFO Critique & Spec Body"));
      inspEl.appendChild(el("p", { className: "muted-text" }, "(No critique or spec body)"));
    }

    inspEl.appendChild(
      el(
        "h4",
        { className: "md-h" },
        "Protected HUMAN-NOTES (preserved byte-for-byte on Promote/Archive)"
      )
    );
    inspEl.appendChild(
      el(
        "div",
        { className: "notes-preserve-box" },
        active.human_notes !== null && active.human_notes !== undefined && active.human_notes !== ""
          ? active.human_notes
          : "(Empty HUMAN-NOTES block)"
      )
    );
  }

  async function loadStaging() {
    const data = await apiJson("/api/staging");
    state.stagingItems = data.items || data.opportunities || [];
    renderStagingView();
    syncBriefOpportunitySelect();
    updateHeaderTelemetry();
  }

  // =========================================================================
  // VIEW 3: Interactive Dataset -> Opportunity -> Power BI Lineage Graph
  // =========================================================================
  function columnIndexForType(type) {
    if (type === "external_data") return 0;
    if (type === "dataset") return 1;
    if (type === "opportunity") return 2;
    if (type === "existing_report" || type === "new_report") return 3;
    return 4;
  }

  function computeConnectedNodeSet(nodes, edges, rootId) {
    if (!rootId) return null;
    const nodeIds = new Set((nodes || []).map((n) => n.id));
    if (!nodeIds.has(rootId)) return new Set();

    const parents = new Map();
    const children = new Map();
    for (const e of edges || []) {
      if (!e || !e.source || !e.target) continue;
      const src = String(e.source);
      const dst = String(e.target);
      if (!children.has(src)) children.set(src, []);
      children.get(src).push(dst);
      if (!parents.has(dst)) parents.set(dst, []);
      parents.get(dst).push(src);
    }

    const ancestors = new Set([rootId]);
    const qUp = [rootId];
    for (let i = 0; i < qUp.length; i += 1) {
      const curr = qUp[i];
      for (const p of parents.get(curr) || []) {
        if (!ancestors.has(p)) {
          ancestors.add(p);
          qUp.push(p);
        }
      }
    }

    const descendants = new Set([rootId]);
    const qDown = [rootId];
    for (let i = 0; i < qDown.length; i += 1) {
      const curr = qDown[i];
      for (const c of children.get(curr) || []) {
        if (!descendants.has(c)) {
          descendants.add(c);
          qDown.push(c);
        }
      }
    }

    return new Set([...ancestors, ...descendants]);
  }

  function populateLineageSelect(nodes) {
    const sel = document.getElementById("lineage-filter-select");
    if (!sel) return;
    const prev = state.selectedLineageNodeId;
    clearNode(sel);
    sel.appendChild(el("option", { value: "" }, "Show Full Vault Lineage"));

    const reports = nodes.filter(
      (n) => n.type === "existing_report" || n.type === "new_report"
    );
    const others = nodes.filter(
      (n) => n.type !== "existing_report" && n.type !== "new_report"
    );
    for (const n of [...reports, ...others]) {
      sel.appendChild(el("option", { value: n.id }, `[${n.type}] ${n.label}`));
    }
    if (prev && nodes.some((n) => n.id === prev)) {
      sel.value = prev;
    } else {
      state.selectedLineageNodeId = "";
      sel.value = "";
    }
  }

  function renderLineageDetail(nodes, edges) {
    const detail = document.getElementById("lineage-detail");
    if (!detail) return;
    clearNode(detail);

    const target = nodes.find((n) => n.id === state.selectedLineageNodeId);
    if (!target) {
      detail.appendChild(
        el(
          "p",
          { className: "muted-text" },
          "Click any node in the graph—especially a Power BI Report—to highlight every DS opportunity upgrading it, upstream datasets, and stakeholders."
        )
      );
      return;
    }

    const meta = target.metadata || {};
    detail.appendChild(el("h3", { className: "section-title" }, target.label));
    detail.appendChild(
      el("div", { className: "pill-wrap", style: "margin-block:8px" }, [
        el("span", { className: "badge" }, target.type),
        target.exists_in_vault === false
          ? el("span", { className: "badge badge-verify" }, "UNRESOLVED [[WIKILINK]]")
          : el("span", { className: "badge badge-funded" }, "In Vault"),
      ])
    );

    const incoming = edges
      .filter((e) => e.target === target.id)
      .map((e) => ({
        relation: e.relation,
        node: nodes.find((n) => n.id === e.source),
      }))
      .filter((x) => x.node);

    const outgoing = edges
      .filter((e) => e.source === target.id)
      .map((e) => ({
        relation: e.relation,
        node: nodes.find((n) => n.id === e.target),
      }))
      .filter((x) => x.node);

    if (target.type === "existing_report") {
      const upgradingOpps = incoming.filter(
        (x) => x.node.type === "opportunity"
      );
      detail.appendChild(
        el("h4", { className: "md-h" }, `Opportunities Upgrading This Report (${upgradingOpps.length})`)
      );
      if (upgradingOpps.length === 0) {
        detail.appendChild(
          el("p", { className: "muted-text" }, "No DS opportunities linked to this report yet.")
        );
      } else {
        detail.appendChild(
          el(
            "ul",
            {},
            upgradingOpps.map((u) => el("li", {}, `${u.node.label} (${u.relation})`))
          )
        );
      }
      if (Array.isArray(meta.bi_to_ds_gaps) && meta.bi_to_ds_gaps.length > 0) {
        detail.appendChild(el("h4", { className: "md-h" }, "Flagged BI / DAX Gaps"));
        detail.appendChild(
          el(
            "ul",
            {},
            meta.bi_to_ds_gaps.map((g) => el("li", {}, g))
          )
        );
      }
    }

    if (incoming.length > 0) {
      detail.appendChild(el("h4", { className: "md-h" }, "Upstream Inputs & Owners"));
      detail.appendChild(
        el(
          "ul",
          {},
          incoming.map((x) => el("li", {}, `${x.node.label} [${x.relation}]`))
        )
      );
    }

    if (outgoing.length > 0) {
      detail.appendChild(el("h4", { className: "md-h" }, "Downstream Consumers"));
      detail.appendChild(
        el(
          "ul",
          {},
          outgoing.map((x) => el("li", {}, `${x.node.label} [${x.relation}]`))
        )
      );
    }
  }

  function renderLineageGraph(restoreFocusNodeId = null) {
    const svg = document.getElementById("lineage-svg");
    const pills = document.getElementById("lineage-summary-pills");
    if (!svg) return;
    clearNode(svg);
    svg.appendChild(
      svgEl(
        "title",
        { id: "lineage-svg-title" },
        "Interactive Dataset to Opportunity to Power BI Lineage Graph"
      )
    );

    const nodes = state.lineage.nodes || [];
    const edges = state.lineage.edges || [];

    if (pills) {
      clearNode(pills);
      pills.appendChild(el("span", { className: "badge" }, `${nodes.length} Nodes`));
      pills.appendChild(el("span", { className: "badge" }, `${edges.length} Edges`));
    }

    const nodeW = 192;
    const nodeH = 54;
    const rowGap = 16;
    const topOffset = 56;

    const cols = [[], [], [], [], []];
    for (const n of nodes) {
      cols[columnIndexForType(n.type)].push(n);
    }

    const maxRows = nodes.length === 0 ? 1 : Math.max(1, ...cols.map((c) => c.length));
    const svgH = nodes.length === 0 ? 300 : Math.max(420, topOffset + maxRows * (nodeH + rowGap) + 32);
    svg.setAttribute("viewBox", `0 0 1120 ${svgH}`);

    for (const colDef of LINEAGE_COLUMNS) {
      svg.appendChild(
        svgEl(
          "text",
          {
            x: colDef.x + nodeW / 2,
            y: 30,
            "text-anchor": "middle",
            fill: "var(--text-secondary)",
            "font-size": 12,
            "font-weight": 700,
          },
          colDef.title
        )
      );
    }

    if (nodes.length === 0) {
      svg.appendChild(
        svgEl(
          "text",
          {
            x: 560,
            y: 165,
            "text-anchor": "middle",
            fill: "var(--text-muted)",
            "font-size": 14,
          },
          "No vault lineage nodes found yet."
        )
      );
      renderLineageDetail(nodes, edges);
      return;
    }

    const posById = new Map();
    cols.forEach((colNodes, colIdx) => {
      const colX = LINEAGE_COLUMNS[colIdx].x;
      colNodes.forEach((n, rowIdx) => {
        posById.set(n.id, {
          col: colIdx,
          x: colX,
          y: topOffset + rowIdx * (nodeH + rowGap),
        });
      });
    });

    const connectedSet = computeConnectedNodeSet(
      nodes,
      edges,
      state.selectedLineageNodeId
    );

    for (const edge of edges) {
      const src = posById.get(edge.source);
      const dst = posById.get(edge.target);
      if (!src || !dst) continue;

      const y1 = src.y + nodeH / 2;
      const y2 = dst.y + nodeH / 2;
      let dPath;
      if (src.col < dst.col) {
        const x1 = src.x + nodeW;
        const x2 = dst.x;
        const dx = Math.max(40, (x2 - x1) * 0.45);
        dPath = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
      } else if (src.col === dst.col) {
        const x1 = src.x + nodeW;
        const x2 = dst.x + nodeW;
        const arcX = x1 + 38;
        dPath =
          edge.source === edge.target
            ? `M ${x1} ${y1 - 6} C ${arcX} ${y1 - 20}, ${arcX} ${y1 + 20}, ${x2} ${y1 + 6}`
            : `M ${x1} ${y1} C ${arcX} ${y1}, ${arcX} ${y2}, ${x2} ${y2}`;
      } else {
        const x1 = src.x;
        const x2 = dst.x + nodeW;
        const dx = Math.max(40, (x1 - x2) * 0.45);
        dPath = `M ${x1} ${y1} C ${x1 - dx} ${y1}, ${x2 + dx} ${y2}, ${x2} ${y2}`;
      }

      const edgeActive =
        !connectedSet || (connectedSet.has(edge.source) && connectedSet.has(edge.target));
      svg.appendChild(
        svgEl("path", {
          class: `lineage-edge${edgeActive ? "" : " is-dimmed"}`,
          d: dPath,
          fill: "none",
          stroke: edgeActive && connectedSet ? "var(--accent-primary)" : "var(--border-strong)",
          "stroke-width": edgeActive && connectedSet ? 2.2 : 1.5,
        })
      );
    }

    let focusTarget = null;
    for (const node of nodes) {
      const pos = posById.get(node.id);
      if (!pos) continue;
      const isPlaceholder = node.exists_in_vault === false;
      const isSelected = state.selectedLineageNodeId === node.id;
      const isActive = !connectedSet || connectedSet.has(node.id);

      const classes = ["lineage-node"];
      if (isPlaceholder) classes.push("is-placeholder");
      if (isSelected) classes.push("is-selected");
      if (!isActive) classes.push("is-dimmed");

      const rawLabel = String(node.label || node.id);
      const shortLabel = rawLabel.length > 26 ? `${rawLabel.slice(0, 24)}..` : rawLabel;
      const subTag = isPlaceholder
        ? "UNRESOLVED [[REF]]"
        : String(
            (node.metadata &&
              (node.metadata.medallion_layer ||
                node.metadata.status ||
                node.metadata.influence_tier ||
                node.metadata.acquisition)) ||
              node.type
          );

      const activateNode = (fromKeyboard = false) => {
        state.selectedLineageNodeId =
          state.selectedLineageNodeId === node.id ? "" : node.id;
        const sel = document.getElementById("lineage-filter-select");
        if (sel) sel.value = state.selectedLineageNodeId;
        renderLineageGraph(fromKeyboard === true ? node.id : null);
      };

      const g = svgEl(
        "g",
        {
          class: classes.join(" "),
          tabindex: 0,
          role: "button",
          "aria-label": `${node.type}: ${rawLabel}`,
          onClick: () => activateNode(false),
          onKeydown: (ev) => {
            if (ev.key === "Enter" || ev.key === " ") {
              ev.preventDefault();
              activateNode(true);
            }
          },
        },
        [
          svgEl("title", {}, `${node.type}: ${rawLabel}`),
          svgEl("rect", {
            class: "node-box",
            x: pos.x,
            y: pos.y,
            width: nodeW,
            height: nodeH,
            rx: 8,
            fill: "var(--bg-surface)",
            stroke: isSelected
              ? "var(--accent-primary)"
              : isPlaceholder
              ? "var(--status-displaced)"
              : "var(--border-strong)",
            "stroke-width": isSelected ? 2.6 : 1.5,
          }),
          svgEl(
            "text",
            {
              x: pos.x + 10,
              y: pos.y + 22,
              fill: "var(--text-primary)",
              "font-size": 11.5,
              "font-weight": 700,
            },
            shortLabel
          ),
          svgEl(
            "text",
            {
              x: pos.x + 10,
              y: pos.y + 40,
              fill: isPlaceholder ? "var(--status-displaced)" : "var(--text-muted)",
              "font-size": 10,
            },
            subTag
          ),
        ]
      );
      if (restoreFocusNodeId && node.id === restoreFocusNodeId) {
        focusTarget = g;
      }
      svg.appendChild(g);
    }
    if (focusTarget && typeof focusTarget.focus === "function") {
      focusTarget.focus();
    }

    renderLineageDetail(nodes, edges);
  }

  async function loadLineage() {
    const qs = new URLSearchParams({
      include_staging: String(state.lineageIncludeStaging),
    });
    const data = await apiJson(`/api/lineage?${qs.toString()}`);
    state.lineage = data;
    populateLineageSelect(data.nodes || []);
    renderLineageGraph();
  }

  // =========================================================================
  // VIEW 4: Day 1-90 Playbook Readiness & Open Questions Tracker
  // =========================================================================
  function renderReadinessView() {
    const data = state.questionsData;
    if (!data) return;
    const summary = data.summary || {};
    const phases = data.phases || data.playbook_phases || [];
    const questions = data.questions || [];

    const pbPct = finiteNum(summary.playbook_progress_pct, 0);
    const qPct = finiteNum(summary.question_progress_pct, 0);
    const allPct = finiteNum(summary.readiness_pct, 0);

    const pbPctEl = document.getElementById("kpi-playbook-pct");
    const pbBar = document.getElementById("playbook-progress-bar");
    const pbSub = document.getElementById("kpi-playbook-sub");
    if (pbPctEl) pbPctEl.textContent = `${pbPct.toFixed(0)}%`;
    if (pbBar) pbBar.style.width = `${Math.min(100, Math.max(0, pbPct))}%`;
    if (pbSub) {
      pbSub.textContent = `${finiteNum(summary.completed_phases, 0)} of ${finiteNum(
        summary.total_phases,
        0
      )} phases completed`;
    }

    const qPctEl = document.getElementById("kpi-questions-pct");
    const qBar = document.getElementById("question-progress-bar");
    const qSub = document.getElementById("kpi-questions-sub");
    if (qPctEl) qPctEl.textContent = `${qPct.toFixed(0)}%`;
    if (qBar) qBar.style.width = `${Math.min(100, Math.max(0, qPct))}%`;
    if (qSub) {
      qSub.textContent = `${finiteNum(summary.answered, 0)} answered | ${finiteNum(
        summary.open,
        0
      )} open | ${finiteNum(summary.blocked, 0)} blocked`;
    }

    const allPctEl = document.getElementById("kpi-overall-pct");
    if (allPctEl) allPctEl.textContent = `${allPct.toFixed(0)}%`;

    const catPills = document.getElementById("readiness-category-pills");
    if (catPills) {
      clearNode(catPills);
      for (const [cat, counts] of Object.entries(summary.by_category || {})) {
        catPills.appendChild(
          el("span", { className: "badge" }, `${cat}: ${counts.answered}/${counts.total}`)
        );
      }
    }

    const pbGrid = document.getElementById("playbook-grid");
    if (pbGrid) {
      clearNode(pbGrid);
      for (const phase of phases) {
        const cb = el("input", {
          type: "checkbox",
          checked: Boolean(phase.completed),
          onChange: async (ev) => {
            try {
              await apiJson(`/api/readiness/${encodeURIComponent(phase.id)}`, {
                method: "PATCH",
                body: JSON.stringify({ completed: ev.target.checked }),
              });
              showToast(`Updated ${phase.id}`);
              await loadQuestions();
            } catch (err) {
              showError(err.message);
            }
          },
        });
        pbGrid.appendChild(
          el(
            "div",
            { className: `phase-card${phase.completed ? " is-complete" : ""}` },
            [
              el("label", { className: "checkbox-label" }, [
                cb,
                el("strong", {}, `${phase.id} — ${phase.phase}`),
              ]),
              el("p", { className: "muted-text" }, phase.goal),
              el(
                "ul",
                {},
                (phase.actions || []).map((act) => el("li", {}, act))
              ),
              el("span", { className: "badge" }, `Output: ${phase.output}`),
            ]
          )
        );
      }
    }

    const qList = document.getElementById("questions-list");
    if (qList) {
      clearNode(qList);
      if (questions.length === 0) {
        qList.appendChild(
          el("p", { className: "muted-text" }, "No discovery questions match current filter.")
        );
      }
      for (const q of questions) {
        const statusSel = el(
          "select",
          { "aria-label": `Status for ${q.id}`, value: q.status },
          [
            el("option", { value: "open", selected: q.status === "open" }, "open"),
            el("option", { value: "answered", selected: q.status === "answered" }, "answered"),
            el("option", { value: "blocked", selected: q.status === "blocked" }, "blocked"),
          ]
        );
        statusSel.value = q.status;

        const ansBox = el("textarea", {
          "aria-label": `Answer summary for ${q.id}`,
          placeholder: "Capture stakeholder answer or blocker notes...",
          value: q.answer_summary || "",
        });

        const saveBtn = el(
          "button",
          {
            type: "button",
            className: "btn btn-primary",
            onClick: async () => {
              try {
                await apiJson(`/api/questions/${encodeURIComponent(q.id)}`, {
                  method: "PATCH",
                  body: JSON.stringify({
                    status: statusSel.value,
                    answer: ansBox.value,
                  }),
                });
                showToast(`Saved ${q.id}`);
                await loadQuestions();
              } catch (err) {
                showError(err.message);
              }
            },
          },
          "Save"
        );

        qList.appendChild(
          el("div", { className: "question-card" }, [
            el("div", { className: "question-meta" }, [
              el("strong", {}, q.id),
              el("span", { className: "badge" }, q.category_label || q.category),
              el("span", { className: "badge" }, `Ask: ${q.who_to_ask}`),
              el("span", { className: "badge" }, q.topic),
            ]),
            el("p", { style: "margin:0;font-weight:600" }, q.question),
            el("div", { className: "question-editor-row" }, [statusSel, ansBox, saveBtn]),
          ])
        );
      }
    }
  }

  async function loadQuestions() {
    const qs = new URLSearchParams({
      category: state.qCategory,
      status: state.qStatus,
    });
    const data = await apiJson(`/api/questions?${qs.toString()}`);
    state.questionsData = data;
    renderReadinessView();
    updateHeaderTelemetry();
  }

  // =========================================================================
  // VIEW 5: 1-Click Microsoft Teams / Exec Pitch / VERIFY Brief Exporter
  // =========================================================================
  function syncBriefOpportunitySelect() {
    const sel = document.getElementById("brief-opp-select");
    const typeSel = document.getElementById("brief-type-select");
    if (!sel || !typeSel) return;

    const prev = sel.value;
    const bType = typeSel.value;
    clearNode(sel);

    if (bType === "verify_questions") {
      sel.appendChild(el("option", { value: "" }, "All Opportunities & Open Questions"));
    }

    const seen = new Set();
    const allOpps = [...getPortfolioItems(), ...(state.stagingItems || [])];
    for (const o of allOpps) {
      if (!o || !o.id || seen.has(o.id)) continue;
      seen.add(o.id);
      sel.appendChild(el("option", { value: o.id }, `${o.id}: ${o.title} (${o.status})`));
    }

    if (prev && Array.from(sel.options).some((opt) => opt.value === prev)) {
      sel.value = prev;
    }
    syncBriefControls();
  }

  function syncBriefControls() {
    const typeSel = document.getElementById("brief-type-select");
    const oppSel = document.getElementById("brief-opp-select");
    const fmtSel = document.getElementById("brief-format-select");
    if (!typeSel || !oppSel || !fmtSel) return;
    const bType = typeSel.value;
    oppSel.disabled = bType === "weekly_status";
    fmtSel.disabled = bType !== "opportunity";
  }

  async function generateBrief(saveExportOverride = null) {
    const typeSel = document.getElementById("brief-type-select");
    const oppSel = document.getElementById("brief-opp-select");
    const fmtSel = document.getElementById("brief-format-select");
    const capInput = document.getElementById("brief-capacity-input");
    const saveCb = document.getElementById("brief-save-checkbox");
    const savedBanner = document.getElementById("brief-saved-banner");
    const titleEl = document.getElementById("brief-preview-title");
    const badgeEl = document.getElementById("brief-type-badge");
    const previewEl = document.getElementById("brief-rendered-preview");
    const rawEl = document.getElementById("brief-raw-text");

    if (!typeSel || !previewEl || !rawEl) return;

    const bType = typeSel.value;
    const oppId = bType === "weekly_status" ? null : (oppSel && oppSel.value) || null;
    if (bType === "opportunity" && !oppId) {
      clearNode(previewEl);
      previewEl.appendChild(
        el("p", { className: "muted-text" }, "Select an opportunity to generate a 1-page pitch.")
      );
      rawEl.textContent = "";
      state.currentBriefMarkdown = "";
      return;
    }

    const cap = Math.max(0.5, finiteNum(capInput && capInput.value, state.capacityWeeks));
    try {
      const res = await apiJson("/api/brief", {
        method: "POST",
        body: JSON.stringify({
          opp_id: oppId,
          brief_type: bType,
          format: fmtSel ? fmtSel.value : "teams",
          capacity_weeks: cap,
          save_export:
            typeof saveExportOverride === "boolean"
              ? saveExportOverride
              : Boolean(saveCb && saveCb.checked),
        }),
      });

      state.currentBriefMarkdown = res.markdown || "";
      if (titleEl) titleEl.textContent = res.title || "Formatted Safe Preview";
      if (badgeEl) badgeEl.textContent = `${res.brief_type} | ${res.format}`;
      clearNode(previewEl);
      previewEl.appendChild(renderSafeMarkdown(state.currentBriefMarkdown));
      rawEl.textContent = state.currentBriefMarkdown;

      if (savedBanner) {
        if (res.saved_path) {
          savedBanner.hidden = false;
          savedBanner.textContent = `Saved Markdown export to: ${res.saved_path}`;
        } else {
          savedBanner.hidden = true;
          savedBanner.textContent = "";
        }
      }
    } catch (err) {
      showError(err.message);
    }
  }

  // =========================================================================
  // Event Binding & Initial Boot
  // =========================================================================
  function bindControls() {
    const capSlider = document.getElementById("capacity-slider");
    const capInput = document.getElementById("capacity-input");
    const domSelect = document.getElementById("domain-filter");
    const colorSelect = document.getElementById("color-mode-select");
    const incArchCb = document.getElementById("include-archived-checkbox");

    const applyCapacity = (rawVal) => {
      const parsed = finiteNum(rawVal, 12.0);
      if (parsed <= 0) return;
      state.capacityWeeks = parsed;
      if (capSlider) capSlider.value = String(Math.min(52, Math.max(1, parsed)));
      if (capInput) capInput.value = String(parsed);
      const briefCap = document.getElementById("brief-capacity-input");
      if (briefCap) briefCap.value = String(parsed);
      loadPortfolio().catch((e) => showError(e.message));
    };

    if (capSlider) {
      capSlider.addEventListener("change", () => applyCapacity(capSlider.value));
    }
    if (capInput) {
      capInput.addEventListener("change", () => applyCapacity(capInput.value));
    }
    if (domSelect) {
      domSelect.addEventListener("change", () => {
        state.domainFilter = domSelect.value;
        loadPortfolio().catch((e) => showError(e.message));
      });
    }
    if (colorSelect) {
      colorSelect.addEventListener("change", () => {
        state.colorMode = colorSelect.value;
        renderRoiBubbleMatrix();
      });
    }
    if (incArchCb) {
      incArchCb.addEventListener("change", () => {
        state.includeArchived = incArchCb.checked;
        loadPortfolio().catch((e) => showError(e.message));
      });
    }

    const copyTradeoffBtn = document.getElementById("btn-copy-tradeoff");
    const copyTradeoffStatus = document.getElementById("copy-tradeoff-status");
    if (copyTradeoffBtn) {
      copyTradeoffBtn.addEventListener("click", () => {
        if (!state.portfolio) return;
        const displaced = getPortfolioItems().filter((i) => i.displaced_by_mandate);
        const lines = [
          `**Quarterly Capacity Tradeoff (${state.portfolio.used_capacity_weeks}w / ${state.portfolio.capacity_weeks}w)**`,
          `- P0 Boss-Mandate Effort: ${state.portfolio.mandate_weeks}w`,
          ...displaced.map(
            (d) =>
              `- Displaced below cutline: **${d.id} (${d.title})** — ${d.effort_weeks}w, Base ${formatUsd(
                d.annual_benefit_base_usd
              )}, Baseline Rank #${d.baseline_rank} -> #${d.rank}`
          ),
        ];
        copyTextWithFallback(lines.join("\n"), copyTradeoffBtn, copyTradeoffStatus);
      });
    }

    const linStagingCb = document.getElementById("lineage-include-staging");
    const linSelect = document.getElementById("lineage-filter-select");
    const linReset = document.getElementById("btn-lineage-reset");
    if (linStagingCb) {
      linStagingCb.addEventListener("change", () => {
        state.lineageIncludeStaging = linStagingCb.checked;
        loadLineage().catch((e) => showError(e.message));
      });
    }
    if (linSelect) {
      linSelect.addEventListener("change", () => {
        state.selectedLineageNodeId = linSelect.value;
        renderLineageGraph();
      });
    }
    if (linReset) {
      linReset.addEventListener("click", () => {
        state.selectedLineageNodeId = "";
        if (linSelect) linSelect.value = "";
        renderLineageGraph();
      });
    }

    const qCatSel = document.getElementById("q-category-filter");
    const qStatSel = document.getElementById("q-status-filter");
    const copyQsBtn = document.getElementById("btn-copy-category-questions");
    const copyQsStatus = document.getElementById("copy-questions-status");

    if (qCatSel) {
      qCatSel.addEventListener("change", () => {
        state.qCategory = qCatSel.value;
        loadQuestions().catch((e) => showError(e.message));
      });
    }
    if (qStatSel) {
      qStatSel.addEventListener("change", () => {
        state.qStatus = qStatSel.value;
        loadQuestions().catch((e) => showError(e.message));
      });
    }
    if (copyQsBtn) {
      copyQsBtn.addEventListener("click", () => {
        const qs = (state.questionsData && state.questionsData.questions) || [];
        const unanswered = qs.filter((q) => q.status !== "answered");
        if (unanswered.length === 0) {
          showToast("No unanswered questions in current view");
          return;
        }
        const grouped = new Map();
        for (const q of unanswered) {
          const owner = q.who_to_ask || "Stakeholder";
          if (!grouped.has(owner)) grouped.set(owner, []);
          grouped.get(owner).push(`- **${q.id}** (${q.topic}): ${q.question}`);
        }
        const md = [];
        for (const [owner, bullets] of grouped.entries()) {
          md.push(`### ${owner}`, ...bullets, "");
        }
        copyTextWithFallback(md.join("\n").trim(), copyQsBtn, copyQsStatus);
      });
    }

    const briefTypeSel = document.getElementById("brief-type-select");
    const genBriefBtn = document.getElementById("btn-generate-brief");
    const copyBriefBtn = document.getElementById("btn-copy-brief");
    const copyBriefStatus = document.getElementById("copy-brief-status");

    if (briefTypeSel) {
      briefTypeSel.addEventListener("change", () => {
        syncBriefOpportunitySelect();
      });
    }
    if (genBriefBtn) {
      genBriefBtn.addEventListener("click", () => {
        generateBrief();
      });
    }
    if (copyBriefBtn) {
      copyBriefBtn.addEventListener("click", () => {
        copyTextWithFallback(
          state.currentBriefMarkdown,
          copyBriefBtn,
          copyBriefStatus
        );
      });
    }

    const refreshBtn = document.getElementById("btn-refresh-all");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => {
        refreshAll().then(() => showToast("Refreshed from Markdown vault"));
      });
    }
  }

  async function refreshAll() {
    try {
      const health = await apiJson("/api/health");
      state.health = health;
      if (health && Number.isFinite(health.quarterly_capacity_weeks) && !state.portfolio) {
        state.capacityWeeks = health.quarterly_capacity_weeks;
        const capSlider = document.getElementById("capacity-slider");
        const capInput = document.getElementById("capacity-input");
        const briefCap = document.getElementById("brief-capacity-input");
        if (capSlider) capSlider.value = String(Math.min(52, Math.max(1, state.capacityWeeks)));
        if (capInput) capInput.value = String(state.capacityWeeks);
        if (briefCap) briefCap.value = String(state.capacityWeeks);
      }
      await Promise.all([
        loadPortfolio(),
        loadStaging(),
        loadLineage(),
        loadQuestions(),
      ]);
      const briefTypeSel = document.getElementById("brief-type-select");
      if (briefTypeSel && getPortfolioItems().length === 0 && state.stagingItems.length === 0) {
        briefTypeSel.value = "weekly_status";
        syncBriefOpportunitySelect();
      }
      await generateBrief(false);
    } catch (err) {
      showError(err.message);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    initThemeToggle();
    initTabs();
    bindControls();
    refreshAll();
  });
})();
