const PAGE_SIZE = 500;

function parseNonNegativeInt(raw, fallback = 0) {
  const parsed = Number.parseInt(String(raw || ""), 10);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return fallback;
  }
  return parsed;
}

function getTokenFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const token = (params.get("token") || "").trim();
  if (!token) return "";
  params.delete("token");
  const next = `${window.location.pathname}${params.toString() ? `?${params.toString()}` : ""}${window.location.hash}`;
  window.history.replaceState({}, "", next);
  return token;
}

function readViewStateFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const action = (params.get("action") || "statements").trim();
  return {
    action,
    txStatementId: (params.get("statement_id") || "").trim(),
    txStatementProduct: (params.get("statement_product") || "").trim(),
    summaryStatementId: (params.get("summary_statement_id") || "").trim(),
    txCard: (params.get("card_number") || "").trim(),
    txCardholder: (params.get("cardholder_name") || "").trim(),
    txDateFrom: (params.get("tx_date_from") || "").trim(),
    txDateTo: (params.get("tx_date_to") || "").trim(),
    txSearch: (params.get("q") || "").trim(),
    stOffset: parseNonNegativeInt(params.get("st_offset"), 0),
    txOffset: parseNonNegativeInt(params.get("tx_offset"), 0),
  };
}

function writeViewStateToUrl() {
  const url = new URL(window.location.href);
  const p = url.searchParams;
  p.set("action", state.action || "statements");

  const txFields = [
    ["statement_id", els.txStatementId.value.trim()],
    ["statement_product", els.txStatementProduct.value.trim()],
    ["summary_statement_id", els.summaryStatementId.value.trim()],
    ["card_number", els.txCard.value.trim()],
    ["cardholder_name", els.txCardholder.value.trim()],
    ["tx_date_from", els.txDateFrom.value.trim()],
    ["tx_date_to", els.txDateTo.value.trim()],
    ["q", els.txSearch.value.trim()],
  ];
  for (const [k, v] of txFields) {
    if (v) p.set(k, v);
    else p.delete(k);
  }
  if (state.stOffset > 0) p.set("st_offset", String(state.stOffset));
  else p.delete("st_offset");
  if (state.txOffset > 0) p.set("tx_offset", String(state.txOffset));
  else p.delete("tx_offset");

  // Never persist token in browser URL by default.
  p.delete("token");
  window.history.replaceState({}, "", `${url.pathname}?${p.toString()}${url.hash}`);
}

const state = {
  token: getTokenFromUrl() || localStorage.getItem("token") || "",
  role: null,
  username: null,
  action: "statements",
  stOffset: 0,
  txOffset: 0,
  stHasMore: false,
  txHasMore: false,
};

const els = {
  loginPanel: document.getElementById("login-panel"),
  appPanel: document.getElementById("app-panel"),
  tokenInput: document.getElementById("token-input"),
  loginBtn: document.getElementById("login-btn"),
  copyLinkBtn: document.getElementById("copy-link-btn"),
  loginMsg: document.getElementById("login-msg"),
  appMsg: document.getElementById("app-msg"),
  userLine: document.getElementById("user-line"),
  logoutBtn: document.getElementById("logout-btn"),
  uploadTab: document.getElementById("upload-tab"),
  statementsBody: document.querySelector("#statements-table tbody"),
  statementDetail: document.getElementById("statement-detail"),
  stPrev: document.getElementById("st-prev"),
  stNext: document.getElementById("st-next"),
  stPageInfo: document.getElementById("st-page-info"),
  txBody: document.querySelector("#transactions-table tbody"),
  refreshTransactions: document.getElementById("refresh-transactions"),
  summaryStatementId: document.getElementById("summary-statement-id"),
  refreshSummary: document.getElementById("refresh-summary"),
  summaryMsg: document.getElementById("summary-msg"),
  summaryHead: document.getElementById("summary-head"),
  summaryAccountsBody: document.querySelector("#summary-accounts-table tbody"),
  summaryCardsBody: document.querySelector("#summary-cards-table tbody"),
  txPrev: document.getElementById("tx-prev"),
  txNext: document.getElementById("tx-next"),
  txPageInfo: document.getElementById("tx-page-info"),
  txStatementId: document.getElementById("tx-statement-id"),
  txStatementProduct: document.getElementById("tx-statement-product"),
  txCard: document.getElementById("tx-card"),
  txCardholder: document.getElementById("tx-cardholder"),
  txDateFrom: document.getElementById("tx-date-from"),
  txDateTo: document.getElementById("tx-date-to"),
  txSearch: document.getElementById("tx-search"),
  uploadFile: document.getElementById("upload-file"),
  uploadBtn: document.getElementById("upload-btn"),
  uploadMsg: document.getElementById("upload-msg"),
};

els.tokenInput.value = state.token;

function setLoginMessage(msg, isError = true) {
  els.loginMsg.style.color = isError ? "#b42318" : "#027a48";
  els.loginMsg.textContent = msg;
}

function setUploadMessage(msg, isError = true) {
  els.uploadMsg.style.color = isError ? "#b42318" : "#027a48";
  els.uploadMsg.textContent = msg;
}

function setAppMessage(msg, isError = true) {
  els.appMsg.style.color = isError ? "#b42318" : "#027a48";
  els.appMsg.textContent = msg;
}

function setSummaryMessage(msg, isError = true) {
  els.summaryMsg.style.color = isError ? "#b42318" : "#027a48";
  els.summaryMsg.textContent = msg;
}

function buildLoginLinkFromInput() {
  const token = (state.token || "").trim();
  if (!token) {
    throw new Error("No active login token");
  }

  const url = new URL(window.location.href);
  url.searchParams.set("token", token);
  return url.toString();
}

async function copyLoginLink() {
  try {
    const link = buildLoginLinkFromInput();
    await copyTextWithFallback(link);
    setAppMessage("Login link copied", false);
  } catch (err) {
    setAppMessage(String(err.message || err));
  }
}

async function copyTextWithFallback(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    await navigator.clipboard.writeText(text);
    return;
  }

  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  ta.style.top = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();

  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (e) {
    copied = false;
  } finally {
    document.body.removeChild(ta);
  }

  if (copied) {
    return;
  }

  // Last fallback: manual copy dialog
  window.prompt("Copy this login link:", text);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) {
    headers["Authorization"] = `Bearer ${state.token}`;
  }

  const resp = await fetch(path, {
    ...options,
    headers,
  });

  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      detail = body.detail || detail;
    } catch (e) {
      // ignore
    }
    throw new Error(detail);
  }

  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    return await resp.json();
  }
  return resp;
}

async function login() {
  state.token = els.tokenInput.value.trim();
  localStorage.setItem("token", state.token);

  if (!state.token) {
    setLoginMessage("Token is required");
    return;
  }

  try {
    const data = await api("api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.token }),
    });
    state.role = data.role;
    state.username = data.username;

    els.userLine.textContent = `${data.username} (${data.role})`;
    els.uploadTab.classList.toggle("hidden", data.role !== "admin");

    els.loginPanel.classList.add("hidden");
    els.appPanel.classList.remove("hidden");
    setLoginMessage("", false);

    if (state.role !== "admin" && state.action === "upload") {
      state.action = "statements";
    }
    setActiveTab(state.action);
    writeViewStateToUrl();

    await Promise.all([loadStatements(), loadTransactions()]);
    if (state.action === "summary") {
      await loadSummary();
    }
  } catch (err) {
    setLoginMessage(String(err.message || err));
  }
}

function logout() {
  localStorage.removeItem("token");
  state.token = "";
  state.role = null;
  state.username = null;
  els.tokenInput.value = "";
  setAppMessage("");
  els.appPanel.classList.add("hidden");
  els.loginPanel.classList.remove("hidden");
}

function updateStatementsPager(meta) {
  const offset = parseNonNegativeInt(meta.offset, state.stOffset);
  const returned = parseNonNegativeInt(meta.returned, 0);
  const totalKnown = Number.isFinite(meta.total);
  const total = totalKnown ? Number(meta.total) : null;
  state.stHasMore = Boolean(meta.has_more);

  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const text = totalKnown
    ? `Page ${currentPage} | Offset ${offset} | ${returned} rows | Total ${total}`
    : `Page ${currentPage} | Offset ${offset} | ${returned} rows`;
  els.stPageInfo.textContent = text;
  els.stPrev.disabled = offset <= 0;
  els.stNext.disabled = !state.stHasMore;
}

async function loadStatements() {
  els.statementDetail.textContent = "";
  els.statementsBody.innerHTML = "";

  const resp = await api(`api/statements?limit=${PAGE_SIZE}&offset=${state.stOffset}`);
  const rows = Array.isArray(resp) ? resp : resp.items || [];
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.id}</td>
      <td>${row.statement_date}</td>
      <td>${row.statement_product}</td>
      <td>${row.original_filename}</td>
      <td>
        <button class="small" data-action="viewraw" data-id="${row.id}">View Raw</button>
        <button class="small" data-action="viewtx" data-id="${row.id}">View Tx</button>
        <button class="small" data-action="viewsummary" data-id="${row.id}">View Summary</button>
        <button class="small" data-action="pdf" data-id="${row.id}">PDF</button>
      </td>
    `;
    els.statementsBody.appendChild(tr);
  }

  els.statementsBody.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-id");
      const action = btn.getAttribute("data-action");
      if (action === "viewraw") {
        const detail = await api(`api/statements/${id}`);
        els.statementDetail.textContent = JSON.stringify(detail, null, 2);
      } else if (action === "viewtx") {
        state.txOffset = 0;
        els.txStatementId.value = id;
        els.txStatementProduct.value = "";
        els.txCard.value = "";
        els.txCardholder.value = "";
        els.txDateFrom.value = "";
        els.txDateTo.value = "";
        els.txSearch.value = "";
        setActiveTab("transactions");
        writeViewStateToUrl();
        await loadTransactions();
      } else if (action === "viewsummary") {
        els.summaryStatementId.value = id;
        setActiveTab("summary");
        writeViewStateToUrl();
        await loadSummary();
      } else if (action === "pdf") {
        const resp = await api(`api/statements/${id}/file`);
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        window.open(url, "_blank", "noopener,noreferrer");
      }
    });
  });

  if (Array.isArray(resp)) {
    updateStatementsPager({
      offset: state.stOffset,
      returned: rows.length,
      total: null,
      has_more: rows.length >= PAGE_SIZE,
    });
  } else {
    updateStatementsPager(resp);
  }
}

function q(v) {
  return encodeURIComponent(v || "");
}

function formatMoney(v) {
  return Number(v || 0).toFixed(2);
}

async function loadTransactions() {
  els.txBody.innerHTML = "";

  const params = [];
  if (els.txStatementId.value) params.push(`statement_id=${q(els.txStatementId.value)}`);
  if (els.txStatementProduct.value) params.push(`statement_product=${q(els.txStatementProduct.value)}`);
  if (els.txCard.value) params.push(`card_number=${q(els.txCard.value)}`);
  if (els.txCardholder.value) params.push(`cardholder_name=${q(els.txCardholder.value)}`);
  if (els.txDateFrom.value) params.push(`tx_date_from=${q(els.txDateFrom.value)}`);
  if (els.txDateTo.value) params.push(`tx_date_to=${q(els.txDateTo.value)}`);
  if (els.txSearch.value) params.push(`q=${q(els.txSearch.value)}`);
  params.push(`limit=${PAGE_SIZE}`);
  params.push(`offset=${state.txOffset}`);

  const resp = await api(`api/transactions?${params.join("&")}`);
  const rows = Array.isArray(resp) ? resp : resp.items || [];
  for (const tx of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${tx.id}</td>
      <td>${tx.statement_id}<br/>${tx.statement_product}</td>
      <td>${tx.post_date}</td>
      <td>${tx.description}</td>
      <td>${tx.account_currency || ""} ${tx.signed_amount}</td>
      <td>${tx.currency} ${tx.currency_amount}</td>
      <td>${tx.payment_method || ""}</td>
      <td>${tx.card_number}</td>
      <td>${tx.cardholder_name || ""}</td>
    `;
    els.txBody.appendChild(tr);
  }

  if (Array.isArray(resp)) {
    state.txHasMore = rows.length >= PAGE_SIZE;
    const currentPage = Math.floor(state.txOffset / PAGE_SIZE) + 1;
    els.txPageInfo.textContent = `Page ${currentPage} | Offset ${state.txOffset} | ${rows.length} rows`;
  } else {
    state.txHasMore = Boolean(resp.has_more);
    const currentPage = Math.floor(parseNonNegativeInt(resp.offset, state.txOffset) / PAGE_SIZE) + 1;
    els.txPageInfo.textContent = `Page ${currentPage} | Offset ${parseNonNegativeInt(resp.offset, state.txOffset)} | ${parseNonNegativeInt(resp.returned, rows.length)} rows`;
  }
  els.txPrev.disabled = state.txOffset <= 0;
  els.txNext.disabled = !state.txHasMore;
}

async function loadSummary() {
  const statementId = els.summaryStatementId.value.trim();
  els.summaryHead.textContent = "";
  els.summaryAccountsBody.innerHTML = "";
  els.summaryCardsBody.innerHTML = "";

  if (!statementId) {
    setSummaryMessage("Statement ID is required");
    return;
  }
  if (!/^\d+$/.test(statementId) || Number(statementId) <= 0) {
    setSummaryMessage("Statement ID must be a positive integer");
    return;
  }

  const data = await api(`api/statement_summary?statement_id=${q(statementId)}`);
  setSummaryMessage("", false);

  const headParts = [`Statement #${data.statement_id}`];
  if (data.statement_date) headParts.push(data.statement_date);
  if (data.statement_product) headParts.push(data.statement_product);
  els.summaryHead.textContent = headParts.join(" | ");

  const accounts = data.accounts || [];
  if (accounts.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="6">No visible account summary for this statement.</td>`;
    els.summaryAccountsBody.appendChild(tr);
  } else {
    for (const acc of accounts) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${acc.account_number}</td>
        <td>${acc.account_currency || ""}</td>
        <td>${formatMoney(acc.spend)}</td>
        <td>${formatMoney(acc.refund)}</td>
        <td>${formatMoney(acc.net_spend)}</td>
        <td>${formatMoney(acc.payment)}</td>
      `;
      els.summaryAccountsBody.appendChild(tr);
    }
  }

  const cards = data.cards || [];
  if (cards.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8">No visible card summary for this statement.</td>`;
    els.summaryCardsBody.appendChild(tr);
  } else {
    for (const card of cards) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${card.account_number}</td>
        <td>${card.card_number}</td>
        <td>${card.cardholder_name || ""}</td>
        <td>${card.account_currency || ""}</td>
        <td>${formatMoney(card.spend)}</td>
        <td>${formatMoney(card.refund)}</td>
        <td>${formatMoney(card.net_spend)}</td>
        <td>${formatMoney(card.payment)}</td>
      `;
      els.summaryCardsBody.appendChild(tr);
    }
  }
}

async function uploadStatement() {
  const file = els.uploadFile.files && els.uploadFile.files[0];
  if (!file) {
    setUploadMessage("Please choose a PDF file");
    return;
  }
  const form = new FormData();
  form.append("file", file);

  try {
    const data = await api("api/statements/upload", {
      method: "POST",
      body: form,
    });
    setUploadMessage(`Uploaded statement #${data.statement_id}`, false);
    await Promise.all([loadStatements(), loadTransactions()]);
  } catch (err) {
    setUploadMessage(String(err.message || err));
  }
}

function setActiveTab(action) {
  const normalized = ["statements", "transactions", "summary", "upload"].includes(action)
    ? action
    : "statements";
  state.action = normalized;

  const tabButtons = Array.from(document.querySelectorAll(".tab"));
  const tabContents = {
    statements: document.getElementById("tab-statements"),
    transactions: document.getElementById("tab-transactions"),
    summary: document.getElementById("tab-summary"),
    upload: document.getElementById("tab-upload"),
  };

  tabButtons.forEach((b) => {
    b.classList.toggle("active", b.getAttribute("data-tab") === normalized);
  });
  Object.entries(tabContents).forEach(([name, node]) => {
    node.classList.toggle("hidden", name !== normalized);
  });
}

function initTabs() {
  const tabButtons = Array.from(document.querySelectorAll(".tab"));
  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      const target = btn.getAttribute("data-tab");
      setActiveTab(target);
      writeViewStateToUrl();
    });
  }
}

els.loginBtn.addEventListener("click", login);
els.copyLinkBtn.addEventListener("click", copyLoginLink);
els.logoutBtn.addEventListener("click", logout);
els.stPrev.addEventListener("click", () => {
  state.stOffset = Math.max(0, state.stOffset - PAGE_SIZE);
  writeViewStateToUrl();
  loadStatements().catch((e) => setLoginMessage(e.message));
});
els.stNext.addEventListener("click", () => {
  if (!state.stHasMore) return;
  state.stOffset += PAGE_SIZE;
  writeViewStateToUrl();
  loadStatements().catch((e) => setLoginMessage(e.message));
});
els.txPrev.addEventListener("click", () => {
  state.txOffset = Math.max(0, state.txOffset - PAGE_SIZE);
  writeViewStateToUrl();
  loadTransactions().catch((e) => setLoginMessage(e.message));
});
els.txNext.addEventListener("click", () => {
  if (!state.txHasMore) return;
  state.txOffset += PAGE_SIZE;
  writeViewStateToUrl();
  loadTransactions().catch((e) => setLoginMessage(e.message));
});
els.refreshSummary.addEventListener("click", () => {
  state.action = "summary";
  writeViewStateToUrl();
  loadSummary().catch((e) => setSummaryMessage(e.message));
});
els.refreshTransactions.addEventListener("click", () => {
  state.action = "transactions";
  state.txOffset = 0;
  writeViewStateToUrl();
  loadTransactions().catch((e) => setLoginMessage(e.message));
});
els.uploadBtn.addEventListener("click", uploadStatement);

const initialView = readViewStateFromUrl();
state.action = initialView.action;
state.stOffset = initialView.stOffset;
state.txOffset = initialView.txOffset;
els.summaryStatementId.value = initialView.summaryStatementId;
els.txStatementId.value = initialView.txStatementId;
els.txStatementProduct.value = initialView.txStatementProduct;
els.txCard.value = initialView.txCard;
els.txCardholder.value = initialView.txCardholder;
els.txDateFrom.value = initialView.txDateFrom;
els.txDateTo.value = initialView.txDateTo;
els.txSearch.value = initialView.txSearch;

initTabs();
setActiveTab(state.action);
writeViewStateToUrl();
if (state.token) {
  login();
}
