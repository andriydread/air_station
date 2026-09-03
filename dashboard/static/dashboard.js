'use strict';

// ---------------------------------------------------------------------------
// Air Station dashboard. The browser is never told anything: every 10 s it
// asks /api/changes "what changed?" and fetches only the parts whose stamp
// moved; for 15 s after a button press it asks every second. All times from
// the server are Unix seconds; they become local time only here.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

const DASH = '—';

const metricFormats = {
  co2: (v) => v == null ? DASH : `${Math.round(v)} ppm`,
  temp: (v) => v == null ? DASH : `${v.toFixed(1)} °C`,
  humid: (v) => v == null ? DASH : `${v.toFixed(1)} %`,
  pm1: (v) => v == null ? DASH : `${v.toFixed(2)} µg/m³`,
  pm25: (v) => v == null ? DASH : `${v.toFixed(2)} µg/m³`,
  pm10: (v) => v == null ? DASH : `${v.toFixed(2)} µg/m³`,
  tps: (v) => v == null ? DASH : `${v.toFixed(2)} µm`,
  nc05: (v) => v == null ? DASH : `${v.toFixed(1)} /cm³`,
  nc1: (v) => v == null ? DASH : `${v.toFixed(1)} /cm³`,
  nc25: (v) => v == null ? DASH : `${v.toFixed(1)} /cm³`,
};

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

const two = (n) => String(n).padStart(2, '0');

function formatTimestamp(seconds) {
  if (seconds == null) return DASH;
  const date = new Date(seconds * 1000);
  return `${two(date.getHours())}:${two(date.getMinutes())} ${two(date.getDate())}.${two(date.getMonth() + 1)}.${date.getFullYear()}`;
}

function formatClock(seconds) {
  if (seconds == null) return DASH;
  const date = new Date(seconds * 1000);
  return `${two(date.getHours())}:${two(date.getMinutes())}`;
}

function formatAge(seconds) {
  if (seconds == null) return 'no data';
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function formatRelative(seconds) {
  if (seconds == null) return DASH;
  return formatAge(Math.max(0, Date.now() / 1000 - seconds));
}

function formatDuration(seconds) {
  if (seconds == null) return DASH;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

function formatMb(mb) {
  if (mb == null) return DASH;
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${Number(mb).toFixed(mb >= 100 ? 0 : 1)} MB`;
}

function prettyJson(value) {
  return JSON.stringify(value || {}, null, 2);
}

// Magnus-formula dew point — the honest "how the air feels" number.
function dewPoint(tempC, humidityPct) {
  if (tempC == null || humidityPct == null || humidityPct <= 0) return null;
  const gamma = Math.log(humidityPct / 100) + (17.62 * tempC) / (243.12 + tempC);
  return (243.12 * gamma) / (17.62 - gamma);
}

function describeDew(dew) {
  if (dew == null) return '';
  if (dew < 10) return 'dry';
  if (dew < 13) return 'comfortable';
  if (dew < 16) return 'a bit humid';
  if (dew < 18) return 'muggy';
  return 'oppressive';
}

// SPS30 "typical particle size" translated to what usually floats at that size.
function describeTps(um) {
  if (um == null) return '';
  if (um < 1) return 'ultrafine — fresh smoke, soot';
  if (um < 2.5) return 'fine — smoke, bacteria';
  if (um < 4) return 'fine dust';
  if (um < 10) return 'coarse — pollen, mold, dust';
  return 'very coarse — large dust, sand';
}

const weatherIconMap = {
  0: 'sun.png', 1: 'sun.png', 2: 'partly_cloudy.png', 3: 'cloud.png',
  45: 'fog.png', 48: 'fog.png',
  51: 'rain.png', 53: 'rain.png', 55: 'rain.png', 56: 'rain.png', 57: 'rain.png',
  61: 'rain.png', 63: 'rain.png', 65: 'rain.png', 66: 'rain.png', 67: 'rain.png',
  71: 'snow.png', 73: 'snow.png', 75: 'snow.png', 77: 'snow.png',
  80: 'rain.png', 81: 'rain.png', 82: 'rain.png', 85: 'snow.png', 86: 'snow.png',
  95: 'storm.png', 96: 'storm.png', 99: 'storm.png',
};

// ---------------------------------------------------------------------------
// Toast: the ONE place errors and confirmations surface
// ---------------------------------------------------------------------------

let toastTimer = null;

function toast(message, kind = 'info') {
  const element = document.getElementById('toast');
  element.textContent = message;
  element.className = `toast toast-${kind}`;
  element.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { element.hidden = true; }, 6000);
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function fetchJson(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    throw new Error('Station unreachable');
  }
  let data = {};
  try { data = await response.json(); } catch (_error) { data = {}; }
  if (!response.ok) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

async function submitCommand(type, payload = {}) {
  const data = await fetchJson('/api/commands', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type, payload }),
  });
  toast(`Queued for the ${data.to_whom} (#${data.id}). Result appears in Diagnostics.`, 'info');
  startBurst();
  return data;
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

let activeTab = 'live';
const TAB_NAMES = ['live', 'history', 'vitals', 'diagnostics', 'controls'];
// Each tab registers how to refresh itself; the poll loop calls the active one.
const tabRefreshers = {};

function switchTab(name, updateHash = true) {
  activeTab = name;
  document.querySelectorAll('.tab-button').forEach((button) => {
    button.classList.toggle('active', button.dataset.tab === name);
  });
  document.querySelectorAll('.tab').forEach((section) => {
    section.classList.toggle('active', section.id === `tab-${name}`);
  });
  // Deep-linkable tabs (#history): pushState makes the phone's back
  // gesture walk tabs instead of leaving the page.
  if (updateHash && location.hash !== `#${name}`) {
    history.pushState(null, '', `#${name}`);
  }
  const refresh = tabRefreshers[name];
  if (refresh) refresh({ reason: 'tab' }).catch((e) => toast(e.message, 'error'));
}

window.addEventListener('hashchange', () => {
  const name = location.hash.slice(1) || 'live';
  if (TAB_NAMES.includes(name) && name !== activeTab) switchTab(name, false);
});

// ---------------------------------------------------------------------------
// The poll loop: /api/changes every 10 s (every 1 s for 15 s after a command)
// ---------------------------------------------------------------------------

let lastChanges = null;
let lastLive = null;          // the last /api/live answer (display data + both status documents)
let serverOffset = 0;         // server "now" minus browser now, so ages do not depend on the Pi's clock vs ours
let burstUntil = 0;
let pollTimer = null;
// Hooks other sections register: called when the matching stamp moved.
const changeHooks = { events: [], commands: [], vitals: [], raw: [] };

function serverNow() {
  return Date.now() / 1000 + serverOffset;
}

function startBurst() {
  burstUntil = Date.now() + 15000;
  schedulePoll(1000);
}

function schedulePoll(delay) {
  window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(() => { poll().catch(() => {}); }, delay);
}

async function poll() {
  let changes;
  try {
    changes = await fetchJson('/api/changes');
  } catch (error) {
    renderStatusStrip(['Station unreachable']);
    schedulePoll(Date.now() < burstUntil ? 1000 : 10000);
    return;
  }
  serverOffset = changes.now - Date.now() / 1000;
  const previous = lastChanges || {};
  const moved = (key) => changes[key] !== previous[key];
  lastChanges = changes;
  const jobs = [];
  if (moved('display_data') || moved('collector_status') || moved('manager_status') || moved('last_calibration') || !lastLive) {
    jobs.push(refreshLive());
  }
  if (moved('event_id')) changeHooks.events.forEach((hook) => jobs.push(hook(changes)));
  if (moved('command_id')) changeHooks.commands.forEach((hook) => jobs.push(hook(changes)));
  if (moved('vitals_at')) changeHooks.vitals.forEach((hook) => jobs.push(hook(changes)));
  if (moved('raw_at')) changeHooks.raw.forEach((hook) => jobs.push(hook(changes)));
  await Promise.allSettled(jobs);
  renderUpdatedAge();
  schedulePoll(Date.now() < burstUntil ? 1000 : 10000);
}

// ---------------------------------------------------------------------------
// Live tab
// ---------------------------------------------------------------------------

const heroUnits = { temp: '°C', humid: '%', co2: 'ppm' };

function heroValueHtml(metric, value) {
  if (value == null) return DASH;
  const number = metric === 'co2' ? String(Math.round(value)) : value.toFixed(1);
  return `${number}<span class="unit"> ${heroUnits[metric]}</span>`;
}

// Colour by the server-sent words so thresholds can't drift from the backend;
// the first word of each scale stays uncoloured to keep the calm look.
const warnWords = new Set(['Moderate', 'Elevated', 'Unhealthy for Sensitive Groups']);

function applyBandClass(elementId, word) {
  const element = document.getElementById(elementId);
  if (!element) return;
  element.classList.remove('value-warn', 'value-bad');
  if (!word || word === 'Good') return;
  element.classList.add(warnWords.has(word) ? 'value-warn' : 'value-bad');
}

function displayDoc() {
  return lastLive?.display_data?.value || null;
}

function renderUpdatedAge() {
  // "updated 40s ago" from the manager's own stamp; amber past 90 s, red past
  // 3 min ("manager silent") — the one freshness signal the Live tab has.
  const element = document.getElementById('updated-age');
  const stamp = lastLive?.display_data?.updated_at;
  if (!stamp) {
    element.textContent = 'No display data yet';
    element.className = 'brand-sub';
    return;
  }
  const age = Math.max(0, serverNow() - stamp);
  element.textContent = `updated ${formatAge(age)}`;
  element.title = formatTimestamp(stamp);
  element.className = age > 180 ? 'brand-sub age-bad' : (age > 90 ? 'brand-sub age-warn' : 'brand-sub');
}

function renderStatusStrip(problems) {
  // All healthy -> one muted line; otherwise only the things that are wrong.
  const strip = document.getElementById('status-strip');
  strip.innerHTML = '';
  if (!problems.length) {
    strip.innerHTML = '<span class="pill">All systems ok</span>';
    return;
  }
  for (const text of problems) {
    const pill = document.createElement('span');
    pill.className = 'pill pill-bad';
    pill.textContent = text;
    strip.appendChild(pill);
  }
}

function collectorProblems(collector, doc) {
  const problems = [];
  const sensors = collector?.sensors || {};
  const bad = ['scd41', 'sht41', 'sps30'].filter((key) => sensors[key] && sensors[key].healthy === false);
  const warming = ['scd41', 'sht41', 'sps30'].filter((key) => sensors[key] && sensors[key].warmup_left > 0);
  if (doc?.collector_silent) problems.push('Collector not reporting');
  if (bad.length) problems.push(`${bad.length} sensor issue${bad.length === 1 ? '' : 's'}: ${bad.join(', ')}`);
  if (warming.length && !doc?.collector_silent) problems.push(`Warming up: ${warming.join(', ')}`);
  return problems;
}

function renderWeather(weather) {
  const grid = document.getElementById('forecast-grid');
  grid.innerHTML = '';
  const blocks = weather?.blocks || [];
  const note = document.getElementById('weather-updated');
  if (weather?.stale || !blocks.length) {
    grid.innerHTML = `<div class="empty-state">${weather?.fetched_at ? 'Forecast is stale (older than 6 h) — waiting for a fresh fetch.' : 'No forecast yet.'}</div>`;
    note.textContent = weather?.fetched_at ? `Fetched: ${formatTimestamp(weather.fetched_at)} · stale` : 'Fetched: —';
    return;
  }
  note.textContent = `Fetched: ${formatTimestamp(weather.fetched_at)}`;
  for (const block of blocks) {
    const icon = weatherIconMap[block.wmo] || 'sun.png';
    const tempText = (block.t_max != null && block.t_min != null) ? `${block.t_max} / ${block.t_min} °C` : `${DASH} / ${DASH} °C`;
    const card = document.createElement('article');
    card.className = 'forecast-card';
    card.innerHTML = `
      <p class="forecast-window">${escapeHtml(block.label)}${block.is_night ? ' · night' : ''}</p>
      <div class="forecast-body">
        <img class="forecast-icon" src="/assets/icons/${icon}" alt="">
        <div>
          <p class="forecast-stat">${escapeHtml(tempText)}</p>
          <p class="forecast-stat">Rain: ${block.rain != null ? escapeHtml(String(block.rain)) : DASH}%</p>
        </div>
      </div>`;
    grid.appendChild(card);
  }
}

function renderVersion(live) {
  const version = live?.version || {};
  const commit = document.getElementById('version-commit');
  if (version.commit && version.commit !== '-') commit.textContent = version.commit;
  const up = version.uptimes || {};
  document.getElementById('version-uptimes').textContent =
    `collector ${formatDuration(up.collector)} · manager ${formatDuration(up.manager)} · dashboard ${formatDuration(up.dashboard)}`;
}

function renderLive(live) {
  lastLive = live;
  const doc = live.display_data?.value || {};
  const values = doc.values || {};
  const collector = live.collector_status?.value || null;
  const manager = live.manager_status?.value || null;
  const managerAge = live.manager_status ? serverNow() - live.manager_status.updated_at : null;
  const collectorAge = live.collector_status ? serverNow() - live.collector_status.updated_at : null;

  document.getElementById('warming-banner').hidden = !doc.warming_up;
  for (const metric of ['co2', 'temp', 'humid', 'pm25', 'pm10', 'tps']) {
    const target = document.getElementById(`metric-${metric}`);
    const value = doc.collector_silent ? null : values[metric];
    if (heroUnits[metric]) {
      target.innerHTML = heroValueHtml(metric, value);
    } else {
      target.textContent = metricFormats[metric](value);
    }
  }
  document.getElementById('metric-tps-note').textContent = describeTps(values.tps);
  const dew = dewPoint(values.temp, values.humid);
  document.getElementById('metric-dew').textContent = dew == null ? DASH : `${dew.toFixed(1)} °C`;
  document.getElementById('metric-dew-note').textContent = describeDew(dew);
  document.getElementById('metric-aqi').textContent = doc.aqi == null || doc.collector_silent ? DASH : String(doc.aqi);
  document.getElementById('metric-aqi-label').textContent = doc.aqi_category || DASH;
  document.getElementById('metric-co2-label').textContent = doc.co2_category || DASH;
  applyBandClass('metric-aqi', doc.aqi_category);
  applyBandClass('metric-co2', doc.co2_category);

  const problems = [];
  if (!live.display_data || (managerAge != null && managerAge > 180)) problems.push('Manager not reporting');
  problems.push(...collectorProblems(collectorAge != null && collectorAge <= 90 ? collector : null, doc));
  if (collectorAge != null && collectorAge > 90 && !doc.collector_silent) problems.push('Collector not reporting');
  const wifi = manager?.wifi || {};
  if (wifi.router_ok === false) problems.push('Router unreachable');
  else if (wifi.internet_ok === false) problems.push('No internet');
  if ((manager?.power?.now || []).length) problems.push('Power issue');
  if (manager?.display?.available && manager.display.healthy === false) problems.push('E-paper error');
  renderStatusStrip(problems);

  renderWeather(doc.weather);
  renderVersion(live);
  renderUpdatedAge();

  // A pinned tab doubles as an ambient display.
  const titleCo2 = values.co2 != null && !doc.collector_silent ? `${Math.round(values.co2)} ppm` : DASH;
  const titleAqi = doc.aqi != null && !doc.collector_silent ? ` · AQI ${doc.aqi}` : '';
  document.title = `${titleCo2}${titleAqi} — Air Station`;
  document.dispatchEvent(new CustomEvent('live-updated', { detail: live }));
}

async function refreshLive() {
  renderLive(await fetchJson('/api/live'));
  if (activeTab === 'live') reloadPreview();
}

// --- Hero sparklines (last 24h, no axes — trends live in History) ----------

let sparkRows = null;
const sparklineMinSpan = { temp: 2, humid: 6, co2: 250, aqi: 25 };

async function refreshSparklines() {
  try {
    const data = await fetchJson('/api/history');
    sparkRows = data.rows || [];
  } catch (_error) {
    return; // quiet: sparklines are decoration, the loop will retry
  }
  for (const key of ['temp', 'humid', 'co2', 'aqi']) {
    renderSparkline(`spark-${key}`, sparkRows, key);
    renderHeroRange(key);
  }
  renderTodayRecap();
}

function renderTodayRecap() {
  const wrapper = document.getElementById('today-recap');
  const list = document.getElementById('today-recap-list');
  if (!wrapper || !list || !sparkRows) return;
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const todayRows = sparkRows.filter((row) => row.ts * 1000 >= midnight.getTime());
  const extreme = (key, pick) => {
    const rows = todayRows.filter((row) => row[key] != null);
    if (rows.length < 2) return null;
    return rows.reduce((best, row) => (pick(row[key], best[key]) ? row : best));
  };
  const warmest = extreme('temp', (a, b) => a > b);
  const coolest = extreme('temp', (a, b) => a < b);
  const co2Peak = extreme('co2', (a, b) => a > b);
  const lines = [];
  if (warmest && coolest) {
    lines.push(['Warmest', `${warmest.temp.toFixed(1)}° at ${formatClock(warmest.ts)}`]);
    lines.push(['Coolest', `${coolest.temp.toFixed(1)}° at ${formatClock(coolest.ts)}`]);
  }
  if (co2Peak) lines.push(['CO2 peak', `${Math.round(co2Peak.co2)} ppm at ${formatClock(co2Peak.ts)}`]);
  if (!lines.length) {
    wrapper.hidden = true;
    return;
  }
  list.innerHTML = lines.map(([label, value]) =>
    `<p><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></p>`
  ).join('');
  wrapper.hidden = false;
}

function renderSparkline(svgId, rows, key) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const points = rows.filter((row) => row[key] != null && row.ts != null);
  if (points.length < 2) {
    svg.innerHTML = '';
    return;
  }
  const width = 240;
  const height = 48;
  const pad = 3;
  const xs = points.map((row) => row.ts);
  const ys = points.map((row) => row[key]);
  const xMin = Math.min(...xs);
  const xSpan = (Math.max(...xs) - xMin) || 1;
  let yMin = Math.min(...ys);
  let yMax = Math.max(...ys);
  const minSpan = sparklineMinSpan[key] || 0;
  if (yMax - yMin < minSpan) {
    const mid = (yMax + yMin) / 2;
    yMin = mid - minSpan / 2;
    yMax = mid + minSpan / 2;
  }
  const ySpan = (yMax - yMin) || 1;
  const coords = points.map((row) => [
    pad + ((row.ts - xMin) / xSpan) * (width - 2 * pad),
    pad + (1 - ((row[key] - yMin) / ySpan)) * (height - 2 * pad),
  ]);
  const line = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const [lastX, lastY] = coords[coords.length - 1];
  svg.innerHTML = `<polyline fill="none" stroke="currentColor" stroke-width="2" points="${line}"></polyline>
    <circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="2.5" fill="currentColor"></circle>`;
}

function renderHeroRange(key) {
  const element = document.getElementById(`range-${key}`);
  if (!element || !sparkRows) return;
  const values = sparkRows.filter((row) => row[key] != null).map((row) => row[key]);
  if (values.length < 2) {
    element.textContent = '';
    return;
  }
  const digits = key === 'co2' || key === 'aqi' ? 0 : 1;
  element.textContent = `24h ${Math.min(...values).toFixed(digits)} – ${Math.max(...values).toFixed(digits)}`;
}

let previewObjectUrl = null;

async function reloadPreview() {
  const image = document.getElementById('display-preview');
  const note = document.getElementById('preview-note');
  try {
    // Stable URL on purpose: the server ETags the frame, so an unchanged
    // preview revalidates as a free 304 instead of a fresh render every poll.
    const response = await fetch('/api/display-preview.png');
    if (!response.ok) throw new Error(`preview ${response.status}`);
    const blob = await response.blob();
    if (previewObjectUrl) URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = URL.createObjectURL(blob);
    image.src = previewObjectUrl;
    image.hidden = false;
    note.hidden = true;
  } catch (_error) {
    image.hidden = true;
    note.hidden = false;
  }
}

tabRefreshers.live = async () => {
  await Promise.allSettled([refreshLive(), refreshSparklines()]);
};
changeHooks.raw.push(async () => {
  if (activeTab === 'live') await refreshSparklines();
});

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

// The toggle shows the mode a click switches TO: a moon while light, a sun while dark.
const themeIcons = {
  light: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg>',
  dark: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.2" y1="4.2" x2="5.6" y2="5.6"/><line x1="18.4" y1="18.4" x2="19.8" y2="19.8"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.2" y1="19.8" x2="5.6" y2="18.4"/><line x1="18.4" y1="5.6" x2="19.8" y2="4.2"/></svg>',
};

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { window.localStorage.setItem('airstation-theme', theme); } catch (_e) { /* private mode */ }
  const toggle = document.getElementById('theme-toggle');
  toggle.innerHTML = themeIcons[theme] || themeIcons.light;
  toggle.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  document.dispatchEvent(new CustomEvent('theme-changed'));
}

function initTheme() {
  let stored = null;
  try { stored = window.localStorage.getItem('airstation-theme'); } catch (_e) { /* private mode */ }
  setTheme(stored || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
  document.getElementById('theme-toggle').addEventListener('click', () => {
    setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
}

// ---------------------------------------------------------------------------
// Header clock (mirrors the e-paper's own header)
// ---------------------------------------------------------------------------

function startClock() {
  const timeEl = document.getElementById('clock-time');
  const dateEl = document.getElementById('clock-date');
  const tick = () => {
    const now = new Date();
    timeEl.textContent = `${two(now.getHours())}:${two(now.getMinutes())}`;
    const weekday = now.toLocaleDateString(undefined, { weekday: 'long' });
    dateEl.textContent = `${weekday} · ${two(now.getDate())}.${two(now.getMonth() + 1)}.${now.getFullYear()}`;
    renderUpdatedAge(); // the "updated 40s ago" line breathes with the clock
  };
  tick();
  window.setInterval(tick, 10000);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function installHints() {
  // Hints must work by tap too — hover doesn't exist on the primary device.
  const closeAllHints = () => {
    document.querySelectorAll('.hint.hint-open').forEach((open) => open.classList.remove('hint-open'));
  };
  document.querySelectorAll('.hint[data-hint]').forEach((hint) => {
    hint.setAttribute('aria-label', hint.dataset.hint);
    hint.addEventListener('click', (event) => {
      event.stopPropagation();
      const wasOpen = hint.classList.contains('hint-open');
      closeAllHints();
      if (!wasOpen) hint.classList.add('hint-open');
    });
  });
  document.addEventListener('click', closeAllHints);
}

function installTabs() {
  document.querySelectorAll('.tab-button').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });
}

const installers = []; // other sections push their DOMContentLoaded work here

window.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  startClock();
  installHints();
  installTabs();
  installers.forEach((install) => install());
  const initialTab = location.hash.slice(1);
  if (TAB_NAMES.includes(initialTab) && initialTab !== 'live') switchTab(initialTab, false);
  // Phones background the tab and browsers throttle timers; coming back must show now.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) schedulePoll(0);
  });
  try {
    await poll();
    if (activeTab === 'live') await refreshSparklines();
  } catch (error) {
    toast(error.message, 'error');
  }
});
