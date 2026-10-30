// State & Config
const JUNCTION_ID = "A";
const API_BASE = ""; // Relative path to FastAPI backend
let lastSuccessfulPoll = Date.now();
let lastSensorEventPayload = null;
let currentEventSeq = 1500;
let pendingCommandsList = [];

// DOM Elements
const serverClockEl = document.getElementById("server-clock");
const connectionBadgeEl = document.getElementById("connection-badge");
const staleBannerEl = document.getElementById("stale-banner");
const staleSecondsEl = document.getElementById("stale-seconds");

const emergencyBannerEl = document.getElementById("emergency-banner");
const failureBannerEl = document.getElementById("failure-banner");
const failureReasonEl = document.getElementById("failure-reason-text");
const manualBannerEl = document.getElementById("manual-banner");
const manualAdminEl = document.getElementById("manual-admin-text");
const manualTimerEl = document.getElementById("manual-timer-text");

const modeBadgeEl = document.getElementById("mode-badge");
const phaseBadgeEl = document.getElementById("phase-badge");
const stepKindDisplay = document.getElementById("step-kind-display");
const stepTimerDisplay = document.getElementById("step-timer-display");
const stepStatusDisplay = document.getElementById("step-status-display");

const deviceTableBody = document.getElementById("device-table-body");
const auditLogList = document.getElementById("audit-log-list");
const clearSelectEl = document.getElementById("sim-clear-select");
const pendingCmdsSelectEl = document.getElementById("sim-pending-cmds");

// Tab switching
function switchTab(tabId) {
  document.querySelectorAll(".tab-btn").forEach(btn => btn.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(content => content.classList.remove("active"));

  event.currentTarget.classList.add("active");
  document.getElementById(tabId).classList.add("active");
}

// -------------------------------------------------------------
// Status Polling (Every 1000ms)
// -------------------------------------------------------------
async function fetchStatus() {
  try {
    const res = await fetch(`${API_BASE}/api/junctions/${JUNCTION_ID}/status`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const data = await res.json();
    lastSuccessfulPoll = Date.now();
    staleBannerEl.classList.add("hidden");
    connectionBadgeEl.textContent = "● Connected";
    connectionBadgeEl.className = "badge badge-success";

    renderStatus(data);
  } catch (err) {
    const diffSec = Math.round((Date.now() - lastSuccessfulPoll) / 1000);
    connectionBadgeEl.textContent = "● Disconnected";
    connectionBadgeEl.className = "badge badge-danger";

    if (diffSec >= 5) {
      staleBannerEl.classList.remove("hidden");
      staleSecondsEl.textContent = diffSec;
    }
  }
}

function renderStatus(data) {
  // 1. Clock & Mode
  const sTime = new Date(data.server_time);
  serverClockEl.textContent = sTime.toTimeString().split(" ")[0] + " UTC";

  modeBadgeEl.textContent = data.mode;
  modeBadgeEl.className = `badge ${getModeBadgeClass(data.mode)}`;
  phaseBadgeEl.textContent = `Phase: ${data.phase || "None"}`;

  // 2. Mode Banners
  if (data.mode === "EMERGENCY") {
    emergencyBannerEl.classList.remove("hidden");
  } else {
    emergencyBannerEl.classList.add("hidden");
  }

  if (data.failure && data.failure.active) {
    failureBannerEl.classList.remove("hidden");
    failureReasonEl.textContent = data.failure.reason || "Signal or Controller Offline";
  } else {
    failureBannerEl.classList.add("hidden");
  }

  if (data.manual && data.manual.active && !data.manual.suspended) {
    manualBannerEl.classList.remove("hidden");
    manualAdminEl.textContent = data.manual.requested_by || "Admin";
    const rem = Math.round(data.manual.seconds_remaining || 0);
    const mins = Math.floor(rem / 60);
    const secs = rem % 60;
    manualTimerEl.textContent = `${mins}:${secs < 10 ? "0" : ""}${secs}`;
  } else {
    manualBannerEl.classList.add("hidden");
  }

  // 3. Step Indicator
  stepKindDisplay.textContent = data.step.kind;
  stepStatusDisplay.textContent = data.step.status;

  if (data.step.phase_ends_at) {
    const ends = new Date(data.step.phase_ends_at).getTime();
    const srv = new Date(data.server_time).getTime();
    const remaining = Math.max(0, Math.round((ends - srv) / 1000));
    stepTimerDisplay.textContent = `${remaining}s`;
  } else {
    stepTimerDisplay.textContent = "--s";
  }

  // 4. Traffic Lamps & Queues per Direction
  ["NORTH", "SOUTH", "EAST", "WEST"].forEach(dir => {
    const actualCol = data.actual_signals[dir] || "UNKNOWN";
    const desiredCol = data.desired_signals[dir] || "RED";
    const qCount = data.queues[dir] || 0;

    // Update queue label
    const qEl = document.getElementById(`queue-${dir}`);
    if (qEl) qEl.textContent = qCount;

    // Update state text & mismatch badge
    const stateTxt = document.getElementById(`state-txt-${dir}`);
    if (stateTxt) stateTxt.textContent = actualCol;

    const mismatchEl = document.getElementById(`mismatch-${dir}`);
    if (mismatchEl) {
      if (actualCol !== desiredCol && actualCol !== "UNKNOWN") {
        mismatchEl.classList.remove("hidden");
      } else {
        mismatchEl.classList.add("hidden");
      }
    }

    // Update traffic lamps
    const lightContainer = document.getElementById(`light-${dir}`);
    if (lightContainer) {
      const redLamp = lightContainer.querySelector('.lamp[data-color="RED"]');
      const yellowLamp = lightContainer.querySelector('.lamp[data-color="YELLOW"]');
      const greenLamp = lightContainer.querySelector('.lamp[data-color="GREEN"]');

      redLamp.classList.toggle("active", actualCol === "RED");
      yellowLamp.classList.toggle("active", actualCol === "YELLOW");
      greenLamp.classList.toggle("active", actualCol === "GREEN");
    }
  });

  // 5. Update Device Table
  deviceTableBody.innerHTML = "";
  ["NORTH", "SOUTH", "EAST", "WEST"].forEach(dir => {
    const dev = data.devices[dir] || { signal_controller: "UNKNOWN", sensor: "ONLINE" };
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><strong>${dir}</strong></td>
      <td><span class="badge ${getStatusBadgeClass(dev.signal_controller)}">${dev.signal_controller}</span></td>
      <td><span class="badge ${getStatusBadgeClass(dev.sensor)}">${dev.sensor}</span></td>
      <td>${data.desired_signals[dir]}</td>
      <td><strong>${data.actual_signals[dir]}</strong></td>
    `;
    deviceTableBody.appendChild(tr);
  });

  // 6. Update Simulator Waiting Vehicles Dropdown
  updateWaitingVehiclesDropdown(data.waiting_vehicles || data.emergency.vehicles || [], data.queues);

  // 7. Update Pending Commands for Simulator
  pendingCommandsList = data.pending_commands || [];
  updatePendingCommandsDropdown(pendingCommandsList);
}

function getModeBadgeClass(mode) {
  switch (mode) {
    case "EMERGENCY": return "badge-danger";
    case "FAILURE": return "badge-danger";
    case "MANUAL": return "badge-primary";
    case "DEGRADED": return "badge-warning";
    default: return "badge-success";
  }
}

function getStatusBadgeClass(status) {
  switch (status) {
    case "ONLINE": return "badge-success";
    case "DEGRADED": return "badge-warning";
    case "OFFLINE": return "badge-danger";
    default: return "badge-secondary";
  }
}

function updateWaitingVehiclesDropdown(vehicleList, queues) {
  const currentSelected = clearSelectEl.value;
  clearSelectEl.innerHTML = "";
  const addedDirs = new Set();

  // 1. Authoritative waiting vehicles from backend
  if (vehicleList && vehicleList.length > 0) {
    vehicleList.forEach(v => {
      addedDirs.add(v.direction);
      const opt = document.createElement("option");
      opt.value = JSON.stringify({ vehicle_id: v.vehicle_id, direction: v.direction });
      let icon = "🚗";
      if (v.vehicle_type === "EMERGENCY") icon = "🚨";
      else if (v.vehicle_type === "TRUCK") icon = "🚚";
      const waitTxt = v.waiting_seconds !== undefined ? ` (${v.waiting_seconds}s waiting)` : "";
      opt.textContent = `${icon} ${v.vehicle_id} [${v.vehicle_type}] at ${v.direction}${waitTxt}`;
      clearSelectEl.appendChild(opt);
    });
  }

  // 2. Fallback: Any direction with active queue count > 0 if no specific vehicle mapped
  if (queues) {
    ["NORTH", "SOUTH", "EAST", "WEST"].forEach(dir => {
      const count = queues[dir] || 0;
      if (count > 0 && !addedDirs.has(dir)) {
        const opt = document.createElement("option");
        opt.value = JSON.stringify({ vehicle_id: `VH-${dir}-WAIT`, direction: dir });
        opt.textContent = `🚗 Waiting vehicle on ${dir} (${count} queued)`;
        clearSelectEl.appendChild(opt);
      }
    });
  }

  // Restore selection if still valid
  if (currentSelected && Array.from(clearSelectEl.options).some(o => o.value === currentSelected)) {
    clearSelectEl.value = currentSelected;
  }

  // Empty fallback
  if (clearSelectEl.options.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "-- No waiting vehicles in queues --";
    clearSelectEl.appendChild(opt);
  }
}

function updatePendingCommandsDropdown(cmds) {
  const currentVal = pendingCmdsSelectEl.value;
  pendingCmdsSelectEl.innerHTML = "";

  if (cmds.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "-- No pending commands --";
    pendingCmdsSelectEl.appendChild(opt);
  } else {
    cmds.forEach(c => {
      const opt = document.createElement("option");
      opt.value = c.command_id;
      opt.textContent = `${c.command_id}: ${c.direction} -> ${c.requested_state} (Attempt ${c.attempt})`;
      pendingCmdsSelectEl.appendChild(opt);
    });
  }
}

// -------------------------------------------------------------
// History Polling (Every 2000ms)
// -------------------------------------------------------------
async function fetchHistory() {
  try {
    const res = await fetch(`${API_BASE}/api/junctions/${JUNCTION_ID}/history?limit=15`);
    if (!res.ok) return;

    const data = await res.json();
    renderHistory(data.items);
  } catch (err) {
    console.error("Failed to fetch history:", err);
  }
}

function renderHistory(items) {
  if (!items || items.length === 0) {
    auditLogList.innerHTML = `<div class="audit-empty">No audit records recorded yet.</div>`;
    return;
  }

  auditLogList.innerHTML = "";
  items.forEach(item => {
    const timeStr = new Date(item.timestamp).toTimeString().split(" ")[0];
    const div = document.createElement("div");
    div.className = "audit-item";

    div.innerHTML = `
      <div class="audit-header">
        <span class="audit-type">${item.event_type}</span>
        <span class="audit-time">${timeStr}</span>
      </div>
      <div class="audit-reason">${item.reason}</div>
    `;
    auditLogList.appendChild(div);
  });
}

// -------------------------------------------------------------
// Manual Commands
// -------------------------------------------------------------
async function sendManualCommand(direction) {
  const feedback = document.getElementById("manual-feedback");
  feedback.className = "feedback-msg";
  feedback.classList.remove("hidden");
  feedback.textContent = `Requesting green for ${direction}...`;

  try {
    const res = await fetch(`${API_BASE}/api/junctions/${JUNCTION_ID}/commands`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command: "MANUAL_GREEN_REQUEST",
        direction: direction,
        requested_by: "evaluator",
      }),
    });

    const body = await res.json();
    if (!res.ok) {
      feedback.className = "feedback-msg feedback-error";
      feedback.textContent = `Rejected (${res.status}): ${body.detail || body.message}`;
    } else {
      feedback.className = "feedback-msg feedback-success";
      feedback.textContent = `Accepted: Transition to ${direction} green initiated.`;
      fetchStatus();
    }
  } catch (e) {
    feedback.className = "feedback-msg feedback-error";
    feedback.textContent = `Network error: ${e.message}`;
  }
}

async function sendReturnToAutomatic() {
  const feedback = document.getElementById("manual-feedback");
  feedback.className = "feedback-msg";
  feedback.classList.remove("hidden");
  feedback.textContent = "Returning to automatic control...";

  try {
    const res = await fetch(`${API_BASE}/api/junctions/${JUNCTION_ID}/commands`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command: "RETURN_TO_AUTOMATIC",
        requested_by: "evaluator",
      }),
    });

    const body = await res.json();
    if (!res.ok) {
      feedback.className = "feedback-msg feedback-error";
      feedback.textContent = `Error: ${body.detail || body.message}`;
    } else {
      feedback.className = "feedback-msg feedback-success";
      feedback.textContent = "Returned to Automatic Mode";
      fetchStatus();
    }
  } catch (e) {
    feedback.className = "feedback-msg feedback-error";
    feedback.textContent = `Network error: ${e.message}`;
  }
}

// -------------------------------------------------------------
// Simulator Actions
// -------------------------------------------------------------
async function simulateArrival() {
  const direction = document.getElementById("sim-direction").value;
  const vtype = document.getElementById("sim-vtype").value;
  const vid = document.getElementById("sim-vid").value || "VH-" + Math.floor(Math.random() * 900 + 100);
  const evid = document.getElementById("sim-evid").value || "evt-" + Math.floor(Math.random() * 9000 + 1000);

  currentEventSeq++;
  const payload = {
    event_id: evid,
    junction_id: JUNCTION_ID,
    direction: direction,
    event_type: "VEHICLE_ARRIVED",
    vehicle_id: vid,
    vehicle_type: vtype,
    sequence_no: currentEventSeq,
    timestamp: new Date().toISOString(),
  };

  lastSensorEventPayload = payload;
  await postSensorEvent(payload);

  // Auto-increment IDs for next click
  document.getElementById("sim-vid").value = "VH-" + Math.floor(Math.random() * 900 + 100);
  document.getElementById("sim-evid").value = "evt-" + Math.floor(Math.random() * 9000 + 1000);
}

async function repeatLastEvent() {
  if (!lastSensorEventPayload) {
    alert("Please submit at least one vehicle arrival first!");
    return;
  }
  await postSensorEvent(lastSensorEventPayload);
}

async function postSensorEvent(payload) {
  const feedback = document.getElementById("simulator-feedback");
  feedback.className = "feedback-msg";
  feedback.classList.remove("hidden");
  feedback.textContent = `Posting event ${payload.event_id}...`;

  try {
    const res = await fetch(`${API_BASE}/api/sensor-events`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const data = await res.json();
    if (!res.ok) {
      feedback.className = "feedback-msg feedback-error";
      feedback.textContent = `Failed (${res.status}): ${data.detail || data.message}`;
    } else {
      feedback.className = "feedback-msg feedback-success";
      feedback.textContent = `Outcome: ${data.outcome} | Queues: N:${data.queues.NORTH} S:${data.queues.SOUTH} E:${data.queues.EAST} W:${data.queues.WEST}`;
      fetchStatus();
      fetchHistory();
    }
  } catch (e) {
    feedback.className = "feedback-msg feedback-error";
    feedback.textContent = `Error: ${e.message}`;
  }
}

async function simulateClearance() {
  const val = clearSelectEl.value;
  if (!val) {
    alert("No vehicle selected or no vehicles currently waiting.");
    return;
  }
  const item = JSON.parse(val);
  currentEventSeq++;

  const payload = {
    event_id: "evt-clr-" + Math.floor(Math.random() * 9000 + 1000),
    junction_id: JUNCTION_ID,
    direction: item.direction,
    event_type: "VEHICLE_CLEARED",
    vehicle_id: item.vehicle_id,
    sequence_no: currentEventSeq,
    timestamp: new Date().toISOString(),
  };

  await postSensorEvent(payload);
}

async function updateSimulatorBehavior() {
  const behavior = document.getElementById("sim-behavior-select").value;
  try {
    const res = await fetch(`${API_BASE}/api/simulator/controller-behavior`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ junction_id: JUNCTION_ID, behavior: behavior }),
    });
    const data = await res.json();
    const feedback = document.getElementById("simulator-feedback");
    feedback.className = "feedback-msg feedback-success";
    feedback.classList.remove("hidden");
    feedback.textContent = `Simulator behavior updated to: ${data.behavior}`;
  } catch (e) {
    console.error(e);
  }
}

async function sendManualAck(status) {
  const cmdId = pendingCmdsSelectEl.value;
  if (!cmdId) {
    alert("No pending hardware commands to acknowledge!");
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/controller-events`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command_id: cmdId,
        junction_id: JUNCTION_ID,
        status: status,
      }),
    });
    const data = await res.json();
    const feedback = document.getElementById("simulator-feedback");
    feedback.className = "feedback-msg feedback-success";
    feedback.classList.remove("hidden");
    feedback.textContent = `Sent ${status} for ${cmdId}: ${data.message}`;
    fetchStatus();
  } catch (e) {
    console.error(e);
  }
}

async function simulateDeviceEvent() {
  const devType = document.getElementById("sim-dev-type").value;
  const devDir = document.getElementById("sim-dev-dir").value;
  const status = document.getElementById("sim-dev-status").value;

  const payload = {
    event_id: "dev-ev-" + Math.floor(Math.random() * 9000 + 1000),
    junction_id: JUNCTION_ID,
    device_type: devType,
    direction: devDir,
    status: status,
    timestamp: new Date().toISOString(),
  };

  const feedback = document.getElementById("simulator-feedback");
  feedback.className = "feedback-msg";
  feedback.classList.remove("hidden");
  feedback.textContent = `Submitting ${devType} ${status} event...`;

  try {
    const res = await fetch(`${API_BASE}/api/device-events`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    feedback.className = "feedback-msg feedback-success";
    feedback.textContent = `Device status applied: ${data.message}`;
    fetchStatus();
    fetchHistory();
  } catch (e) {
    feedback.className = "feedback-msg feedback-error";
    feedback.textContent = `Error: ${e.message}`;
  }
}

// -------------------------------------------------------------
// Init & Loops
// -------------------------------------------------------------
fetchStatus();
fetchHistory();
setInterval(fetchStatus, 1000);
setInterval(fetchHistory, 2000);
