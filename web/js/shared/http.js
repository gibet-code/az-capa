async function extractResponseError(response, fallback = "request failed") {
  let detail = "";
  try {
    const data = await response.clone().json();
    detail = (data && (data.message || data.error)) || "";
  } catch (_) {
    try { detail = (await response.text()) || ""; } catch (_) { /* ignore */ }
  }
  detail = (detail || response.statusText || fallback).trim();
  return `${detail} (HTTP ${response.status})`;
}

async function requestResponse(url, options = {}, fallback = "request failed") {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(await extractResponseError(response, fallback));
  return response;
}

async function requestJson(url, options = {}, fallback = "request failed") {
  const response = await requestResponse(url, options, fallback);
  if (response.status === 204) return null;
  return response.json();
}

function createLatestRequest() {
  let activeController = null;
  let sequence = 0;
  return {
    begin() {
      sequence += 1;
      if (activeController) activeController.abort();
      activeController = new AbortController();
      return { id: sequence, signal: activeController.signal };
    },

    isCurrent(id) {
      return id === sequence;
    },

    finish(id) {
      if (id === sequence) activeController = null;
    },
  };
}