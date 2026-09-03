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
  const shown = doc.collector_silent ? {} : values;
  document.getElementById('metric-tps-note').textContent = describeTps(shown.tps);
  const dew = dewPoint(shown.temp, shown.humid);
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

// ---------------------------------------------------------------------------
// History tab: charts and statistics over a range (raw inside the retention
// window, hourly beyond it — the server decides and says which)
// ---------------------------------------------------------------------------

let range = { mode: 'preset', hours: 24 };
let lastHistory = null;
// Monotonic token: a slow 30d response landing after a fast 6h one must not
// overwrite the charts with data for a range no longer selected.
let historyRequestToken = 0;

const statsMetrics = [
  ['temp', 'Temperature, °C', 1],
  ['co2_temp', 'CO2 sensor temperature, °C', 1],
  ['humid', 'Humidity, %', 1],
  ['co2_humid', 'CO2 sensor humidity, %', 1],
  ['co2', 'CO2, ppm', 0],
  ['pm1', 'PM1, µg/m³', 2],
  ['pm25', 'PM2.5, µg/m³', 2],
  ['pm10', 'PM10, µg/m³', 2],
  ['tps', 'Particle size, µm', 2],
  ['nc05', 'Particles ≥0.5 µm, /cm³', 1],
  ['nc1', 'Particles ≥1 µm, /cm³', 1],
  ['nc25', 'Particles ≥2.5 µm, /cm³', 1],
];

function dynamicFromZero(values, minSpan) {
  const rawMax = Math.max(...values, 0);
  const paddedMax = rawMax <= 0 ? minSpan : Math.ceil((rawMax * 1.1) / minSpan) * minSpan;
  return { min: 0, max: Math.max(minSpan, paddedMax) };
}

const SECONDARY = { opacity: 0.45, width: 2 };
const TERTIARY = { opacity: 0.3, width: 2 };

// Each chart: the series it draws (first = main), the value formatter for the
// tooltip, and how to pick the y axis.
const chartConfigs = {
  'chart-temp': {
    series: [{ key: 'temp', color: '#b85c38' }, { key: 'co2_temp', color: '#b85c38', ...SECONDARY }],
    format: (row) => `${fmt(row.temp, 1)} °C · CO2 sensor ${fmt(row.co2_temp, 1)} °C`,
    bounds: (values) => {
      const min = Math.min(...values);
      const max = Math.max(...values);
      return { min: min < 0 ? Math.floor(min - 1) : 0, max: max > 40 ? Math.ceil(max + 1) : 40 };
    },
  },
  'chart-humid': {
    series: [{ key: 'humid', color: '#2b6f9e' }, { key: 'co2_humid', color: '#2b6f9e', ...SECONDARY }],
    format: (row) => `${fmt(row.humid, 1)} % · CO2 sensor ${fmt(row.co2_humid, 1)} %`,
    bounds: () => ({ min: 0, max: 100 }),
  },
  'chart-co2': {
    series: [{ key: 'co2', color: '#1f5c4a' }],
    format: (row) => `${fmt(row.co2, 0)} ppm`,
    bounds: (v) => dynamicFromZero(v, 100),
    guides: [{ at: 1000, label: 'elevated' }, { at: 2000, label: 'poor' }],
  },
  'chart-aqi': {
    series: [{ key: 'aqi', color: '#9e6f00' }],
    format: (row) => `AQI ${fmt(row.aqi, 0)}`,
    bounds: (v) => dynamicFromZero(v, 25),
    guides: [{ at: 50, label: 'good' }, { at: 100, label: 'moderate' }, { at: 150, label: 'sensitive' }, { at: 200, label: 'unhealthy' }],
  },
  'chart-pm25': {
    series: [{ key: 'pm25', color: '#5b4b8a' }],
    format: (row) => `${fmt(row.pm25, 2)} µg/m³`,
    bounds: (v) => dynamicFromZero(v, 5),
  },
  'chart-pm10': {
    series: [{ key: 'pm10', color: '#6f4a2a' }],
    format: (row) => `${fmt(row.pm10, 2)} µg/m³`,
    bounds: (v) => dynamicFromZero(v, 5),
  },
  'chart-nc': {
    series: [{ key: 'nc05', color: '#3b6e8f' }, { key: 'nc1', color: '#3b6e8f', ...SECONDARY }, { key: 'nc25', color: '#3b6e8f', ...TERTIARY }],
    format: (row) => `0.5 µm ${fmt(row.nc05, 1)} · 1 µm ${fmt(row.nc1, 1)} · 2.5 µm ${fmt(row.nc25, 1)} /cm³`,
    bounds: (v) => dynamicFromZero(v, 10),
  },
  'chart-tps': {
    series: [{ key: 'tps', color: '#7a6a3a' }],
    format: (row) => `${fmt(row.tps, 2)} µm`,
    bounds: (v) => dynamicFromZero(v, 0.5),
  },
};

function fmt(value, digits) {
  return value == null ? DASH : Number(value).toFixed(digits);
}

function rangeBounds() {
  if (range.mode === 'custom') return { from: range.from, to: range.to };
  const to = Math.floor(serverNow());
  return { from: to - range.hours * 3600, to };
}

async function refreshHistory() {
  const token = ++historyRequestToken;
  const { from, to } = rangeBounds();
  const data = await fetchJson(`/api/history?from=${from}&to=${to}`);
  if (token !== historyRequestToken) return; // superseded by a newer request
  lastHistory = data;
  renderAllCharts(data);
  renderStats(data);
  document.getElementById('export-csv').href = `/api/export.csv?from=${from}&to=${to}`;
  const note = document.getElementById('resolution-note');
  note.textContent = data.resolution === 'hourly'
    ? 'Hourly averages (min/max in the tooltip) — this range reaches beyond the 10-second rows.'
    : `10-second rows averaged per ${data.bucket_seconds >= 60 ? `${data.bucket_seconds / 60} min` : `${data.bucket_seconds} s`}.`;
}

function renderStats(data) {
  const stats = data.stats || {};
  const samples = stats.co2?.n ?? stats.temp?.n;
  document.getElementById('stats-note').textContent =
    samples != null
      ? `${samples} ${data.resolution === 'hourly' ? 'samples in hourly rows' : 'raw samples'} · ${formatTimestamp(data.from)} → ${formatTimestamp(data.to)}`
      : DASH;
  const body = document.querySelector('#stats-table tbody');
  body.innerHTML = '';
  for (const [key, label, digits] of statsMetrics) {
    const entry = stats[key] || {};
    const row = document.createElement('tr');
    row.innerHTML = `<td>${escapeHtml(label)}</td><td>${fmt(entry.min, digits)}</td><td>${fmt(entry.avg, digits)}</td><td>${fmt(entry.max, digits)}</td><td class="stats-range">${statsRangeBar(key, entry)}</td>`;
    body.appendChild(row);
  }
}

function statsRangeBar(key, entry) {
  // A one-row box-plot-lite: min→max as a slim track, a dot at the average.
  if (entry.min == null || entry.max == null || entry.avg == null) return '';
  const config = Object.values(chartConfigs).find((c) => c.series[0].key === key);
  const bounds = config ? config.bounds([entry.min, entry.max]) : { min: 0, max: Math.max(entry.max * 1.15, 1) };
  const span = bounds.max - bounds.min || 1;
  const clamp = (value) => Math.min(Math.max(value, 0), 100);
  const left = clamp(((entry.min - bounds.min) / span) * 100);
  const right = clamp(((entry.max - bounds.min) / span) * 100);
  const dot = clamp(((entry.avg - bounds.min) / span) * 100);
  return `<div class="range-track">` +
    `<div class="range-fill" style="left:${left.toFixed(1)}%;width:${Math.max(right - left, 1.5).toFixed(1)}%"></div>` +
    `<div class="range-dot" style="left:${dot.toFixed(1)}%"></div></div>`;
}

function renderAllCharts(data) {
  // The axis spans the range that was asked for, not the data that exists:
  // a 30-day view of a two-day-old station shows 28 days of honest gap.
  const options = { xMin: data.from, xMax: data.to, group: 'history' };
  for (const [svgId, config] of Object.entries(chartConfigs)) {
    renderLineChart(svgId, data.rows || [], config, data.bucket_seconds || 60, options);
  }
}

// --- SVG line chart (hand-rolled, zero dependencies) -----------------------

const chartState = new Map();

function themeColors() {
  const styles = getComputedStyle(document.documentElement);
  return {
    chartGrid: styles.getPropertyValue('--chart-grid').trim(),
    chartGridSoft: styles.getPropertyValue('--chart-grid-soft').trim(),
    chartLabel: styles.getPropertyValue('--chart-label').trim(),
    paper: styles.getPropertyValue('--paper').trim(),
  };
}

function computeTicks(min, max, count) {
  if (count <= 1) return [min];
  const step = (max - min) / (count - 1);
  return Array.from({ length: count }, (_, index) => min + step * index);
}

function formatTickLabel(tick, yRange) {
  return yRange >= 50 ? String(Math.round(tick)) : (yRange >= 5 ? tick.toFixed(1) : tick.toFixed(2));
}

function nightRects(xMin, xMax, toX, padding, height) {
  // Local 22:00–07:00 shading; only worthwhile on short ranges.
  if (xMax - xMin > 3 * 86400) return '';
  const rects = [];
  const cursor = new Date(xMin * 1000);
  cursor.setHours(0, 0, 0, 0);
  cursor.setDate(cursor.getDate() - 1);
  while (cursor.getTime() / 1000 < xMax) {
    const nightStart = new Date(cursor);
    nightStart.setHours(22, 0, 0, 0);
    const nightEnd = new Date(cursor);
    nightEnd.setDate(nightEnd.getDate() + 1);
    nightEnd.setHours(7, 0, 0, 0);
    const start = Math.max(nightStart.getTime() / 1000, xMin);
    const end = Math.min(nightEnd.getTime() / 1000, xMax);
    if (end > start) {
      rects.push(`<rect x="${toX(start)}" y="${padding.top}" width="${toX(end) - toX(start)}" height="${height - padding.top - padding.bottom}" fill="currentColor" opacity="0.05"></rect>`);
    }
    cursor.setDate(cursor.getDate() + 1);
  }
  return rects.join('');
}

// Optional shaded intervals (Vitals: throttled minutes) drawn like nights but stronger.
function shadeRects(intervals, toX, padding, height) {
  return (intervals || []).map(([start, end]) =>
    `<rect x="${toX(start)}" y="${padding.top}" width="${Math.max(toX(end) - toX(start), 2)}" height="${height - padding.top - padding.bottom}" fill="#c0392b" opacity="0.12"></rect>`
  ).join('');
}

function formatAxisTimestamp(seconds, spanSeconds) {
  const date = new Date(seconds * 1000);
  if (spanSeconds > 48 * 3600) return `${two(date.getDate())}.${two(date.getMonth() + 1)}`;
  return `${two(date.getHours())}:${two(date.getMinutes())}`;
}

function renderLineChart(svgId, rows, config, bucketSeconds, options = {}) {
  const svg = document.getElementById(svgId);
  const tooltip = document.getElementById(`tooltip-${svgId}`);
  if (!svg) return;
  const rowsWithTime = rows.filter((row) => row.ts != null);
  const mainKey = config.series[0].key;
  const anyPoints = rowsWithTime.filter((row) => config.series.some((s) => row[s.key] != null));
  const colors = themeColors();

  if (!anyPoints.length) {
    svg.innerHTML = `<text x="24" y="40" fill="${colors.chartLabel}" font-size="16">No data</text>`;
    if (tooltip) tooltip.style.opacity = '0';
    chartState.delete(svgId);
    return;
  }

  const width = 640;
  const height = 220;
  const padding = { top: 18, right: 16, bottom: 40, left: 54 };
  const values = [];
  config.series.forEach((s) => rowsWithTime.forEach((row) => { if (row[s.key] != null) values.push(row[s.key]); }));
  const yBounds = config.bounds(values);
  const xMin = options.xMin ?? Math.min(...rowsWithTime.map((row) => row.ts));
  const xMaxRaw = options.xMax ?? Math.max(...rowsWithTime.map((row) => row.ts));
  const xMax = xMaxRaw === xMin ? xMin + 1 : xMaxRaw;
  const yRange = yBounds.max - yBounds.min || 1;
  const span = xMax - xMin;
  const toX = (ts) => padding.left + ((ts - xMin) / (xMax - xMin)) * (width - padding.left - padding.right);
  const toY = (value) => padding.top + (height - padding.top - padding.bottom) * (1 - ((value - yBounds.min) / yRange));
  const gapSeconds = Math.max(bucketSeconds * 2, 120);

  const seriesSvg = config.series.map((s) => {
    const points = rowsWithTime.filter((row) => row[s.key] != null).map((row) => ({ x: toX(row.ts), y: toY(row[s.key]), row }));
    // Split into segments across data gaps: an offline stretch must render as
    // a gap, not a confident straight line bridging fabricated values.
    const segments = [];
    let current = [];
    points.forEach((point, index) => {
      if (index > 0 && point.row.ts - points[index - 1].row.ts > gapSeconds) {
        segments.push(current);
        current = [];
      }
      current.push(point);
    });
    segments.push(current);
    const widthPx = s.width || 3;
    const opacity = s.opacity ?? 1;
    return segments.map((segment) =>
      segment.length === 1
        ? `<circle cx="${segment[0].x}" cy="${segment[0].y}" r="4" fill="${s.color}" opacity="${opacity}"></circle>`
        : `<polyline fill="none" stroke="${s.color}" stroke-width="${widthPx}" opacity="${opacity}" points="${segment.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')}"></polyline>`
    ).join('');
  }).reverse().join('');  // main series drawn last, on top

  const mainPoints = rowsWithTime.filter((row) => row[mainKey] != null || config.series.some((s) => row[s.key] != null))
    .map((row) => ({ x: toX(row.ts), y: toY(row[mainKey] != null ? row[mainKey] : config.series.map((s) => row[s.key]).find((v) => v != null)), row }));

  const horizontalGrid = computeTicks(yBounds.min, yBounds.max, 5).map((tick) => {
    const y = toY(tick);
    return `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="${colors.chartGrid}" stroke-dasharray="4 4" />` +
      `<text x="8" y="${y + 4}" fill="${colors.chartLabel}" font-size="12">${formatTickLabel(tick, yRange)}</text>`;
  }).join('');
  const verticalTicks = computeTicks(xMin, xMax, 5).map((tick) => {
    const x = toX(tick);
    return `<line x1="${x}" y1="${padding.top}" x2="${x}" y2="${height - padding.bottom}" stroke="${colors.chartGridSoft}" />` +
      `<text x="${x}" y="${height - 12}" text-anchor="middle" fill="${colors.chartLabel}" font-size="12">${formatAxisTimestamp(tick, span)}</text>`;
  }).join('');
  const guides = (config.guides || [])
    .filter((guide) => guide.at > yBounds.min && guide.at < yBounds.max)
    .map((guide) => {
      const y = toY(guide.at);
      return `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="${colors.chartLabel}" stroke-width="1" stroke-dasharray="2 5" opacity="0.6" />` +
        `<text x="${width - padding.right - 4}" y="${y - 4}" text-anchor="end" fill="${colors.chartLabel}" font-size="10">${guide.at} · ${guide.label}</text>`;
    }).join('');

  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="transparent"></rect>
    <g style="color:${colors.chartLabel}">${nightRects(xMin, xMax, toX, padding, height)}</g>
    ${shadeRects(options.shade, toX, padding, height)}
    ${horizontalGrid}
    ${verticalTicks}
    ${guides}
    <line id="crosshair-${svgId}" x1="0" y1="${padding.top}" x2="0" y2="${height - padding.bottom}" stroke="${config.series[0].color}" stroke-width="1.5" stroke-dasharray="4 4" opacity="0"></line>
    <circle id="focus-${svgId}" cx="0" cy="0" r="5" fill="${config.series[0].color}" stroke="${colors.paper}" stroke-width="2" opacity="0"></circle>
    ${seriesSvg}`;

  chartState.set(svgId, { coordinates: mainPoints, format: config.format, width, height, group: options.group || 'history' });
}

function hideChartTooltip(svgId) {
  const crosshair = document.getElementById(`crosshair-${svgId}`);
  const focus = document.getElementById(`focus-${svgId}`);
  const tooltip = document.getElementById(`tooltip-${svgId}`);
  if (crosshair) crosshair.setAttribute('opacity', '0');
  if (focus) focus.setAttribute('opacity', '0');
  if (tooltip) tooltip.style.opacity = '0';
}

function hideAllChartTooltips() {
  chartState.forEach((_state, id) => hideChartTooltip(id));
}

// Charts of one group render the same rows, so one hover moves every
// crosshair to the same moment — cause-and-effect reading across metrics.
function syncCrosshairs(sourceId, ts) {
  const group = chartState.get(sourceId)?.group;
  chartState.forEach((state, id) => {
    if (id === sourceId || state.group !== group || !state.coordinates.length) return;
    let nearest = state.coordinates[0];
    for (const point of state.coordinates) {
      if (Math.abs(point.row.ts - ts) < Math.abs(nearest.row.ts - ts)) nearest = point;
    }
    const crosshair = document.getElementById(`crosshair-${id}`);
    const focus = document.getElementById(`focus-${id}`);
    if (!crosshair || !focus) return;
    crosshair.setAttribute('x1', nearest.x);
    crosshair.setAttribute('x2', nearest.x);
    crosshair.setAttribute('opacity', '1');
    focus.setAttribute('cx', nearest.x);
    focus.setAttribute('cy', nearest.y);
    focus.setAttribute('opacity', '1');
  });
}

function installChartHover(svgId) {
  const svg = document.getElementById(svgId);
  const tooltip = document.getElementById(`tooltip-${svgId}`);
  if (!svg || !tooltip) return;
  const show = (event) => {
    const state = chartState.get(svgId);
    if (!state || !state.coordinates.length) return;
    const rect = svg.getBoundingClientRect();
    const cursorX = (event.clientX - rect.left) * (state.width / rect.width);
    let nearest = state.coordinates[0];
    for (const point of state.coordinates) {
      if (Math.abs(point.x - cursorX) < Math.abs(nearest.x - cursorX)) nearest = point;
    }
    const crosshair = document.getElementById(`crosshair-${svgId}`);
    const focus = document.getElementById(`focus-${svgId}`);
    crosshair.setAttribute('x1', nearest.x);
    crosshair.setAttribute('x2', nearest.x);
    crosshair.setAttribute('opacity', '1');
    focus.setAttribute('cx', nearest.x);
    focus.setAttribute('cy', nearest.y);
    focus.setAttribute('opacity', '1');
    const extra = nearest.row.samples != null && nearest.row[`${state.mainKey}_min`] != null ? '' : '';
    tooltip.innerHTML = `<strong>${escapeHtml(state.format(nearest.row))}</strong><br>${escapeHtml(formatTimestamp(nearest.row.ts))}${extra}`;
    tooltip.style.opacity = '1';
    tooltip.style.left = `${(nearest.x / state.width) * rect.width}px`;
    tooltip.style.top = `${(nearest.y / state.height) * rect.height - 10}px`;
    syncCrosshairs(svgId, nearest.row.ts);
  };
  // Pointer events instead of mouse events: a tap pins the tooltip (there is
  // no hover on the primary device — a phone); tap-outside dismisses.
  svg.addEventListener('pointermove', (event) => { if (event.pointerType === 'mouse') show(event); });
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('mouseleave', hideAllChartTooltips);
}

document.addEventListener('pointerdown', (event) => {
  if (!event.target.closest('.chart-frame')) hideAllChartTooltips();
});

tabRefreshers.history = refreshHistory;
changeHooks.raw.push(async () => {
  if (activeTab === 'history' && range.mode === 'preset') await refreshHistory();
});
document.addEventListener('theme-changed', () => {
  if (lastHistory && activeTab === 'history') renderAllCharts(lastHistory);
});

installers.push(() => {
  Object.keys(chartConfigs).forEach(installChartHover);
  document.querySelectorAll('#range-presets button').forEach((button) => {
    button.addEventListener('click', () => {
      range = { mode: 'preset', hours: Number(button.dataset.hours) };
      document.querySelectorAll('#range-presets button').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      refreshHistory().catch((e) => toast(e.message, 'error'));
    });
  });
  document.getElementById('custom-range-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const fromValue = document.getElementById('range-from').value;
    const toValue = document.getElementById('range-to').value;
    if (!fromValue) { toast('Pick a start date first.', 'error'); return; }
    const from = Math.floor(new Date(fromValue).getTime() / 1000);
    const to = toValue ? Math.floor(new Date(toValue).getTime() / 1000) : Math.floor(serverNow());
    range = { mode: 'custom', from, to };
    document.querySelectorAll('#range-presets button').forEach((item) => item.classList.remove('active'));
    refreshHistory().catch((e) => toast(e.message, 'error'));
  });
});

// ---------------------------------------------------------------------------
// Vitals tab: the machine's health, one row a minute from the manager
// ---------------------------------------------------------------------------

let vitalsRange = { hours: 24 };
let lastVitals = null;
let vitalsRequestToken = 0;

const vitalsCharts = {
  'chart-cpu': {
    series: [{ key: 'cpu_temp', color: '#b85c38' }],
    format: (row) => `${fmt(row.cpu_temp, 1)} °C`,
    bounds: (v) => ({ min: 0, max: Math.max(80, Math.ceil(Math.max(...v) + 5)) }),
    guides: [{ at: 75, label: 'hot' }],
  },
  'chart-load': {
    series: [{ key: 'load', color: '#5b4b8a' }],
    format: (row) => `load ${fmt(row.load, 2)}`,
    bounds: (v) => dynamicFromZero(v, 1),
  },
  'chart-mem': {
    series: [{ key: 'mem_free', color: '#2b6f9e' }],
    format: (row) => `${fmt(row.mem_free, 0)} MB free`,
    bounds: (v) => dynamicFromZero(v, 100),
    guides: [{ at: 50, label: 'low' }],
  },
  'chart-disk': {
    series: [{ key: 'disk_free', color: '#1f5c4a' }, { key: 'db_size', color: '#1f5c4a', ...SECONDARY }],
    format: (row) => `${formatMb(row.disk_free)} free · database ${formatMb(row.db_size)}`,
    bounds: (v) => dynamicFromZero(v, 1000),
  },
  'chart-wifi': {
    series: [{ key: 'wifi_rssi', color: '#9e6f00' }, { key: 'wan_ms', color: '#9e6f00', ...SECONDARY }],
    format: (row) => `${fmt(row.wifi_rssi, 0)} dBm · router ${fmt(row.lan_ms, 0)} ms · internet ${fmt(row.wan_ms, 0)} ms`,
    bounds: (v) => ({ min: Math.min(-100, Math.floor(Math.min(...v) - 5)), max: Math.max(50, Math.ceil(Math.max(...v) + 10)) }),
  },
  'chart-lag': {
    series: [{ key: 'collector_lag', color: '#6f4a2a' }],
    format: (row) => `collector ${fmt(row.collector_lag, 0)} s behind`,
    bounds: (v) => dynamicFromZero(v, 30),
    guides: [{ at: 60, label: 'silent' }],
  },
};

function signalQuality(dbm) {
  if (dbm == null) return '';
  if (dbm >= -60) return 'good';
  if (dbm >= -70) return 'ok';
  if (dbm >= -80) return 'weak';
  return 'very weak';
}

function throttledIntervals(rows, bucketSeconds) {
  // Minutes with any power flag set, merged into intervals for shading.
  const intervals = [];
  let current = null;
  for (const row of rows) {
    if (row.throttled) {
      if (current && row.ts - current[1] <= bucketSeconds) {
        current[1] = row.ts + bucketSeconds;
      } else {
        current = [row.ts, row.ts + bucketSeconds];
        intervals.push(current);
      }
    }
  }
  return intervals;
}

function renderVitalsNow(latest) {
  const list = document.getElementById('vitals-now');
  if (!latest) {
    list.innerHTML = '<div class="empty-state">No vitals yet.</div>';
    return;
  }
  const power = latest.throttled_now?.length ? latest.throttled_now.join(', ') : 'ok';
  const sinceBoot = latest.throttled_since_boot?.length ? latest.throttled_since_boot.join(', ') : 'none';
  const lines = [
    ['CPU', latest.cpu_temp == null ? DASH : `${latest.cpu_temp.toFixed(1)} °C`],
    ['Load', latest.load == null ? DASH : latest.load.toFixed(2)],
    ['Memory free', latest.mem_free == null ? DASH : `${latest.mem_free} MB`],
    ['Disk free', formatMb(latest.disk_free)],
    ['Database', formatMb(latest.db_size)],
    ['Wi-Fi signal', latest.wifi_rssi == null ? DASH : `${latest.wifi_rssi} dBm · ${signalQuality(latest.wifi_rssi)}`],
    ['Link speed', latest.wifi_link == null ? DASH : `${latest.wifi_link} Mbit/s`],
    ['Latency router / internet', `${fmt(latest.lan_ms, 0)} / ${fmt(latest.wan_ms, 0)} ms`],
    ['Power now', power],
    ['Power since boot', sinceBoot],
    ['Uptime', formatDuration(latest.uptime)],
    ['Collector lag', latest.collector_lag == null ? DASH : `${latest.collector_lag} s`],
    ['Recorded', formatRelative(latest.recorded_at)],
  ];
  list.innerHTML = lines.map(([label, value]) =>
    `<p><span>${escapeHtml(label)}</span><strong${label === 'Power now' && power !== 'ok' ? ' class="health-bad"' : ''}>${escapeHtml(value)}</strong></p>`
  ).join('');
}

async function refreshVitals() {
  const token = ++vitalsRequestToken;
  const to = Math.floor(serverNow());
  const from = to - vitalsRange.hours * 3600;
  const data = await fetchJson(`/api/vitals?from=${from}&to=${to}`);
  if (token !== vitalsRequestToken) return;
  lastVitals = data;
  renderVitalsCharts(data);
  renderVitalsNow(data.latest);
}

function renderVitalsCharts(data) {
  const options = {
    xMin: data.from, xMax: data.to, group: 'vitals',
    shade: throttledIntervals(data.rows || [], data.bucket_seconds || 60),
  };
  for (const [svgId, config] of Object.entries(vitalsCharts)) {
    renderLineChart(svgId, data.rows || [], config, data.bucket_seconds || 60, options);
  }
}

tabRefreshers.vitals = refreshVitals;
changeHooks.vitals.push(async () => {
  if (activeTab === 'vitals') await refreshVitals();
});
document.addEventListener('theme-changed', () => {
  if (lastVitals && activeTab === 'vitals') renderVitalsCharts(lastVitals);
});

installers.push(() => {
  Object.keys(vitalsCharts).forEach(installChartHover);
  document.querySelectorAll('#vitals-presets button').forEach((button) => {
    button.addEventListener('click', () => {
      vitalsRange = { hours: Number(button.dataset.hours) };
      document.querySelectorAll('#vitals-presets button').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      refreshVitals().catch((e) => toast(e.message, 'error'));
    });
  });
});

// ---------------------------------------------------------------------------
// Diagnostics tab: events (from all three programs), commands, health,
// connectivity, housekeeping
// ---------------------------------------------------------------------------

let commandsCache = [];
let recentVitalsLatest = null;

function eventQuery() {
  const query = new URLSearchParams({ limit: 100 });
  for (const key of ['app', 'level', 'source']) {
    const value = document.getElementById(`event-${key}`).value;
    if (value) query.set(key, value);
  }
  return query.toString();
}

async function refreshDiagnostics() {
  const now = Math.floor(serverNow());
  const [events, commands, restarts, storage, cleans, vitals] = await Promise.all([
    fetchJson(`/api/events?${eventQuery()}`),
    fetchJson('/api/commands?limit=20'),
    fetchJson('/api/restarts?hours=24'),
    fetchJson('/api/events?source=storage&limit=30'),
    fetchJson('/api/events?source=sps30&limit=50'),
    fetchJson(`/api/vitals?from=${now - 600}&to=${now}`),
  ]);
  renderEvents(events.events || []);
  commandsCache = commands.commands || [];
  renderCommands(commandsCache);
  document.dispatchEvent(new CustomEvent('commands-updated', { detail: commandsCache }));
  renderRestartCount(restarts);
  recentVitalsLatest = vitals.latest || null;
  renderStationHealth(lastLive);
  renderConnectivity(lastLive);
  renderHousekeeping(lastLive, storage.events || [], cleans.events || []);
}

async function refreshCommandsOnly() {
  const commands = await fetchJson('/api/commands?limit=20');
  commandsCache = commands.commands || [];
  if (activeTab === 'diagnostics') renderCommands(commandsCache);
  document.dispatchEvent(new CustomEvent('commands-updated', { detail: commandsCache }));
}

function renderEvents(events) {
  const list = document.getElementById('event-list');
  list.innerHTML = '';
  if (!events.length) {
    list.innerHTML = '<div class="empty-state">No events match.</div>';
    return;
  }
  for (const event of events) {
    const item = document.createElement('article');
    item.className = 'event-item';
    const details = Object.keys(event.details || {}).length
      ? `<pre>${escapeHtml(prettyJson(event.details))}</pre>` : '';
    item.innerHTML = `
      <header>
        <span>${escapeHtml(event.app)} · ${escapeHtml(event.source)} / ${escapeHtml(event.type)}</span>
        <span class="event-level event-level-${escapeHtml(event.level)}">${escapeHtml(event.level)}</span>
      </header>
      <p>${escapeHtml(event.message)}</p>
      <p class="event-time" title="${escapeHtml(formatTimestamp(event.ts))}">${escapeHtml(formatRelative(event.ts))}</p>
      ${details}`;
    list.appendChild(item);
  }
}

function commandTiming(command) {
  // Whether the Pi actually heard the button: completed rows say how fast,
  // an old pending row says the addressed program isn't picking commands up.
  if (command.status === 'pending') {
    const waiting = serverNow() - command.created_at;
    return waiting > 15
      ? ` <span class="health-bad">· not picked up — is the ${escapeHtml(command.to_whom)} running?</span>`
      : ' · waiting for the Pi…';
  }
  if (command.status === 'running') return ' · running…';
  const took = Math.max(0, Math.round(command.updated_at - command.created_at));
  return ` · ${took}s`;
}

function renderCommands(commands) {
  const list = document.getElementById('command-list');
  list.innerHTML = '';
  if (!commands.length) {
    list.innerHTML = '<div class="empty-state">No commands yet.</div>';
    return;
  }
  for (const command of commands.slice(0, 10)) {
    const item = document.createElement('article');
    item.className = 'command-item';
    const result = command.result ? `<pre>${escapeHtml(prettyJson(command.result))}</pre>` : '';
    item.innerHTML = `
      <header>
        <span>${escapeHtml(command.type)} → ${escapeHtml(command.to_whom)}</span>
        <span class="command-status-${escapeHtml(command.status)}">${escapeHtml(command.status)}${commandTiming(command)}</span>
      </header>
      <p class="event-time" title="${escapeHtml(formatTimestamp(command.created_at))}">${escapeHtml(formatRelative(command.created_at))}</p>
      ${result}`;
    list.appendChild(item);
  }
}

function renderRestartCount(restarts) {
  // >1 start in 24 h is the watchdog-crash-loop tell; a single boot is normal.
  const element = document.getElementById('collector-restarts');
  const total = (restarts.collector || 0);
  element.textContent = `collector ${restarts.collector ?? DASH} · manager ${restarts.manager ?? DASH} · dashboard ${restarts.dashboard ?? DASH}`;
  element.className = total > 1 || (restarts.manager || 0) > 1 ? 'health-bad' : '';
}

const healthRows = [
  ['collector', 'scd41', 'SCD41 · CO2'],
  ['collector', 'sht41', 'SHT41 · temp/RH'],
  ['collector', 'sps30', 'SPS30 · particulates'],
  ['collector', 'i2c', 'I2C bus'],
  ['manager', 'display', 'E-paper'],
  ['manager', 'weather', 'Weather fetch'],
  ['manager', 'wifi', 'Wi-Fi'],
  ['manager', 'power', 'Power'],
  ['manager', 'storage', 'Storage'],
];

function healthOf(app, key, live) {
  if (app === 'collector') {
    const entry = live?.collector_status?.value?.sensors?.[key];
    if (!entry) return null;
    if (entry.available === false) return { ok: false, text: entry.last_error || 'missing' };
    if (entry.warmup_left > 0) return { ok: true, warn: true, text: `warming up · ${entry.warmup_left}s` };
    if (entry.healthy === false) return { ok: false, text: entry.last_error || 'unhealthy' };
    const id = entry.id ? ` · ${entry.id}` : '';
    const reinits = entry.reinit_count ? ` · ${entry.reinit_count} re-init${entry.reinit_count === 1 ? '' : 's'}` : '';
    return { ok: true, text: `ok${id}${reinits}` };
  }
  const manager = live?.manager_status?.value;
  if (!manager) return null;
  if (key === 'display') {
    const d = manager.display || {};
    if (!d.available) return { ok: false, text: d.last_error || 'not available' };
    return d.healthy === false ? { ok: false, text: d.last_error || 'error' } : { ok: true, text: `ok · ${d.frames ?? 0} frames` };
  }
  if (key === 'weather') {
    const w = manager.weather || {};
    if (w.ok === false) return { ok: false, text: w.error || 'fetch failed' };
    return { ok: true, text: w.fetched_at ? `ok · ${formatRelative(w.fetched_at)}` : 'not fetched yet' };
  }
  if (key === 'wifi') {
    const w = manager.wifi || {};
    if (w.router_ok === false) return { ok: false, text: `router unreachable · ${w.router_failures} probes` };
    if (w.internet_ok === false) return { ok: false, text: 'no internet' };
    return { ok: true, text: w.router_ok == null ? DASH : 'ok' };
  }
  if (key === 'power') {
    const p = manager.power || {};
    if (!p.available) return { ok: true, text: 'n/a' };
    return p.now?.length ? { ok: false, text: p.now.join(', ') } : { ok: true, text: 'ok' };
  }
  if (key === 'storage') {
    const s = manager.storage || {};
    return { ok: true, text: `${formatMb(s.db_mb)}${s.vitals_write_failures ? ` · ${s.vitals_write_failures} write failures` : ''}` };
  }
  return null;
}

function renderStationHealth(live) {
  const list = document.getElementById('sensor-health-list');
  list.innerHTML = '';
  for (const [app, key, label] of healthRows) {
    const health = healthOf(app, key, live);
    const row = document.createElement('p');
    const text = health ? health.text : DASH;
    const cls = health && !health.ok ? ' class="health-bad"' : (health?.warn ? ' class="health-warn"' : '');
    row.innerHTML = `<span>${escapeHtml(label)}</span><strong${cls}>${escapeHtml(text)}</strong>`;
    list.appendChild(row);
  }
}

function renderConnectivity(live) {
  const collector = live?.collector_status?.value || {};
  const manager = live?.manager_status?.value || {};
  const wifi = manager.wifi || {};
  const power = manager.power || {};
  const set = (id, text, bad = false) => {
    const element = document.getElementById(id);
    element.textContent = text;
    element.className = bad ? 'health-bad' : '';
  };
  set('collector-uptime', collector.uptime == null ? DASH : `${formatDuration(collector.uptime)} (since ${formatTimestamp(collector.started_at)})`);
  set('manager-uptime', manager.uptime == null ? DASH : `${formatDuration(manager.uptime)} (since ${formatTimestamp(manager.started_at)})`);
  set('network-router', wifi.router_ok == null ? DASH : (wifi.router_ok ? `reachable · ${wifi.gateway || ''}` : `unreachable · ${wifi.router_failures} probes`), wifi.router_ok === false);
  set('network-internet', wifi.internet_ok == null ? DASH : (wifi.internet_ok ? 'reachable' : `unreachable · ${wifi.wan_failures} probes`), wifi.internet_ok === false);
  const rssi = recentVitalsLatest?.wifi_rssi;
  set('network-signal', rssi == null ? DASH : `${rssi} dBm · ${signalQuality(rssi)}`);
  set('network-latency', `router ${fmt(wifi.lan_ms, 0)} ms · internet ${fmt(wifi.wan_ms, 0)} ms`);
  set('network-bounce', wifi.last_bounce_at ? `${formatRelative(wifi.last_bounce_at)} (${wifi.bounces} total)` : 'never');
  set('power-now', !power.available ? 'n/a' : (power.now?.length ? power.now.join(', ') : 'ok'), power.now?.length > 0);
  set('power-since-boot', !power.available ? 'n/a' : (power.since_boot?.length ? power.since_boot.join(', ') : 'none'));
}

function renderHousekeeping(live, storageEvents, sps30Events) {
  const storage = live?.manager_status?.value?.storage || {};
  const overdueSeconds = 26 * 3600; // nightly tasks get a 2 h grace period
  const setLine = (id, ts, canBeOverdue) => {
    const element = document.getElementById(id);
    if (!ts) {
      element.textContent = 'never';
      element.className = '';
      return;
    }
    const overdue = canBeOverdue && serverNow() - ts > overdueSeconds;
    element.textContent = formatRelative(ts) + (overdue ? ' · overdue' : '');
    element.title = formatTimestamp(ts);
    element.className = overdue ? 'health-bad' : '';
  };
  const nightly = storageEvents.find((event) => event.type === 'nightly');
  setLine('hk-backup', storage.last_backup_at || nightly?.ts || null, true);
  setLine('hk-prune', storage.last_prune_at || nightly?.ts || null, true);
  setLine('hk-rollup', storage.last_rollup_hour ? storage.last_rollup_hour + 3600 : null, false);
  const clean = sps30Events.find((event) => event.type === 'fan_clean');
  setLine('hk-clean', clean?.ts || null, false);
  document.getElementById('hk-db-size').textContent = storage.db_mb == null
    ? DASH : `${formatMb(storage.db_mb)}${storage.last_backup_mb != null ? ` · backup ${formatMb(storage.last_backup_mb)}` : ''}`;
}

// --- Custom dropdowns --------------------------------------------------------
// Native <select> popups commit on mouse-release on the operator's system,
// which made the menus unusable. The native select stays in the DOM as the
// value store — change listeners and .value reads keep working.

function upgradeSelect(select) {
  const wrapper = document.createElement('span');
  wrapper.className = 'dropdown';
  select.parentNode.insertBefore(wrapper, select);
  wrapper.appendChild(select);
  select.tabIndex = -1;
  select.style.display = 'none';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'dropdown-toggle';
  toggle.setAttribute('aria-haspopup', 'listbox');
  toggle.setAttribute('aria-expanded', 'false');
  const menu = document.createElement('ul');
  menu.className = 'dropdown-menu';
  menu.setAttribute('role', 'listbox');
  menu.hidden = true;
  const labelOf = (option) => option.textContent.trim() || option.value;
  const syncToggle = () => {
    const selected = select.options[select.selectedIndex];
    toggle.textContent = selected ? labelOf(selected) : DASH;
  };
  let focusIndex = -1;
  const highlight = () => {
    menu.querySelectorAll('li').forEach((item, index) => item.classList.toggle('focused', index === focusIndex));
  };
  const close = () => {
    menu.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    focusIndex = -1;
  };
  const choose = (index) => {
    select.selectedIndex = index;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    syncToggle();
    close();
  };
  const open = () => {
    menu.innerHTML = '';
    Array.from(select.options).forEach((option, index) => {
      const item = document.createElement('li');
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', index === select.selectedIndex ? 'true' : 'false');
      item.textContent = labelOf(option);
      item.addEventListener('mousedown', (event) => { event.preventDefault(); choose(index); });
      menu.appendChild(item);
    });
    menu.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    focusIndex = select.selectedIndex;
    highlight();
  };
  toggle.addEventListener('click', () => (menu.hidden ? open() : close()));
  toggle.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { close(); return; }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (menu.hidden) { open(); return; }
      focusIndex = Math.min(Math.max(focusIndex + (event.key === 'ArrowDown' ? 1 : -1), 0), select.options.length - 1);
      highlight();
      return;
    }
    if (event.key === 'Enter' && !menu.hidden) {
      event.preventDefault();
      if (focusIndex >= 0) choose(focusIndex);
    }
  });
  toggle.addEventListener('blur', (event) => { if (!wrapper.contains(event.relatedTarget)) close(); });
  document.addEventListener('click', (event) => { if (!menu.hidden && !wrapper.contains(event.target)) close(); });
  wrapper.appendChild(toggle);
  wrapper.appendChild(menu);
  syncToggle();
}

tabRefreshers.diagnostics = refreshDiagnostics;
changeHooks.events.push(async () => {
  if (activeTab === 'diagnostics') await refreshDiagnostics();
});
changeHooks.commands.push(refreshCommandsOnly);
document.addEventListener('live-updated', () => {
  if (activeTab === 'diagnostics') {
    renderStationHealth(lastLive);
    renderConnectivity(lastLive);
  }
});

installers.push(() => {
  ['event-app', 'event-level', 'event-source'].forEach((id) => {
    const select = document.getElementById(id);
    upgradeSelect(select);
    select.addEventListener('change', () => refreshDiagnostics().catch((e) => toast(e.message, 'error')));
  });
  document.getElementById('event-refresh').addEventListener('click', () => {
    refreshDiagnostics().catch((e) => toast(e.message, 'error'));
  });
});

// ---------------------------------------------------------------------------
// Controls tab: six buttons and the calibration checklist
// ---------------------------------------------------------------------------

// The collector enforces these (collector/sensors.py CAL_*); mirrored here so
// the button unlocks exactly when a command would pass.
const calibrationLimits = { min_runtime: 180, min_samples: 3, max_spread: 30, max_delta: 200 };
const pendingCommandTypes = new Set();

function renderControls(live) {
  const collector = live?.collector_status?.value || {};
  const manager = live?.manager_status?.value || {};
  const calibration = live?.last_calibration?.value || null;
  document.getElementById('scd41-asc-state').textContent = collector.asc == null ? DASH : (collector.asc ? 'on (config.toml)' : 'off (config.toml)');
  document.getElementById('scd41-last-calibration').textContent = calibration
    ? `${formatTimestamp(calibration.at)} · target ${calibration.target_ppm} ppm · correction ${calibration.correction_ppm} ppm`
    : 'never';
  document.getElementById('scd41-pressure').textContent = collector.pressure_hpa == null
    ? 'altitude only (no weather pressure yet)' : `${collector.pressure_hpa} hPa from the weather`;
  document.getElementById('sps30-firmware').textContent = collector.sensors?.sps30?.id || DASH;
  document.getElementById('database-size').textContent = formatMb(manager.storage?.db_mb);
  document.getElementById('database-free').textContent = formatMb(recentVitalsLatest?.disk_free);
  document.getElementById('database-newest').textContent = lastChanges?.raw_at ? formatRelative(lastChanges.raw_at) : DASH;
  renderCalibrationChecklist();
}

function renderCommandNotes(commands) {
  pendingCommandTypes.clear();
  for (const command of commands) {
    if (command.status !== 'pending' && command.status !== 'running') continue;
    if (serverNow() - command.created_at <= 120) pendingCommandTypes.add(command.type);
  }
  document.querySelectorAll('[data-command-note]').forEach((note) => {
    const type = note.dataset.commandNote;
    const button = document.querySelector(`[data-command="${type}"]`);
    if (pendingCommandTypes.has(type)) {
      note.textContent = 'running…';
      note.className = 'command-note';
      if (button && type !== 'scd41_calibrate') button.disabled = true;
      return;
    }
    if (button && type !== 'scd41_calibrate') button.disabled = false;
    const latest = commands.find((command) => command.type === type);
    if (!latest) {
      note.textContent = '';
      return;
    }
    const error = latest.status === 'fail' && latest.result?.error ? ` — ${latest.result.error}` : '';
    note.textContent = `Last run: ${formatRelative(latest.updated_at)} · ${latest.status}${error}`;
    note.className = latest.status === 'fail' ? 'command-note health-bad' : 'command-note';
    note.title = formatTimestamp(latest.updated_at);
  });
  renderCalibrationChecklist();
}

function renderCalibrationChecklist() {
  const box = document.getElementById('scd41-checklist');
  const submit = document.getElementById('scd41-calibration-submit');
  const cal = lastLive?.collector_status?.value?.calibration;
  const pending = pendingCommandTypes.has('scd41_calibrate');
  if (!cal) {
    box.innerHTML = '<p class="cal-check cal-wait">• Waiting for the collector…</p>';
    submit.disabled = true;
    return;
  }
  const limits = calibrationLimits;
  const target = Number(document.getElementById('target-co2').value) || 420;
  const driftOverride = document.getElementById('scd41-calibration-drift').checked;
  const confirmed = document.getElementById('scd41-calibration-confirm').checked;
  const delta = cal.average_co2 == null ? null : Math.abs(cal.average_co2 - target);
  const checks = [
    { ok: cal.runtime_seconds >= limits.min_runtime, text: `Warmed up — running ${cal.runtime_seconds}s of ${limits.min_runtime}s` },
    { ok: cal.sample_count >= limits.min_samples, text: `Enough readings — ${cal.sample_count} of ${limits.min_samples} in the last 5 min` },
    { ok: cal.spread_co2 != null && cal.spread_co2 <= limits.max_spread,
      text: cal.spread_co2 == null ? 'Stable readings — no data yet' : `Stable readings — spread ${cal.spread_co2} ppm (limit ${limits.max_spread})` },
    driftOverride
      ? { ok: true, text: 'Near target — skipped (drift override)' }
      : { ok: delta != null && delta <= limits.max_delta,
          text: delta == null ? `Near ${target} ppm — no data yet` : `Near ${target} ppm — sensor reads ${cal.average_co2} (±${limits.max_delta} allowed)` },
    { ok: confirmed, text: 'You confirmed the station is in known reference air' },
  ];
  box.innerHTML = checks.map((check) =>
    `<p class="cal-check ${check.ok ? 'cal-ok' : 'cal-wait'}">${check.ok ? '✓' : '•'} ${escapeHtml(check.text)}</p>`
  ).join('');
  submit.disabled = !checks.every((check) => check.ok) || pending;
}

async function runButton(button) {
  const type = button.dataset.command;
  if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
  const payload = {};
  if (button.dataset.typed) {
    const typed = window.prompt(`Type ${button.dataset.typed} to confirm`);
    if (typed !== button.dataset.typed) { toast('Not confirmed.', 'info'); return; }
    payload.confirmed = true;
  }
  button.disabled = true; // a double-tap must not queue two fan cleans
  try {
    await submitCommand(type, payload);
  } catch (error) {
    toast(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

tabRefreshers.controls = async () => {
  const now = Math.floor(serverNow());
  try {
    const vitals = await fetchJson(`/api/vitals?from=${now - 600}&to=${now}`);
    recentVitalsLatest = vitals.latest || recentVitalsLatest;
  } catch (_error) { /* the disk line stays a dash */ }
  renderControls(lastLive);
  renderCommandNotes(commandsCache);
};
document.addEventListener('live-updated', () => { if (activeTab === 'controls') renderControls(lastLive); });
document.addEventListener('commands-updated', (event) => { if (activeTab === 'controls') renderCommandNotes(event.detail || []); });

installers.push(() => {
  document.querySelectorAll('button[data-command]:not([type="submit"])').forEach((button) => {
    button.addEventListener('click', () => runButton(button));
  });
  ['target-co2', 'scd41-calibration-drift', 'scd41-calibration-confirm'].forEach((id) => {
    document.getElementById(id).addEventListener('input', renderCalibrationChecklist);
    document.getElementById(id).addEventListener('change', renderCalibrationChecklist);
  });
  document.getElementById('scd41-calibration-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submitCommand('scd41_calibrate', {
      target_ppm: Number(document.getElementById('target-co2').value),
      allow_large_offset: document.getElementById('scd41-calibration-drift').checked,
      persist: document.getElementById('scd41-calibration-persist').checked,
    }).catch((e) => toast(e.message, 'error'));
  });
});
