"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const roles = ["research", "review", "rights", "script", "voice", "editor", "render", "qc", "final", "publisher"];
const roleLabels = {
  research: "RESEARCH",
  review: "REVIEW",
  rights: "RIGHTS",
  script: "SCRIPT",
  voice: "VOICE",
  editor: "EDITOR",
  render: "RENDER",
  qc: "QC",
  final: "FINAL",
  publisher: "PUBLISH"
};

const lanes = [
  {
    id: "war_history", code: "WAR-01", name: "История войн", color: "#d8a567", progress: 100,
    current: "final", state: "hold", status: "RIGHTS HOLD", review: "Technical PASS",
    thread: "01a04d82-0422-7100-aed1-cf202744cee8",
    description: "Мастер и Telegram-копия прошли FULL QC. Публикация ждёт подтверждения прав на voice reference.",
    roles: { review: "SENSITIVITY", voice: "SOURCE / TTS" }
  },
  {
    id: "celebrity_news", code: "STAR-02", name: "Новости про звёзд", color: "#9b8fba", progress: 100,
    current: "final", state: "hold", status: "FRESHNESS HOLD", review: "Technical PASS",
    thread: "01a04d82-2d2e-7213-9fb0-c9cffe6b63a6",
    description: "Мастер готов. Перед публикацией обязательны 2-часовая перепроверка новости, rights gate и checksum approval.",
    roles: { review: "PRIVACY", voice: "FISH TTS" }
  },
  {
    id: "motivation", code: "MOT-03", name: "Мотивация", color: "#b9d38b", progress: 100,
    current: "final", state: "hold", status: "2 MASTERS / HOLD", review: "Technical PASS ×2",
    thread: "01a04d82-4b0a-72f0-a0be-e0e998844148",
    description: "Два параллельных мастера прошли FULL QC. Fish TTS отключён; требуется разрешение правообладателей интервью.",
    roles: { review: "EDITORIAL", voice: "SOURCE VOICE" }, parallel: true
  },
  {
    id: "chinese_medicine", code: "TCM-04", name: "Китайская медицина", color: "#80a8b8", progress: 100,
    current: "final", state: "hold", status: "RIGHTS HOLD", review: "Technical PASS",
    thread: "01a04d82-6b31-7442-8012-eda94e698a2f",
    description: "Evidence-based разбор мифа прошёл FULL QC. До публикации нужен human/medical review и voice-rights confirmation.",
    roles: { review: "MEDICAL", voice: "FISH TTS" }
  },
  {
    id: "health", code: "HLT-05", name: "Здоровье", color: "#c97461", progress: 100,
    current: "final", state: "hold", status: "RIGHTS HOLD", review: "Technical PASS",
    thread: "01a04d82-8bc4-7240-a7c5-0d1acf0fd89f",
    description: "Мастер на русскоязычном Pixabay-видеоряде прошёл FULL QC. Нужны human/medical review и voice-rights confirmation.",
    roles: { review: "MEDICAL", voice: "FISH TTS" }
  }
];

const roleDescriptions = {
  research: ["Исследует тему и формирует проверяемый source pack.", "sources", "freshness", "evidence"],
  review: ["Профильная проверка риска до передачи текста дальше.", "risk gate", "claims", "context"],
  rights: ["Проверяет лицензию каждого клипа, изображения, голоса и музыкального трека.", "rights", "provenance", "ledger"],
  script: ["Собирает хук, смысловую дугу, текст титров и монтажный план.", "hook", "structure", "timing"],
  voice: ["Подготавливает допустимый голосовой источник для монтажной сборки.", "audio", "identity", "48 kHz"],
  editor: ["Синхронизирует кадры, музыку, субтитры и смысловые акценты.", "timeline", "captions", "mix"],
  render: ["Собирает вертикальный мастер по техническому контракту.", "1080×1920", "30 fps", "H.264"],
  qc: ["Проверяет изображение, титры, звук, технические параметры и артефакты.", "visual qc", "audio qc", "compliance"],
  final: ["Проводит независимый финальный просмотр и принимает контрольную версию.", "human gate", "sign-off", "version"],
  publisher: ["Готовит сжатую копию и отправляет только после разрешения публикации.", "delivery", "telegram", "audit"],
  core: ["Распределяет задания по пяти изолированным линиям и не обходит обязательные gates.", "scheduler", "contracts", "state"],
  vault: ["Хранит локальные исходники, лицензии, манифесты и контрольные суммы.", "local", "immutable", "sha256"],
  renderA: ["Параллельная мотивационная сборка A: один сильный персонаж и цельная мысль.", "motivation", "single speaker", "source voice"],
  renderB: ["Параллельная мотивационная сборка B: монтаж нескольких персонажей по одной теме.", "motivation", "multi speaker", "source voice"]
};

const graph = document.getElementById("network");
const edgeLayer = document.getElementById("edge-layer");
const pulseLayer = document.getElementById("pulse-layer");
const nodeLayer = document.getElementById("node-layer");
const inspector = document.getElementById("inspector");
const inspectorCode = document.getElementById("inspector-code");
const laneList = document.getElementById("lane-list");
const pipelineTable = document.getElementById("pipeline-table");
const ticker = document.getElementById("event-ticker");
const toggleButton = document.getElementById("toggle-simulation");
const clock = document.getElementById("clock");
const eventStream = document.getElementById("event-stream");
const artifactList = document.getElementById("artifact-list");
const qualityVector = document.getElementById("quality-vector");

let selectedNodeId = "core";
let paused = false;
let tickerIndex = 0;
let pulseCounter = 0;
const nodeMeta = new Map();
const edges = [];

const runEvents = [
  { time: "00:00", agent: "SCOUT-03", color: "#80a8b8", copy: "Кандидат зафиксирован · hash и диапазон источника записаны" },
  { time: "00:04", agent: "RIGHTS-07", color: "#d8a567", copy: "Интервью помечено permission_required · публикация закрыта" },
  { time: "00:07", agent: "SCRIPT-12", color: "#b9d38b", copy: "Хук уплотнён до первых 2 секунд · смысловой порядок сохранён" },
  { time: "00:11", agent: "EDIT-A", color: "#b9d38b", copy: "Паузы сокращены · микротитры 1–3 слова синхронизированы" },
  { time: "00:13", agent: "EDIT-B", color: "#d8a567", copy: "Параллельная камера получила semantic punch-ins" },
  { time: "00:18", agent: "MIX-05", color: "#9b8fba", copy: "Voice carve записан · bed освобождён в речевом диапазоне" },
  { time: "00:22", agent: "FACT-02", color: "#c97461", copy: "Celebrity claim перепроверен по РБК / Интерфакс / SAY" },
  { time: "00:26", agent: "ORIG-04", color: "#80a8b8", copy: "Добавлен редакционный контекст · low-value repost gate пройден" }
];

const runArtifacts = [
  { index: "01", name: "Цискаридзе / Уважение", meta: "RU · SOURCE VOICE · 28.8 SEC", state: "QC PASS / HOLD", progress: 100, color: "#b9d38b" },
  { index: "02", name: "Лебедев / 5–10 лет", meta: "RU · SOURCE VOICE · 29.3 SEC", state: "QC PASS / HOLD", progress: 100, color: "#d8a567" },
  { index: "03", name: "Ye / Петербург", meta: "RU · FISH 2/2 · 23.1 SEC", state: "QC PASS / HOLD", progress: 100, color: "#9b8fba" },
  { index: "04", name: "Мидуэй / Водная радиоигра", meta: "RU · FISH 1/2 · 35.2 SEC", state: "QC PASS / HOLD", progress: 100, color: "#d8a567" },
  { index: "05", name: "Банки / Миф о токсинах", meta: "RU · FISH 1/2 · 30.0 SEC", state: "QC PASS / HOLD", progress: 100, color: "#80a8b8" },
  { index: "06", name: "Мытьё рук / Температура", meta: "RU · FISH 2/2 · 27.5 SEC", state: "QC PASS / HOLD", progress: 100, color: "#c97461" }
];

const runQuality = [
  { label: "HOOK ≤2S", value: 94, color: "#b9d38b" },
  { label: "CAPTIONS", value: 91, color: "#80a8b8" },
  { label: "VOICE / MIX", value: 93, color: "#9b8fba" },
  { label: "FACT LOCK", value: 96, color: "#b9d38b" },
  { label: "ORIGINALITY", value: 86, color: "#d8a567" },
  { label: "RIGHTS", value: 67, color: "#c97461" }
];

function svgElement(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, String(value)));
  return el;
}

function roleIndex(role) { return roles.indexOf(role); }

function stateForRole(lane, role) {
  const index = roleIndex(role);
  const currentIndex = roleIndex(lane.current);
  if (index < currentIndex) return "done";
  if (index === currentIndex) return lane.state === "hold" ? "gate" : "active";
  return ["review", "rights", "qc", "final"].includes(role) ? "gate" : "ready";
}

function addPath(id, d, className = "edge", active = false) {
  const path = svgElement("path", { id, d, class: `${className}${active ? " active" : ""}` });
  edgeLayer.appendChild(path);
  edges.push({ id, active });
  return path;
}

function makeNode({ id, x, y, width = 112, height = 44, label, sub, color, state = "ready", laneId = null, role = "core", description = null }) {
  const g = svgElement("g", {
    id: `node-${id}`,
    class: `network-node is-${state}`,
    transform: `translate(${x} ${y})`,
    role: "button",
    tabindex: "0",
    "aria-label": `${label}, ${sub}`,
    style: `--node-color:${color}`
  });
  const rect = svgElement("rect", { class: "node-rect", width, height, rx: 2 });
  const dot = svgElement("circle", { class: `node-dot${state === "active" ? " is-live" : ""}`, cx: 12, cy: 13, r: 3 });
  const text = svgElement("text", { class: "node-label", x: 21, y: 17 });
  text.textContent = label;
  const subText = svgElement("text", { class: "node-sub", x: 12, y: 33 });
  subText.textContent = sub;
  g.append(rect, dot, text, subText);
  g.addEventListener("click", () => selectNode(id));
  g.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectNode(id);
    }
  });
  nodeLayer.appendChild(g);
  nodeMeta.set(id, { id, label, sub, color, state, laneId, role, description });
  return g;
}

function buildGraph() {
  edgeLayer.innerHTML = "";
  pulseLayer.innerHTML = "";
  nodeLayer.innerHTML = "";
  nodeMeta.clear();
  edges.length = 0;

  makeNode({ id: "core", x: 16, y: 376, width: 132, height: 56, label: "ORCHESTRATOR", sub: "CORE / SCHEDULER", color: "#e8e6df", state: "active", role: "core" });
  makeNode({ id: "vault", x: 1426, y: 18, width: 146, height: 50, label: "ASSET VAULT", sub: "LOCAL / IMMUTABLE", color: "#80a8b8", state: "ready", role: "vault" });

  lanes.forEach((lane, laneIndex) => {
    const y = 96 + laneIndex * 143;
    const guide = svgElement("line", { class: "row-guide", x1: 165, y1: y + 22, x2: 1572, y2: y + 22 });
    edgeLayer.appendChild(guide);
    const laneText = svgElement("text", { class: "lane-divider", x: 170, y: y - 11 });
    laneText.textContent = `${lane.code} / ${lane.name.toUpperCase()}`;
    nodeLayer.appendChild(laneText);

    const startX = 178;
    const step = 137;
    const nodeW = 112;
    const nodeH = 44;
    const coreD = `M148 404 C160 ${y + 22}, 162 ${y + 22}, ${startX} ${y + 22}`;
    addPath(`edge-core-${lane.id}`, coreD, "edge", lane.current === "research");

    roles.forEach((role, index) => {
      if (lane.parallel && role === "render") return;
      const x = startX + index * step;
      const label = lane.roles[role] || roleLabels[role];
      const state = stateForRole(lane, role);
      makeNode({
        id: `${lane.id}-${role}`,
        x, y, width: nodeW, height: nodeH,
        label,
        sub: state === "active" ? "PROCESSING" : state === "gate" ? "CONTROL GATE" : state === "done" ? "COMPLETE" : "READY",
        color: lane.color, state, laneId: lane.id, role
      });
      if (index > 0 && !(lane.parallel && role === "qc")) {
        const prevRole = roles[index - 1];
        const prevX = startX + (index - 1) * step;
        addPath(
          `edge-${lane.id}-${prevRole}-${role}`,
          `M${prevX + nodeW} ${y + 22} H${x}`,
          ["review", "rights", "qc", "final"].includes(role) ? "edge gate-edge" : "edge",
          state === "active"
        );
      }
    });

    if (lane.parallel) {
      const editorIndex = roleIndex("editor");
      const renderIndex = roleIndex("render");
      const qcIndex = roleIndex("qc");
      const editorX = startX + editorIndex * step;
      const renderX = startX + renderIndex * step;
      const qcX = startX + qcIndex * step;
      makeNode({ id: `${lane.id}-renderA`, x: renderX, y: y - 30, width: nodeW, height: nodeH, label: "RENDER-A", sub: "SOLO / 76%", color: lane.color, state: "active", laneId: lane.id, role: "renderA" });
      makeNode({ id: `${lane.id}-renderB`, x: renderX, y: y + 30, width: nodeW, height: nodeH, label: "RENDER-B", sub: "MONTAGE / 63%", color: "#d8a567", state: "active", laneId: lane.id, role: "renderB" });
      const fork = svgElement("text", { class: "fork-label", x: renderX + 56, y: y - 39, "text-anchor": "middle" });
      fork.textContent = "PARALLEL ×2";
      nodeLayer.appendChild(fork);
      addPath(`edge-${lane.id}-editor-renderA`, `M${editorX + nodeW} ${y + 22} C${editorX + 126} ${y + 22}, ${renderX - 16} ${y - 8}, ${renderX} ${y - 8}`, "edge", true);
      addPath(`edge-${lane.id}-editor-renderB`, `M${editorX + nodeW} ${y + 22} C${editorX + 126} ${y + 22}, ${renderX - 16} ${y + 52}, ${renderX} ${y + 52}`, "edge", true);
      addPath(`edge-${lane.id}-renderA-qc`, `M${renderX + nodeW} ${y - 8} C${renderX + 126} ${y - 8}, ${qcX - 16} ${y + 22}, ${qcX} ${y + 22}`, "edge", true);
      addPath(`edge-${lane.id}-renderB-qc`, `M${renderX + nodeW} ${y + 52} C${renderX + 126} ${y + 52}, ${qcX - 16} ${y + 22}, ${qcX} ${y + 22}`, "edge", true);
    }

    const voiceIndex = roleIndex("voice");
    const voiceX = startX + voiceIndex * step;
    addPath(`edge-vault-${lane.id}`, `M1426 43 C1370 43, ${voiceX + 56} ${y - 34}, ${voiceX + 56} ${y}`, "edge");
  });

  edges.filter((_, index) => index % 4 === 0 || _.active).slice(0, 18).forEach((edge, index) => addPulse(edge.id, index));
  selectNode(selectedNodeId);
}

function addPulse(edgeId, index) {
  const palette = ["#b9d38b", "#80a8b8", "#d8a567"];
  const circle = svgElement("circle", { class: "pulse", r: index % 3 === 0 ? 3 : 2, fill: palette[index % palette.length] });
  const motion = svgElement("animateMotion", {
    dur: `${3.4 + (index % 5) * .65}s`,
    begin: `${(index % 7) * -.62}s`,
    repeatCount: "indefinite",
    rotate: "auto"
  });
  const mpath = svgElement("mpath", { href: `#${edgeId}` });
  motion.appendChild(mpath);
  circle.appendChild(motion);
  pulseLayer.appendChild(circle);
  pulseCounter += 1;
}

function selectNode(id) {
  if (!nodeMeta.has(id)) id = "core";
  selectedNodeId = id;
  document.querySelectorAll(".network-node.is-selected").forEach((node) => node.classList.remove("is-selected"));
  const element = document.getElementById(`node-${id}`);
  if (element) element.classList.add("is-selected");
  const meta = nodeMeta.get(id);
  const lane = lanes.find((item) => item.id === meta.laneId);
  const detail = roleDescriptions[meta.role] || roleDescriptions.core;
  inspectorCode.textContent = lane ? `${lane.code}/${meta.role.toUpperCase()}` : meta.id.toUpperCase();
  inspector.innerHTML = `
    <p class="inspector-name">${escapeHtml(meta.label)}</p>
    <p class="inspector-lane">${lane ? escapeHtml(lane.name) : "Factory control plane"}</p>
    <span class="inspector-state"><i></i>${escapeHtml(meta.state)}</span>
    <p class="inspector-description">${escapeHtml(meta.description || detail[0])}</p>
    <div class="inspector-tags">${detail.slice(1).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")}</div>
  `;
  if (lane) selectLane(lane.id, false);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

function buildLaneList() {
  laneList.innerHTML = lanes.map((lane) => `
    <button class="lane-button${lane.id === "motivation" ? " is-selected" : ""}" data-lane="${lane.id}" type="button" style="--lane-color:${lane.color}">
      <span class="lane-button-row">
        <span class="lane-code">${lane.code}</span>
        <span class="status-chip ${lane.state}">${lane.status}</span>
      </span>
      <span class="lane-name">${lane.name}</span>
      <span class="lane-meta">${lane.review} · ${lane.thread.slice(-8)}</span>
      <span class="lane-progress" aria-label="Демо-прогресс ${lane.progress}%"><i style="--progress:${lane.progress}%"></i></span>
    </button>
  `).join("");
  laneList.querySelectorAll(".lane-button").forEach((button) => button.addEventListener("click", () => selectLane(button.dataset.lane, true)));
}

function selectLane(laneId, focusNode) {
  laneList.querySelectorAll(".lane-button").forEach((button) => button.classList.toggle("is-selected", button.dataset.lane === laneId));
  if (focusNode) {
    const lane = lanes.find((item) => item.id === laneId);
    const role = lane.parallel ? "renderA" : lane.current;
    selectNode(`${lane.id}-${role}`);
  }
}

function buildPipelineTable() {
  const columns = ["RESEARCH", "REVIEW", "RIGHTS", "SCRIPT", "AUDIO", "EDIT", "RENDER", "QC / FINAL"];
  let html = `<div class="pipeline-head"><div>LANE</div>${columns.map((label) => `<div>${label}</div>`).join("")}</div>`;
  const displayRoles = ["research", "review", "rights", "script", "voice", "editor", "render", "qc"];
  lanes.forEach((lane) => {
    const currentIndex = roleIndex(lane.current);
    html += `<div class="pipeline-row" style="--lane-color:${lane.color}">
      <div class="pipeline-label"><i></i>${lane.code} / ${lane.name}</div>
      ${displayRoles.map((role) => {
        const index = roleIndex(role);
        let className = "pipeline-cell";
        let label = "QUEUED";
        if (index < currentIndex) { className += " done"; label = "DONE"; }
        if (index === currentIndex) { className += " current"; label = lane.state === "hold" ? "HOLD" : "ACTIVE"; }
        if (["review", "rights", "qc"].includes(role) && index >= currentIndex) className += " gate-cell";
        if (lane.parallel && role === "render" && lane.current === "render") { className += " parallel current"; label = "A + B"; }
        else if (lane.parallel && role === "render") label = "A+B DONE";
        if (lane.parallel && role === "voice") label = "SOURCE";
        return `<div class="${className}">${label}</div>`;
      }).join("")}
    </div>`;
  });
  pipelineTable.innerHTML = html;
}

function buildCommandDeck() {
  eventStream.innerHTML = runEvents.map((event) => `
    <div class="event-row" style="--event-color:${event.color}">
      <span class="event-time">${event.time}</span><i class="event-dot"></i>
      <span class="event-agent">${escapeHtml(event.agent)}</span>
      <span class="event-copy">${escapeHtml(event.copy)}</span>
    </div>
  `).join("");
  artifactList.innerHTML = runArtifacts.map((artifact) => `
    <div class="artifact-row" style="--artifact-color:${artifact.color};--artifact-progress:${artifact.progress}%">
      <span class="artifact-index">${artifact.index}</span>
      <div><span class="artifact-name">${escapeHtml(artifact.name)}</span><span class="artifact-meta">${escapeHtml(artifact.meta)}</span></div>
      <span class="artifact-state">${escapeHtml(artifact.state)}</span>
      <span class="artifact-meter"><i></i></span>
    </div>
  `).join("");
  qualityVector.innerHTML = `${runQuality.map((metric) => `
    <div class="quality-row" style="--quality:${metric.value}%;--quality-color:${metric.color}">
      <span>${escapeHtml(metric.label)}</span><span class="quality-track"><i></i></span><strong>${metric.value}</strong>
    </div>
  `).join("")}<p class="quality-note">RIGHTS остаётся жёстким gate: технически готовый ролик не равен разрешённому к публикации.</p>`;
}

const tickerEvents = [
  "ORCHESTRATOR → MOTIVATION / RENDER-A · пакет передан",
  "RIGHTS GATE → MOTIVATION · source voice подтверждён в демо",
  "MOTIVATION / EDITOR → RENDER-B · таймлайн зафиксирован",
  "CELEBRITY / PRIVACY → SCRIPT · claims map обновлён",
  "HEALTH / RESEARCH → MEDICAL · source pack передан",
  "ASSET VAULT → WAR HISTORY · контрольная сумма сверена",
  "MOTIVATION / RENDER-A → QC · master candidate ожидается",
  "MOTIVATION / RENDER-B → QC · master candidate ожидается"
];

function updateTicker() {
  if (paused) return;
  tickerIndex = (tickerIndex + 1) % tickerEvents.length;
  ticker.textContent = tickerEvents[tickerIndex];
}

function updateClock() {
  clock.textContent = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date());
}

toggleButton.addEventListener("click", () => {
  paused = !paused;
  toggleButton.setAttribute("aria-pressed", String(paused));
  toggleButton.querySelector(".control-icon").textContent = paused ? "▶" : "Ⅱ";
  toggleButton.querySelector("span:last-child").textContent = paused ? "Продолжить" : "Пауза";
  if (paused) graph.pauseAnimations(); else graph.unpauseAnimations();
});

buildLaneList();
buildPipelineTable();
buildCommandDeck();
buildGraph();
updateClock();
setInterval(updateClock, 1000);
setInterval(updateTicker, 3100);
