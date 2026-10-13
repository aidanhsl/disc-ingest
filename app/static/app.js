"use strict";

const elements = {
  form: document.querySelector("#rip-form"),
  mediaName: document.querySelector("#media-name"),
  movieYear: document.querySelector("#movie-year"),
  seasonNumber: document.querySelector("#season-number"),
  episodeNumber: document.querySelector("#episode-number"),
  yearField: document.querySelector("#year-field"),
  seasonField: document.querySelector("#season-field"),
  episodeField: document.querySelector("#episode-field"),
  nameLabel: document.querySelector("#name-label"),
  discName: document.querySelector("#disc-name"),
  discSummary: document.querySelector("#disc-summary"),
  drivePath: document.querySelector("#drive-path"),
  discVisual: document.querySelector("#disc-visual"),
  scanButton: document.querySelector("#scan-button"),
  ejectButton: document.querySelector("#eject-button"),
  ripButton: document.querySelector("#rip-button"),
  ripButtonLabel: document.querySelector("#rip-button span"),
  cancelButton: document.querySelector("#cancel-button"),
  destinationPath: document.querySelector("#destination-path"),
  titleList: document.querySelector("#title-list"),
  titlesHelp: document.querySelector("#titles-help"),
  formError: document.querySelector("#form-error"),
  operationError: document.querySelector("#operation-error"),
  headerLamp: document.querySelector("#header-lamp"),
  headerState: document.querySelector("#header-state"),
  operationIndicator: document.querySelector("#operation-indicator"),
  operationStatus: document.querySelector("#operation-status"),
  operationMessage: document.querySelector("#operation-message"),
  operationAction: document.querySelector("#operation-action"),
  progressTrack: document.querySelector("#progress-track"),
  progressBar: document.querySelector("#progress-bar"),
  progressLabel: document.querySelector("#progress-label"),
  activityLog: document.querySelector("#activity-log"),
  opDisc: document.querySelector("#op-disc"),
  opIdentify: document.querySelector("#op-identify"),
  opDestination: document.querySelector("#op-destination"),
  opRip: document.querySelector("#op-rip"),
};

let state = null;
let selectedTitleIds = new Set();
let currentDiscKey = "";
let nameWasEdited = false;
let initialScanRequested = false;
let requestInFlight = false;

function mediaType() {
  return document.querySelector('input[name="media_type"]:checked').value;
}

function setError(message = "") {
  elements.formError.textContent = message;
  elements.formError.hidden = !message;
  elements.operationError.textContent = message;
  elements.operationError.hidden = !message;
}

async function api(path, body = null) {
  const options = { method: "POST", headers: {} };
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || "The request failed.");
    error.state = payload.state;
    throw error;
  }
  return payload;
}

function cleanPathPart(value) {
  return value.replace(/[\\/\x00-\x1f\x7f]+/g, " ").replace(/\s+/g, " ").trim().replace(/^[. ]+|[. ]+$/g, "") || "Untitled";
}

function destination() {
  const root = state?.media_root || "/mnt/data";
  const name = cleanPathPart(elements.mediaName.value);
  if (mediaType() === "movie") {
    const year = elements.movieYear.value.trim();
    const libraryName = year ? `${name} (${year})` : name;
    return `${root}/Movies/${libraryName}`;
  }
  const season = String(Number(elements.seasonNumber.value || 1)).padStart(2, "0");
  return `${root}/Shows/${name}/Season ${season}`;
}

function updateFormMode(forceDefaults = false) {
  const isMovie = mediaType() === "movie";
  elements.yearField.classList.toggle("is-hidden", !isMovie);
  elements.seasonField.classList.toggle("is-hidden", isMovie);
  elements.episodeField.classList.toggle("is-hidden", isMovie);
  elements.nameLabel.textContent = isMovie ? "Movie title" : "Show name";
  elements.mediaName.placeholder = isMovie ? "Movie title" : "Show name";
  elements.ripButtonLabel.textContent = isMovie ? "Rip movie" : "Rip episodes";
  elements.titlesHelp.textContent = isMovie
    ? "The longest title is selected by default."
    : "Similar episode-length titles are selected by default.";

  if (forceDefaults && state?.disc) {
    const defaults = isMovie ? state.disc.movie_default : state.disc.show_default;
    selectedTitleIds = new Set(defaults);
  }
  renderTitles();
  updateDerivedReadouts();
}

function updateDerivedReadouts() {
  const typeLabel = mediaType() === "movie" ? "Movie" : "TV Show";
  const name = elements.mediaName.value.trim() || "Not named";
  elements.destinationPath.textContent = destination();
  elements.opIdentify.textContent = `${typeLabel} · ${name}`;
  elements.opDestination.textContent = destination();
  const ready = state?.phase === "ready" && elements.mediaName.value.trim() && selectedTitleIds.size > 0;
  elements.ripButton.disabled = !ready || requestInFlight || (mediaType() === "movie" && selectedTitleIds.size !== 1);
}

function renderTitles() {
  elements.titleList.replaceChildren();
  if (!state?.disc?.titles?.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "empty-table";
    cell.textContent = state?.phase === "scanning" ? "Reading disc titles…" : "Scan a disc to see its titles.";
    row.append(cell);
    elements.titleList.append(row);
    return;
  }

  state.disc.titles.forEach((title) => {
    const row = document.createElement("tr");
    const checked = selectedTitleIds.has(title.id);
    row.classList.toggle("is-selected", checked);

    const selectCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "title-check";
    checkbox.checked = checked;
    checkbox.setAttribute("aria-label", `Select title ${title.id}`);
    checkbox.addEventListener("change", () => {
      if (mediaType() === "movie") {
        selectedTitleIds = checkbox.checked ? new Set([title.id]) : new Set();
      } else if (checkbox.checked) {
        selectedTitleIds.add(title.id);
      } else {
        selectedTitleIds.delete(title.id);
      }
      renderTitles();
      updateDerivedReadouts();
    });
    selectCell.append(checkbox);

    const nameCell = document.createElement("td");
    nameCell.textContent = title.name ? `Title ${title.id} · ${title.name}` : `Title ${title.id}`;
    const durationCell = document.createElement("td");
    durationCell.textContent = title.duration || "Unknown";
    const sizeCell = document.createElement("td");
    sizeCell.textContent = title.size || "Unknown";
    const chaptersCell = document.createElement("td");
    chaptersCell.textContent = String(title.chapters || "—");
    row.append(selectCell, nameCell, durationCell, sizeCell, chaptersCell);
    elements.titleList.append(row);
  });
}

function phaseLabel(phase) {
  return {
    idle: "Idle",
    no_disc: "No disc",
    scanning: "Scanning",
    ready: "DVD ready",
    ripping: "Ripping",
    complete: "Complete",
    cancelled: "Cancelled",
    error: "Needs attention",
  }[phase] || "Connecting";
}

function phaseClass(phase) {
  if (["ready", "complete"].includes(phase)) return "is-ready";
  if (["scanning", "ripping"].includes(phase)) return "is-busy";
  if (["error", "cancelled"].includes(phase)) return "is-error";
  return "";
}

function renderOperationPath() {
  const hasDisc = Boolean(state?.disc);
  const isBusy = state?.phase === "ripping";
  const hasIdentity = Boolean(elements.mediaName.value.trim());
  const phaseMap = {
    disc: hasDisc ? "is-done" : state?.phase === "scanning" ? "is-active" : "",
    identify: hasDisc && hasIdentity ? "is-done" : hasDisc ? "is-active" : "",
    destination: hasDisc && hasIdentity ? "is-done" : "",
    rip: isBusy
      ? "is-active"
      : state?.phase === "complete"
        ? "is-done"
        : state?.phase === "ready" && hasIdentity && selectedTitleIds.size > 0
          ? "is-active"
          : "",
  };
  document.querySelectorAll(".operation-path li").forEach((item) => {
    item.classList.remove("is-active", "is-done");
    const className = phaseMap[item.dataset.op];
    if (className) item.classList.add(className);
  });
  document.querySelectorAll(".signal-stage").forEach((stage) => {
    stage.classList.remove("is-active", "is-done");
    const className = phaseMap[stage.dataset.stage];
    if (className) stage.classList.add(className);
  });
}

function renderStatus(nextState) {
  state = nextState;
  const busy = ["scanning", "ripping"].includes(state.phase);
  const label = phaseLabel(state.phase);
  const className = phaseClass(state.phase);

  elements.headerState.textContent = label;
  elements.headerLamp.className = `status-lamp ${className}`;
  elements.operationStatus.textContent = label;
  elements.operationIndicator.className = `operation-indicator ${className}`;
  elements.operationMessage.textContent = state.message;
  elements.operationAction.textContent = state.current_action || "Waiting";
  elements.progressBar.style.transform = `scaleX(${(state.progress || 0) / 100})`;
  elements.progressLabel.textContent = `${state.progress || 0}%`;
  elements.progressTrack.setAttribute("aria-valuenow", String(state.progress || 0));
  elements.drivePath.textContent = state.drive || "/dev/sr0";
  elements.scanButton.disabled = busy || requestInFlight;
  elements.ejectButton.disabled = busy || requestInFlight;
  elements.cancelButton.hidden = !busy;
  elements.discVisual.classList.toggle("is-spinning", busy);

  if (state.disc) {
    const key = `${state.disc.name}:${state.disc.titles.map((title) => title.id).join(",")}`;
    elements.discName.textContent = state.disc.name;
    elements.discSummary.textContent = `${state.disc.titles.length} title${state.disc.titles.length === 1 ? "" : "s"} detected`;
    elements.opDisc.textContent = state.disc.name;
    if (key !== currentDiscKey) {
      currentDiscKey = key;
      if (!nameWasEdited) elements.mediaName.value = state.disc.name.replaceAll("_", " ");
      const defaults = mediaType() === "movie" ? state.disc.movie_default : state.disc.show_default;
      selectedTitleIds = new Set(defaults);
    }
  } else {
    elements.discName.textContent = label;
    elements.discSummary.textContent = state.message;
    elements.opDisc.textContent = label;
    currentDiscKey = "";
    selectedTitleIds = new Set();
  }

  if (state.phase === "ripping") {
    elements.opRip.textContent = `${state.progress}% · ${state.current_action}`;
  } else if (state.phase === "complete") {
    elements.opRip.textContent = "Complete";
  } else if (state.phase === "error") {
    elements.opRip.textContent = "Failed · check activity";
  } else {
    elements.opRip.textContent = "Waiting to start";
  }

  const logText = state.logs?.map((entry) => `[${entry.time}]  ${entry.message}`).join("\n") || "No activity yet.";
  const logWasAtBottom = elements.activityLog.scrollHeight - elements.activityLog.scrollTop - elements.activityLog.clientHeight < 18;
  elements.activityLog.textContent = logText;
  if (logWasAtBottom) elements.activityLog.scrollTop = elements.activityLog.scrollHeight;

  renderTitles();
  updateDerivedReadouts();
  renderOperationPath();
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Controller unavailable.");
    renderStatus(await response.json());
    if (!initialScanRequested && state.phase === "idle") {
      initialScanRequested = true;
      await runAction(() => api("/api/scan"));
    }
  } catch (error) {
    elements.headerState.textContent = "Disconnected";
    elements.headerLamp.className = "status-lamp is-error";
    elements.operationStatus.textContent = "Disconnected";
    elements.operationMessage.textContent = error.message;
  }
}

async function runAction(action) {
  if (requestInFlight) return;
  requestInFlight = true;
  setError();
  updateDerivedReadouts();
  try {
    const result = await action();
    renderStatus(result);
  } catch (error) {
    if (error.state) renderStatus(error.state);
    setError(error.message);
  } finally {
    requestInFlight = false;
    updateDerivedReadouts();
  }
}

document.querySelectorAll('input[name="media_type"]').forEach((input) => {
  input.addEventListener("change", () => updateFormMode(true));
});

[elements.mediaName, elements.movieYear, elements.seasonNumber, elements.episodeNumber].forEach((input) => {
  input.addEventListener("input", () => {
    if (input === elements.mediaName) nameWasEdited = true;
    updateDerivedReadouts();
    renderOperationPath();
  });
});

elements.scanButton.addEventListener("click", () => {
  nameWasEdited = false;
  runAction(() => api("/api/scan"));
});
elements.ejectButton.addEventListener("click", () => runAction(() => api("/api/eject")));
elements.cancelButton.addEventListener("click", () => runAction(() => api("/api/cancel")));

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const payload = {
    media_type: mediaType(),
    name: elements.mediaName.value,
    year: elements.movieYear.value,
    season: elements.seasonNumber.value,
    starting_episode: elements.episodeNumber.value,
    title_ids: [...selectedTitleIds],
  };
  runAction(() => api("/api/rip", payload));
});

updateFormMode(false);
refreshStatus();
setInterval(refreshStatus, 1200);
