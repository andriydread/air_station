'use strict';

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

const metricFormats = {
  co2: (v) => v == null ? '--' : `${Math.round(v)} ppm`,
  temp: (v) => v == null ? '--' : `${v.toFixed(1)} °C`,
  humid: (v) => v == null ? '--' : `${v.toFixed(1)} %`,
  pm25: (v) => v == null ? '--' : `${v.toFixed(2)} µg/m³`,
  pm10: (v) => v == null ? '--' : `${v.toFixed(2)} µg/m³`,
  tps: (v) => v == null ? '--' : `${v.toFixed(2)} µm`,
};

const statsMetrics = [
  ['temp', 'Temperature, °C', 1],
  ['humid', 'Humidity, %', 1],
  ['co2', 'CO2, ppm', 0],
  ['pm1', 'PM1, µg/m³', 2],
  ['pm25', 'PM2.5, µg/m³', 2],
  ['pm4', 'PM4, µg/m³', 2],
  ['pm10', 'PM10, µg/m³', 2],
  ['tps', 'Particle size, µm', 2],
];

const weatherIconMap = {
  0: 'sun.png', 1: 'sun.png', 2: 'partly_cloudy.png', 3: 'cloud.png',
  45: 'fog.png', 48: 'fog.png',
  51: 'rain.png', 53: 'rain.png', 55: 'rain.png', 56: 'rain.png', 57: 'rain.png',
  61: 'rain.png', 63: 'rain.png', 65: 'rain.png', 66: 'rain.png', 67: 'rain.png',
  71: 'snow.png', 73: 'snow.png', 75: 'snow.png', 77: 'snow.png',
  80: 'rain.png', 81: 'rain.png', 82: 'rain.png', 85: 'snow.png', 86: 'snow.png',
  95: 'storm.png', 96: 'storm.png', 99: 'storm.png',
};

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function formatTimestamp(value) {
  if (!value) return '--';
  const date = new Date(value);
  const two = (n) => String(n).padStart(2, '0');
  return `${two(date.getHours())}:${two(date.getMinutes())} ${two(date.getDate())}.${two(date.getMonth() + 1)}.${date.getFullYear()}`;
}

function formatAge(seconds) {
  if (seconds == null) return 'no data';
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function formatBytes(bytes) {
  if (bytes == null) return '--';
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatDuration(seconds) {
  if (seconds == null) return '--';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

function formatRelative(value) {
  // "3m ago" for humans; callers put the absolute form in a title attribute.
  if (!value) return '--';
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  return formatAge(seconds);
}

function formatInterval(seconds) {
  if (seconds == null) return '--';
  if (seconds === 0) return 'disabled';
  if (seconds % 86400 === 0) return `${seconds / 86400} day(s)`;
  if (seconds % 3600 === 0) return `${seconds / 3600} hour(s)`;
  if (seconds % 60 === 0) return `${seconds / 60} minute(s)`;
  return `${seconds} second(s)`;
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

async function submitCommand(command, payload = {}) {
  const data = await fetchJson('/api/commands', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command, payload }),
  });
  toast(`Command queued (#${data.id}). Result appears in Diagnostics.`, 'info');
  // Quiet catch: after restart/reboot commands the station is briefly down
  // by design — an unhandled rejection here is noise, not information.
  window.setTimeout(() => refreshSummary().catch(() => {}), 3000);
  // A display command's visible effect is the panel redraw; refresh the
  // preview once the collector has had time to render.
  if (command.startsWith('display_')) window.setTimeout(reloadPreview, 8000);
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

let activeTab = 'live';
const TAB_NAMES = ['live', 'history', 'diagnostics', 'controls'];

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
  if (name === 'history') refreshHistory().catch((e) => toast(e.message, 'error'));
  if (name === 'diagnostics') refreshDiagnostics().catch((e) => toast(e.message, 'error'));
  if (name === 'live') {
    reloadPreview();
    refreshSparklines();
  }
}

window.addEventListener('hashchange', () => {
  const name = location.hash.slice(1) || 'live';
  if (TAB_NAMES.includes(name) && name !== activeTab) switchTab(name, false);
});

// ---------------------------------------------------------------------------
// Live tab
// ---------------------------------------------------------------------------

let lastSummary = null;
let lastSampleIso = null;

function renderSampleAge() {
  // A softly counting "8s ago" is the gentlest proof of life; past ten
  // minutes the absolute time is more useful (and badges/pills are already
  // escalating by then).
  const element = document.getElementById('sample-age');
  if (!lastSampleIso) {
    element.textContent = 'No samples yet';
    return;
  }
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(lastSampleIso).getTime()) / 1000));
  element.textContent = seconds < 600
    ? `Last sample · ${formatAge(seconds)}`
    : `Last sample: ${formatTimestamp(lastSampleIso)}`;
  element.title = formatTimestamp(lastSampleIso);
}
// The ASC checkbox is a form input the user may be editing; it is seeded
// from the collector once and never overwritten by the 10s poll (which used
// to silently revert the user's choice before they clicked Apply).
let ascCheckboxInitialized = false;

function setBadge(metric, age, maxAge) {
  // Quiet when healthy: a fresh metric shows no badge at all, so the one
  // badge that does appear actually means something.
  const badge = document.getElementById(`badge-${metric}`);
  if (!badge) return;
  if (age != null && age <= maxAge) {
    badge.hidden = true;
    return;
  }
  badge.textContent = age == null ? 'no data' : `stale ${formatAge(age)}`;
  badge.className = 'badge badge-stale';
  badge.hidden = false;
}

// Hero values: the number is the star; the unit rides along small and muted.
const heroUnits = { temp: '°C', humid: '%', co2: 'ppm' };

function heroValueHtml(metric, value) {
  if (value == null) return '--';
  const number = metric === 'co2' ? String(Math.round(value)) : value.toFixed(1);
  return `${number}<span class="unit"> ${heroUnits[metric]}</span>`;
}

function applyBandClass(elementId, category) {
  const element = document.getElementById(elementId);
  if (!element) return;
  element.classList.remove('value-warn', 'value-bad');
  if (!category || category === 'Good') return;
  element.classList.add(category === 'Moderate' ? 'value-warn' : 'value-bad');
}

function signalQuality(dbm) {
  if (dbm >= -60) return 'good';
  if (dbm >= -70) return 'ok';
  if (dbm >= -80) return 'weak';
  return 'very weak';
}

const subsystemLabels = [
  ['scd41', 'SCD41 · CO2'],
  ['sht41', 'SHT41 · temp/RH'],
  ['sps30', 'SPS30 · particulates'],
  ['i2c', 'I2C bus'],
  ['display', 'E-paper'],
  ['weather', 'Weather fetch'],
  ['network', 'Wi-Fi'],
  ['power', 'Power'],
  ['storage', 'Storage'],
];

function renderSensorHealthList(sensors) {
  const list = document.getElementById('sensor-health-list');
  if (!list) return;
  list.innerHTML = '';
  for (const [key, label] of subsystemLabels) {
    const entry = sensors[key];
    if (!entry) continue;
    const row = document.createElement('p');
    let detail;
    let bad = false;
    if (entry.healthy === true) {
      const since = entry.last_event_at
        ? ` · quiet ${formatRelative(entry.last_event_at)}` : '';
      detail = `ok${since}`;
    } else if (entry.healthy === false) {
      bad = true;
      detail = entry.last_error || 'unhealthy';
    } else {
      detail = '—'; // never checked yet (e.g. power before the first poll)
    }
    row.innerHTML =
      `<span>${escapeHtml(label)}</span>` +
      `<strong${bad ? ' class="health-bad"' : ''}>${escapeHtml(detail)}</strong>`;
    list.appendChild(row);
  }
}

function sensorHealthSummary(collector) {
  const sensors = collector.sensors || {};
  const entries = ['scd41', 'sht41', 'sps30'].map((key) => sensors[key]).filter(Boolean);
  if (!entries.length) return { headline: '--', ok: false };
  const unhealthy = entries.filter((entry) => !entry.healthy);
  if (!unhealthy.length) return { headline: 'All sensors healthy', ok: true };
  return {
    headline: `${unhealthy.length} sensor issue${unhealthy.length === 1 ? '' : 's'}`,
    ok: false,
  };
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

function renderSummary(summary) {
  lastSummary = summary;
  const live = summary.latest_measurements?.value || {};
  const metrics = Object.keys(live).length ? live : (summary.latest_measurement || {});
  // A crashed collector leaves a last status document saying "running" with
  // young ages forever; the state row's own timestamp is the truth.
  const statusAgeSeconds = summary.collector_status?.updated_at_ts
    ? Date.now() / 1000 - summary.collector_status.updated_at_ts
    : null;
  const collectorSilent = statusAgeSeconds != null && statusAgeSeconds > 90;
  const ages = live.ages || {};
  const agesKnown = Object.keys(ages).length > 0 && !collectorSilent;
  const aqi = summary.aqi || {};
  const collector = summary.collector_status?.value || {};
  const maxAge = collector.measurement_max_age_seconds || 45;
  const health = sensorHealthSummary(collector);
  const network = collector.sensors?.network || {};
  const power = collector.sensors?.power || {};
  const calibration = summary.scd41_last_calibration?.value || {};

  for (const metric of ['co2', 'temp', 'humid', 'pm25', 'pm10', 'tps']) {
    const target = document.getElementById(`metric-${metric}`);
    if (heroUnits[metric]) {
      target.innerHTML = heroValueHtml(metric, metrics[metric]);
    } else {
      target.textContent = metricFormats[metric](metrics[metric]);
    }
    if (agesKnown) {
      setBadge(metric, ages[metric], maxAge);
    } else {
      // Unknown freshness (old collector, or one that stopped reporting):
      // asserting "no data" beside a visible value would be a contradiction.
      const badge = document.getElementById(`badge-${metric}`);
      if (badge) badge.hidden = true;
    }
  }
  document.getElementById('metric-tps-note').textContent = describeTps(metrics.tps);
  const dew = dewPoint(metrics.temp, metrics.humid);
  document.getElementById('metric-dew').textContent = dew == null ? '--' : `${dew.toFixed(1)} °C`;
  document.getElementById('metric-dew-note').textContent = describeDew(dew);
  document.getElementById('metric-aqi').textContent = aqi.value == null ? '--' : String(aqi.value);
  document.getElementById('metric-aqi-label').textContent = aqi.category || '--';
  if (agesKnown) setBadge('aqi', ages.pm25, maxAge);
  document.getElementById('metric-co2-label').textContent = aqi.co2_category || '--';
  // Color by the server-sent band so thresholds can't drift from the
  // backend; "Good" stays uncolored to keep the calm look.
  applyBandClass('metric-aqi', aqi.category);
  applyBandClass('metric-co2', aqi.co2_category);

  lastSampleIso = metrics.timestamp || null;
  renderSampleAge();

  const problems = [];
  if (collectorSilent) problems.push('Collector not reporting');
  else if (!collector.running) problems.push('Collector stopped');
  if (!health.ok && health.headline !== '--') problems.push(health.headline);
  if (network.healthy === false) problems.push('Network offline');
  if (power.available !== false && power.healthy === false) problems.push('Power issue');
  if (collector.sensors?.storage?.healthy === false) problems.push('Low disk space');
  renderStatusStrip(problems);

  // Diagnostics-side details rendered from the same summary
  renderSensorHealthList(collector.sensors || {});
  document.getElementById('collector-uptime').textContent =
    collector.uptime_seconds == null
      ? '--'
      : `${formatDuration(collector.uptime_seconds)} (since ${formatTimestamp(collector.started_at)})`;
  document.getElementById('network-interface').textContent = network.interface || '--';
  // Human words up front; the raw kernel flags live in the hover title.
  const stateElement = document.getElementById('network-status');
  const offlineSince = network.last_success_at
    ? ` · last ok ${formatRelative(network.last_success_at)}` : '';
  stateElement.textContent = network.healthy ? 'Connected' : `Offline${offlineSince}`;
  stateElement.className = network.healthy ? '' : 'health-bad';
  stateElement.title =
    `operstate=${network.operstate || '--'} carrier=${network.carrier || '--'}`;
  document.getElementById('network-signal').textContent =
    network.signal_level_dbm == null ? '--' : `${network.signal_level_dbm} dBm · ${signalQuality(network.signal_level_dbm)}`;
  document.getElementById('network-latency').textContent =
    network.latency_ms == null ? '--' : `${network.latency_ms} ms`;
  const lastSuccess = document.getElementById('network-last-success');
  lastSuccess.textContent = formatRelative(network.last_success_at);
  lastSuccess.title = formatTimestamp(network.last_success_at);
  document.getElementById('network-last-error').textContent = network.last_error || '--';
  document.getElementById('power-undervoltage').textContent =
    power.available === false ? 'n/a' :
      power.undervoltage_now ? 'NOW' : power.undervoltage_since_boot ? 'since boot' : 'no';
  document.getElementById('power-throttled').textContent =
    power.available === false ? 'n/a' :
      power.throttled_now ? 'NOW' : power.throttled_since_boot ? 'since boot' : 'no';

  // Controls-side details
  document.getElementById('auto-clean-current').textContent =
    formatInterval(collector.sps30_auto_cleaning_interval_seconds);
  document.getElementById('scd41-last-calibration').textContent =
    formatTimestamp(calibration.calibrated_at || collector.sensors?.scd41?.last_calibration_at);
  renderCommandNotes(summary.recent_commands || []);
  lastCalibrationReadiness = collector.scd41_calibration || null;
  renderCalibrationChecklist();
  document.getElementById('database-path').textContent = collector.database_path || '--';
  document.getElementById('collector-log-file').textContent = collector.log_file || '--';
  const dbStats = summary.database || {};
  document.getElementById('database-entries').textContent =
    dbStats.measurements == null ? '--' : dbStats.measurements.toLocaleString();
  document.getElementById('database-size').textContent = formatBytes(dbStats.size_bytes);
  document.getElementById('database-free').textContent =
    formatBytes(collector.sensors?.storage?.free_bytes);
  if (!ascCheckboxInitialized && collector.scd41_asc_enabled != null) {
    document.getElementById('scd41-asc-enabled').checked = !!collector.scd41_asc_enabled;
    ascCheckboxInitialized = true;
  }
  document.getElementById('scd41-asc-state').textContent =
    collector.scd41_asc_enabled == null ? '--' : (collector.scd41_asc_enabled ? 'on' : 'off');

  const weather = summary.latest_weather?.value || {};
  document.getElementById('weather-updated').textContent =
    `Updated: ${formatTimestamp(summary.latest_weather?.updated_at)}`;
  renderWeather(weather);
  renderCommands(summary.recent_commands || []);

  // A pinned tab doubles as an ambient display.
  const titleCo2 = metrics.co2 != null ? `${Math.round(metrics.co2)} ppm` : '--';
  const titleAqi = aqi.value != null ? ` · AQI ${aqi.value}` : '';
  document.title = `${titleCo2}${titleAqi} — Air Station`;
}

function renderWeather(weather) {
  const grid = document.getElementById('forecast-grid');
  grid.innerHTML = '';
  const entries = [weather[1] || weather['1'], weather[2] || weather['2'], weather[3] || weather['3']].filter(Boolean);
  if (!entries.length) {
    grid.innerHTML = '<div class="empty-state">No forecast data yet.</div>';
    return;
  }
  for (const block of entries) {
    const [windowLabel, maxTemp, minTemp, precip, code] = block;
    const icon = weatherIconMap[code] || 'sun.png';
    const tempText = (maxTemp != null && minTemp != null) ? `${maxTemp} / ${minTemp} °C` : '-- / -- °C';
    const card = document.createElement('article');
    card.className = 'forecast-card';
    card.innerHTML = `
      <p class="forecast-window">${escapeHtml(windowLabel)}</p>
      <div class="forecast-body">
        <img class="forecast-icon" src="/assets/icons/${icon}" alt="">
        <div>
          <p class="forecast-stat">${escapeHtml(tempText)}</p>
          <p class="forecast-stat">Rain: ${precip != null ? escapeHtml(String(precip)) : '--'}%</p>
        </div>
      </div>`;
    grid.appendChild(card);
  }
}

// --- Hero sparklines (last 24h, no axes — trends live in History) ----------

let sparkRows = null;

async function refreshSparklines() {
  try {
    const data = await fetchJson('/api/history?hours=24');
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
  // Retrospective narration under the forecast — "what happened" beside
  // "what's coming". Derived from the already-fetched 24h rows; nothing
  // here can turn red.
  const wrapper = document.getElementById('today-recap');
  const list = document.getElementById('today-recap-list');
  if (!wrapper || !list) return;
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const todayRows = sparkRows.filter((row) => row.timestamp_ts * 1000 >= midnight.getTime());
  const timeOf = (row) => {
    const date = new Date(row.timestamp_ts * 1000);
    return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  };
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
    lines.push(['Warmest', `${warmest.temp.toFixed(1)}° at ${timeOf(warmest)}`]);
    lines.push(['Coolest', `${coolest.temp.toFixed(1)}° at ${timeOf(coolest)}`]);
  }
  if (co2Peak) lines.push(['CO2 peak', `${Math.round(co2Peak.co2)} ppm at ${timeOf(co2Peak)}`]);
  if (!lines.length) {
    wrapper.hidden = true;
    return;
  }
  list.innerHTML = lines.map(([label, value]) =>
    `<p><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></p>`
  ).join('');
  wrapper.hidden = false;
}

// Minimum y-span per metric: a rock-steady room must render as a nearly
// flat whisper of a line, not autoscaled drama — flat means fine.
const sparklineMinSpan = { temp: 2, humid: 6, co2: 250, aqi: 25 };

function renderSparkline(svgId, rows, key) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  const points = rows.filter((row) => row[key] != null && row.timestamp_ts != null);
  if (points.length < 2) {
    svg.innerHTML = '';
    return;
  }
  const width = 240;
  const height = 48;
  const pad = 3;
  const xs = points.map((row) => row.timestamp_ts);
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
    pad + ((row.timestamp_ts - xMin) / xSpan) * (width - 2 * pad),
    pad + (1 - ((row[key] - yMin) / ySpan)) * (height - 2 * pad),
  ]);
  const line = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const [lastX, lastY] = coords[coords.length - 1];
  svg.innerHTML = `<polyline fill="none" stroke="currentColor" stroke-width="2" points="${line}"></polyline>
    <circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="2.5" fill="currentColor"></circle>`;
}

function renderHeroRange(key) {
  const element = document.getElementById(`range-${key}`);
  if (!element) return;
  const values = sparkRows.filter((row) => row[key] != null).map((row) => row[key]);
  if (values.length < 2) {
    element.textContent = '';
    return;
  }
  const digits = key === 'co2' || key === 'aqi' ? 0 : 1;
  element.textContent =
    `24h ${Math.min(...values).toFixed(digits)} – ${Math.max(...values).toFixed(digits)}`;
}

let previewObjectUrl = null;

async function reloadPreview() {
  const image = document.getElementById('display-preview');
  const note = document.getElementById('preview-note');
  try {
    // Stable URL on purpose: the server ETags the snapshot, so an unchanged
    // preview revalidates as a free 304 (served from the browser cache)
    // instead of a fresh server-side render every poll.
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

// ---------------------------------------------------------------------------
// History tab
// ---------------------------------------------------------------------------

let range = { mode: 'preset', hours: 24 };
let lastHistoryRows = null;
// Monotonic token: a slow 7d response landing after a fast 6h one must not
// overwrite the charts with data for a range no longer selected.
let historyRequestToken = 0;

const chartConfigs = {
  temp: {
    color: '#b85c38',
    formatter: (row) => `${row.temp.toFixed(1)} °C`,
    bounds: (values) => {
      const min = Math.min(...values);
      const max = Math.max(...values);
      return { min: min < 0 ? Math.floor(min - 1) : 0, max: max > 40 ? Math.ceil(max + 1) : 40 };
    },
  },
  humid: { color: '#2b6f9e', formatter: (row) => `${row.humid.toFixed(1)} %`, bounds: () => ({ min: 0, max: 100 }) },
  co2: { color: '#1f5c4a', formatter: (row) => `${Math.round(row.co2)} ppm`, bounds: (v) => dynamicFromZero(v, 100) },
  aqi: { color: '#9e6f00', formatter: (row) => `${Math.round(row.aqi)}`, bounds: (v) => dynamicFromZero(v, 25) },
  pm25: { color: '#5b4b8a', formatter: (row) => `${row.pm25.toFixed(2)} µg/m³`, bounds: (v) => dynamicFromZero(v, 5) },
  pm10: { color: '#6f4a2a', formatter: (row) => `${row.pm10.toFixed(2)} µg/m³`, bounds: (v) => dynamicFromZero(v, 5) },
};

function dynamicFromZero(values, minSpan) {
  const rawMax = Math.max(...values, 0);
  const paddedMax = rawMax <= 0 ? minSpan : Math.ceil((rawMax * 1.1) / minSpan) * minSpan;
  return { min: 0, max: Math.max(minSpan, paddedMax) };
}

function rangeQuery() {
  if (range.mode === 'custom') return `from=${range.from}&to=${range.to}`;
  return `hours=${range.hours}`;
}

let lastBucketSeconds = 60;

async function refreshHistory() {
  const token = ++historyRequestToken;
  const data = await fetchJson(`/api/history?${rangeQuery()}`);
  if (token !== historyRequestToken) return; // superseded by a newer request
  lastBucketSeconds = data.bucket_seconds || 60;
  lastHistoryRows = data.rows || [];
  renderAllCharts(lastHistoryRows);
  renderStats(data.stats || {}, data.from_ts, data.to_ts);
  document.getElementById('export-csv').href = `/api/export.csv?${rangeQuery()}`;
  document.getElementById('open-text').href = `/api/export.txt?${rangeQuery()}`;
}

// Copy the selected range as the paste-friendly text export. The async
// Clipboard API only exists in secure contexts (HTTPS or localhost) and the
// dashboard is plain HTTP on the LAN, so fall back to the legacy
// selection+execCommand path, and to opening the text when even that fails.
async function copyRangeAsText() {
  const url = `/api/export.txt?${rangeQuery()}`;
  let response;
  try {
    response = await fetch(url);
  } catch (_error) {
    throw new Error('Station unreachable');
  }
  if (!response.ok) throw new Error(`Export failed (${response.status})`);
  const text = await response.text();
  const lines = text.split('\n').filter((line) => line && !line.startsWith('#')).length - 1;
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    toast(`Copied ${lines} lines to the clipboard`, 'info');
    return;
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '-1000px';
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try { copied = document.execCommand('copy'); } catch (_error) { copied = false; }
  document.body.removeChild(area);
  if (copied) {
    toast(`Copied ${lines} lines to the clipboard`, 'info');
  } else {
    window.open(url, '_blank', 'noopener');
    toast('Clipboard blocked by the browser — opened the text instead; select all and copy.', 'info');
  }
}

function renderStats(stats, fromTs, toTs) {
  // Show the resolved window so it is obvious the stats follow the range
  // (with young data every range holds the same samples).
  const window_ = (fromTs && toTs)
    ? ` · ${formatTimestamp(fromTs * 1000)} → ${formatTimestamp(toTs * 1000)}`
    : '';
  document.getElementById('stats-note').textContent =
    stats.sample_count != null ? `${stats.sample_count} raw samples${window_}` : '--';
  const body = document.querySelector('#stats-table tbody');
  body.innerHTML = '';
  for (const [key, label, digits] of statsMetrics) {
    const entry = stats[key] || {};
    const format = (v) => v == null ? '--' : Number(v).toFixed(digits);
    const row = document.createElement('tr');
    row.innerHTML = `<td>${escapeHtml(label)}</td><td>${format(entry.min)}</td><td>${format(entry.avg)}</td><td>${format(entry.max)}</td><td class="stats-range">${statsRangeBar(key, entry)}</td>`;
    body.appendChild(row);
  }
}

function statsRangeBar(key, entry) {
  // A one-row box-plot-lite: min→max as a slim track, a dot at the average
  // — shows at a glance whether the average hugs the floor or the ceiling.
  if (entry.min == null || entry.max == null || entry.avg == null) return '';
  const config = chartConfigs[key];
  const bounds = config
    ? config.bounds([entry.min, entry.max])
    : { min: 0, max: Math.max(entry.max * 1.15, 1) };
  const span = bounds.max - bounds.min || 1;
  const clamp = (value) => Math.min(Math.max(value, 0), 100);
  const left = clamp(((entry.min - bounds.min) / span) * 100);
  const right = clamp(((entry.max - bounds.min) / span) * 100);
  const dot = clamp(((entry.avg - bounds.min) / span) * 100);
  return `<div class="range-track">` +
    `<div class="range-fill" style="left:${left.toFixed(1)}%;width:${Math.max(right - left, 1.5).toFixed(1)}%"></div>` +
    `<div class="range-dot" style="left:${dot.toFixed(1)}%"></div></div>`;
}

function renderAllCharts(rows) {
  renderLineChart('chart-temp', rows, 'temp', chartConfigs.temp);
  renderLineChart('chart-humid', rows, 'humid', chartConfigs.humid);
  renderLineChart('chart-co2', rows, 'co2', chartConfigs.co2);
  renderLineChart('chart-aqi', rows, 'aqi', chartConfigs.aqi);
  renderLineChart('chart-pm25', rows, 'pm25', chartConfigs.pm25);
  renderLineChart('chart-pm10', rows, 'pm10', chartConfigs.pm10);
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

// Threshold guide lines, matching the backend's category boundaries
// (get_co2_category / get_aqi_category) so "crossed into stuffy" is
// readable straight off the chart.
const chartGuides = {
  co2: [{ at: 1000, label: 'stuffy' }, { at: 1500, label: 'ventilate' }],
  aqi: [{ at: 50, label: 'good' }, { at: 100, label: 'moderate' }, { at: 175, label: 'unhealthy' }],
};

function formatTickLabel(tick, yRange) {
  return yRange >= 50 ? String(Math.round(tick)) : tick.toFixed(1);
}

function nightRects(xMin, xMax, toX, padding, height) {
  // Local 22:00–07:00 shading; only worthwhile on short ranges where the
  // stripes aid orientation instead of becoming noise.
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
      rects.push(
        `<rect x="${toX(start)}" y="${padding.top}" width="${toX(end) - toX(start)}" ` +
        `height="${height - padding.top - padding.bottom}" fill="currentColor" opacity="0.05"></rect>`
      );
    }
    cursor.setDate(cursor.getDate() + 1);
  }
  return rects.join('');
}

function formatAxisTimestamp(seconds, spanSeconds) {
  const date = new Date(seconds * 1000);
  const two = (n) => String(n).padStart(2, '0');
  if (spanSeconds > 48 * 3600) {
    return `${two(date.getDate())}.${two(date.getMonth() + 1)}`;
  }
  return `${two(date.getHours())}:${two(date.getMinutes())}`;
}

function renderLineChart(svgId, rows, key, config) {
  const svg = document.getElementById(svgId);
  const tooltip = document.getElementById(`tooltip-${svgId}`);
  const rowsWithTime = rows.filter((row) => row.timestamp_ts != null);
  const points = rowsWithTime.filter((row) => row[key] != null);
  const colors = themeColors();

  if (!points.length) {
    svg.innerHTML = `<text x="24" y="40" fill="${colors.chartLabel}" font-size="16">No data</text>`;
    tooltip.style.opacity = '0';
    chartState.delete(svgId);
    return;
  }

  const width = 640;
  const height = 220;
  const padding = { top: 18, right: 16, bottom: 40, left: 54 };
  const values = points.map((row) => row[key]);
  const yBounds = config.bounds(values);
  const xMin = Math.min(...rowsWithTime.map((row) => row.timestamp_ts));
  const xMaxRaw = Math.max(...rowsWithTime.map((row) => row.timestamp_ts));
  const xMax = xMaxRaw === xMin ? xMin + 1 : xMaxRaw;
  const yRange = yBounds.max - yBounds.min || 1;
  const span = xMax - xMin;

  const toX = (ts) =>
    padding.left + ((ts - xMin) / (xMax - xMin)) * (width - padding.left - padding.right);
  const toY = (value) =>
    padding.top + (height - padding.top - padding.bottom) * (1 - ((value - yBounds.min) / yRange));

  const coordinates = points.map((row) => ({ x: toX(row.timestamp_ts), y: toY(row[key]), row }));

  // Split into segments across data gaps: an offline stretch must render as
  // a gap, not a confident straight line bridging fabricated values.
  const gapSeconds = Math.max(lastBucketSeconds * 2, 120);
  const segments = [];
  let current = [];
  coordinates.forEach((point, index) => {
    if (index > 0 &&
        point.row.timestamp_ts - coordinates[index - 1].row.timestamp_ts > gapSeconds) {
      segments.push(current);
      current = [];
    }
    current.push(point);
  });
  segments.push(current);
  const series = segments.map((segment) =>
    segment.length === 1
      ? `<circle cx="${segment[0].x}" cy="${segment[0].y}" r="4" fill="${config.color}"></circle>`
      : `<polyline fill="none" stroke="${config.color}" stroke-width="3" points="${segment.map((p) => `${p.x},${p.y}`).join(' ')}"></polyline>`
  ).join('');

  const horizontalGrid = computeTicks(yBounds.min, yBounds.max, 5).map((tick) => {
    const y = toY(tick);
    return `
      <line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="${colors.chartGrid}" stroke-dasharray="4 4" />
      <text x="8" y="${y + 4}" fill="${colors.chartLabel}" font-size="12">${formatTickLabel(tick, yRange)}</text>`;
  }).join('');
  const verticalTicks = computeTicks(xMin, xMax, 5).map((tick) => {
    const x = toX(tick);
    return `
      <line x1="${x}" y1="${padding.top}" x2="${x}" y2="${height - padding.bottom}" stroke="${colors.chartGridSoft}" />
      <text x="${x}" y="${height - 12}" text-anchor="middle" fill="${colors.chartLabel}" font-size="12">${formatAxisTimestamp(tick, span)}</text>`;
  }).join('');
  const guides = (chartGuides[key] || [])
    .filter((guide) => guide.at > yBounds.min && guide.at < yBounds.max)
    .map((guide) => {
      const y = toY(guide.at);
      return `
      <line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="${colors.chartLabel}" stroke-width="1" stroke-dasharray="2 5" opacity="0.6" />
      <text x="${width - padding.right - 4}" y="${y - 4}" text-anchor="end" fill="${colors.chartLabel}" font-size="10">${guide.at} · ${guide.label}</text>`;
    }).join('');

  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="transparent"></rect>
    <g style="color:${colors.chartLabel}">${nightRects(xMin, xMax, toX, padding, height)}</g>
    ${horizontalGrid}
    ${verticalTicks}
    ${guides}
    <line id="crosshair-${svgId}" x1="0" y1="${padding.top}" x2="0" y2="${height - padding.bottom}" stroke="${config.color}" stroke-width="1.5" stroke-dasharray="4 4" opacity="0"></line>
    <circle id="focus-${svgId}" cx="0" cy="0" r="5" fill="${config.color}" stroke="${colors.paper}" stroke-width="2" opacity="0"></circle>
    ${series}`;

  chartState.set(svgId, { coordinates, formatValue: config.formatter, width, height });
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

// All six charts render the same rows, so one hover can move every
// crosshair to the same moment — cause-and-effect reading across metrics.
function syncCrosshairs(sourceId, timestampTs) {
  chartState.forEach((state, id) => {
    if (id === sourceId || !state.coordinates.length) return;
    let nearest = state.coordinates[0];
    for (const point of state.coordinates) {
      if (Math.abs(point.row.timestamp_ts - timestampTs)
          < Math.abs(nearest.row.timestamp_ts - timestampTs)) {
        nearest = point;
      }
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
    tooltip.innerHTML = `<strong>${escapeHtml(state.formatValue(nearest.row))}</strong><br>${escapeHtml(formatTimestamp(nearest.row.timestamp))}`;
    tooltip.style.opacity = '1';
    tooltip.style.left = `${(nearest.x / state.width) * rect.width}px`;
    tooltip.style.top = `${(nearest.y / state.height) * rect.height - 10}px`;
    syncCrosshairs(svgId, nearest.row.timestamp_ts);
  };

  // Pointer events instead of mouse events: a tap pins the tooltip (there
  // is no hover on the primary device — a phone); tap-outside dismisses
  // via the document listener installed once below.
  svg.addEventListener('pointermove', (event) => {
    if (event.pointerType === 'mouse') show(event);
  });
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('mouseleave', hideAllChartTooltips);
}

document.addEventListener('pointerdown', (event) => {
  if (!event.target.closest('.chart-frame')) hideAllChartTooltips();
});

// ---------------------------------------------------------------------------
// Diagnostics tab
// ---------------------------------------------------------------------------

async function refreshDiagnostics() {
  const level = document.getElementById('event-level').value;
  const source = document.getElementById('event-source').value;
  const query = new URLSearchParams({ limit: 100 });
  if (level) query.set('level', level);
  if (source) query.set('source', source);
  const [events, flags, collectorEvents, storageEvents] = await Promise.all([
    fetchJson(`/api/events?${query}`),
    fetchJson('/api/flags?limit=30'),
    fetchJson('/api/events?source=collector&limit=50'),
    fetchJson('/api/events?source=storage&limit=50'),
  ]);
  renderEvents(events.events || []);
  renderFlagged(flags.flagged || []);
  renderRestartCount(collectorEvents.events || []);
  renderHousekeeping(storageEvents.events || []);
}

function renderHousekeeping(storageEvents) {
  const overdueSeconds = 26 * 3600; // nightly tasks get a 2h grace period
  const setLine = (id, iso, canBeOverdue) => {
    const element = document.getElementById(id);
    if (!element) return;
    if (!iso) {
      element.textContent = 'never';
      element.className = '';
      return;
    }
    const age = (Date.now() - new Date(iso).getTime()) / 1000;
    const overdue = canBeOverdue && age > overdueSeconds;
    element.textContent = formatRelative(iso) + (overdue ? ' · overdue' : '');
    element.title = formatTimestamp(iso);
    element.className = overdue ? 'health-bad' : '';
  };
  const newest = (type) =>
    storageEvents.find((event) => event.event_type === type)?.created_at || null;
  setLine('hk-backup', newest('backup_written'), true);
  setLine('hk-prune', newest('pruned'), true);
  const cleanIso = lastSummary?.collector_status?.value?.sensors?.sps30?.last_manual_clean_at;
  setLine('hk-clean', cleanIso || null, false);
}

function renderRestartCount(collectorEvents) {
  // >1 start in 24h is the watchdog-crash-loop tell; a single boot is normal.
  const element = document.getElementById('collector-restarts');
  if (!element) return;
  const dayAgo = Date.now() - 24 * 3600 * 1000;
  const restarts = collectorEvents.filter(
    (event) => event.event_type === 'started' && new Date(event.created_at).getTime() >= dayAgo
  ).length;
  element.textContent = String(restarts);
  element.className = restarts > 1 ? 'health-bad' : '';
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
        <span>${escapeHtml(event.source)} / ${escapeHtml(event.event_type)}</span>
        <span class="event-level event-level-${escapeHtml(event.level)}">${escapeHtml(event.level)}</span>
      </header>
      <p>${escapeHtml(event.message)}</p>
      <p class="event-time" title="${escapeHtml(formatTimestamp(event.created_at))}">${escapeHtml(formatRelative(event.created_at))}</p>
      ${details}`;
    list.appendChild(item);
  }
}

function renderFlagged(flagged) {
  const list = document.getElementById('flagged-list');
  list.innerHTML = '';
  if (!flagged.length) {
    list.innerHTML = '<div class="empty-state">No flagged samples. Good.</div>';
    return;
  }
  for (const item of flagged) {
    const article = document.createElement('article');
    article.className = 'event-item';
    const parts = Object.entries(item.flags || {})
      .map(([metric, info]) => `${metric}=${info.value} (${info.reason})`).join('; ');
    article.innerHTML = `
      <p>${escapeHtml(parts)}</p>
      <p class="event-time" title="${escapeHtml(formatTimestamp(item.timestamp))}">${escapeHtml(formatRelative(item.timestamp))}</p>`;
    list.appendChild(article);
  }
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
        <span>${escapeHtml(command.command)}</span>
        <span class="command-status-${escapeHtml(command.status)}">${escapeHtml(command.status)}${commandTiming(command)}</span>
      </header>
      <p class="event-time" title="${escapeHtml(formatTimestamp(command.created_at))}">${escapeHtml(formatRelative(command.created_at))}</p>
      ${result}`;
    list.appendChild(item);
  }
}

function commandTiming(command) {
  // Whether the Pi actually heard the button: completed rows say how fast,
  // an old pending row says the collector isn't picking commands up.
  const created = new Date(command.created_at).getTime();
  if (command.status === 'pending') {
    const waiting = (Date.now() - created) / 1000;
    return waiting > 15
      ? ' <span class="health-bad">· not picked up — collector may be down</span>'
      : ' · waiting for the Pi…';
  }
  if (command.status === 'succeeded' || command.status === 'failed') {
    const took = Math.max(0, Math.round((new Date(command.updated_at).getTime() - created) / 1000));
    return ` · ${took}s`;
  }
  return '';
}

// ---------------------------------------------------------------------------
// SCD41 calibration checklist
// ---------------------------------------------------------------------------
// The collector publishes live readiness numbers plus the very limits the
// backend enforces, so the button unlocks exactly when a command would pass.

let lastCalibrationReadiness = null;
// Command names with a fresh pending/running row: their buttons are held
// disabled and footnoted "running…" until the collector reports back.
const pendingCommandNames = new Set();

function renderCommandNotes(commands) {
  pendingCommandNames.clear();
  for (const command of commands) {
    if (command.status !== 'pending' && command.status !== 'running') continue;
    const age = (Date.now() - new Date(command.created_at).getTime()) / 1000;
    if (age <= 60) pendingCommandNames.add(command.command);
  }
  document.querySelectorAll('[data-command-note]').forEach((note) => {
    const name = note.dataset.commandNote;
    const button =
      document.querySelector(`[data-command="${name}"], [data-system="${name}"]`);
    if (pendingCommandNames.has(name)) {
      note.textContent = 'running…';
      note.className = 'command-note';
      if (button) button.disabled = true;
      return;
    }
    if (button) button.disabled = false;
    const latest = commands.find((command) => command.command === name);
    if (!latest) {
      note.textContent = '';
      return;
    }
    note.textContent = `Last run: ${formatRelative(latest.updated_at)} · ${latest.status}`;
    note.className = latest.status === 'failed' ? 'command-note health-bad' : 'command-note';
    note.title = formatTimestamp(latest.updated_at);
  });
}

function renderCalibrationChecklist() {
  const box = document.getElementById('scd41-checklist');
  const submit = document.getElementById('scd41-calibration-submit');
  const cal = lastCalibrationReadiness;
  const calibrationPending = pendingCommandNames.has('scd41_force_calibration');
  if (!cal || !cal.limits) {
    // Collector too old (or down) to publish readiness: don't block the form.
    box.innerHTML = '';
    submit.disabled = calibrationPending;
    return;
  }
  const limits = cal.limits;
  const target = Number(document.getElementById('target-co2').value) || 420;
  const driftOverride = document.getElementById('scd41-calibration-drift').checked;
  const delta = cal.average_co2 == null ? null : Math.abs(cal.average_co2 - target);

  const checks = [
    {
      ok: cal.runtime_seconds >= limits.min_runtime,
      text: `Warmed up — running ${cal.runtime_seconds}s of ${limits.min_runtime}s`,
    },
    {
      ok: cal.sample_count >= limits.min_samples,
      text: `Enough readings — ${cal.sample_count} of ${limits.min_samples}`,
    },
    {
      ok: cal.spread_co2 != null && cal.spread_co2 <= limits.max_spread,
      text: cal.spread_co2 == null
        ? 'Stable readings — no data yet'
        : `Stable readings — spread ${cal.spread_co2} ppm (limit ${limits.max_spread})`,
    },
    driftOverride
      ? { ok: true, text: 'Near target — skipped (drift override)' }
      : {
        ok: delta != null && delta <= limits.max_reference_delta,
        text: delta == null
          ? `Near ${target} ppm — no data yet`
          : `Near ${target} ppm — sensor reads ${cal.average_co2} (±${limits.max_reference_delta} allowed)`,
      },
  ];
  box.innerHTML = checks.map((check) =>
    `<p class="cal-check ${check.ok ? 'cal-ok' : 'cal-wait'}">${check.ok ? '✓' : '•'} ${escapeHtml(check.text)}</p>`
  ).join('');
  submit.disabled = !checks.every((check) => check.ok) || calibrationPending;
}

// ---------------------------------------------------------------------------
// Custom dropdowns
// ---------------------------------------------------------------------------
// Native <select> popups commit on mouse-release on the operator's system
// (press-and-hold semantics), which made the menus unusable. The native
// select stays in the DOM as the value store — existing change listeners
// and .value reads keep working — and if this upgrade ever fails the page
// falls back to the native control.

const dropdownLabels = {
  'event-level': 'Event level',
  'event-source': 'Event source',
  'auto-clean-unit': 'Interval unit',
};

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
  if (dropdownLabels[select.id]) toggle.setAttribute('aria-label', dropdownLabels[select.id]);

  const menu = document.createElement('ul');
  menu.className = 'dropdown-menu';
  menu.setAttribute('role', 'listbox');
  menu.hidden = true;

  const labelOf = (option) => option.textContent.trim() || option.value;
  const syncToggle = () => {
    const selected = select.options[select.selectedIndex];
    toggle.textContent = selected ? labelOf(selected) : '--';
  };

  let focusIndex = -1;

  const highlight = () => {
    menu.querySelectorAll('li').forEach((item, index) => {
      item.classList.toggle('focused', index === focusIndex);
    });
    // Screen readers follow the arrowed-to option through the toggle.
    if (focusIndex >= 0) {
      toggle.setAttribute('aria-activedescendant', `${select.id}-option-${focusIndex}`);
    } else {
      toggle.removeAttribute('aria-activedescendant');
    }
  };

  const close = () => {
    menu.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.removeAttribute('aria-activedescendant');
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
      item.id = `${select.id}-option-${index}`;
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', index === select.selectedIndex ? 'true' : 'false');
      item.textContent = labelOf(option);
      // mousedown + preventDefault: selects before the toggle can blur, and
      // keeps focus on the toggle so the blur-close never races the pick.
      item.addEventListener('mousedown', (event) => {
        event.preventDefault();
        choose(index);
      });
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
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      focusIndex = Math.min(Math.max(focusIndex + delta, 0), select.options.length - 1);
      highlight();
      return;
    }
    if (event.key === 'Enter' && !menu.hidden) {
      event.preventDefault();
      if (focusIndex >= 0) choose(focusIndex);
    }
  });
  toggle.addEventListener('blur', (event) => {
    // Tabbing away must not leave the menu floating over the page.
    if (!wrapper.contains(event.relatedTarget)) close();
  });
  document.addEventListener('click', (event) => {
    if (!menu.hidden && !wrapper.contains(event.target)) close();
  });

  wrapper.appendChild(toggle);
  wrapper.appendChild(menu);
  syncToggle();
}

function upgradeSelects() {
  document.querySelectorAll('#event-level, #event-source, #auto-clean-unit').forEach(upgradeSelect);
}

// ---------------------------------------------------------------------------
// Controls tab actions
// ---------------------------------------------------------------------------

const systemConfirmations = {
  system_restart_collector: 'Restart the collector service? Measurements pause for ~30 seconds.',
  system_restart_web: 'Restart the dashboard service? The page will briefly disconnect.',
  system_reboot: 'REBOOT the Pi? The whole station goes down for about a minute.',
};

async function deleteHistory() {
  if (!window.confirm('Delete ALL stored measurement history? This cannot be undone.')) return;
  const data = await fetchJson('/api/history', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: 'delete' }),
  });
  toast(data.status || 'History deleted.', 'info');
  await refreshAll();
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

// The toggle shows the mode a click switches TO: a moon while light, a sun
// while dark.
const themeIcons = {
  light: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg>',
  dark: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.2" y1="4.2" x2="5.6" y2="5.6"/><line x1="18.4" y1="18.4" x2="19.8" y2="19.8"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.2" y1="19.8" x2="5.6" y2="18.4"/><line x1="18.4" y1="5.6" x2="19.8" y2="4.2"/></svg>',
};

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { window.localStorage.setItem('airmonitor-theme', theme); } catch (_e) { /* private mode */ }
  const toggle = document.getElementById('theme-toggle');
  toggle.innerHTML = themeIcons[theme] || themeIcons.light;
  toggle.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
  if (lastHistoryRows) renderAllCharts(lastHistoryRows);
}

function initTheme() {
  let stored = null;
  try { stored = window.localStorage.getItem('airmonitor-theme'); } catch (_e) { /* private mode */ }
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
    const two = (n) => String(n).padStart(2, '0');
    timeEl.textContent = `${two(now.getHours())}:${two(now.getMinutes())}`;
    const weekday = now.toLocaleDateString(undefined, { weekday: 'long' });
    dateEl.textContent = `${weekday} · ${two(now.getDate())}.${two(now.getMonth() + 1)}.${now.getFullYear()}`;
    renderSampleAge(); // the "8s ago" line breathes with the clock
  };
  tick();
  window.setInterval(tick, 10000);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

async function refreshSummary() {
  renderSummary(await fetchJson('/api/summary'));
}

async function refreshAll() {
  await refreshSummary();
  if (activeTab === 'history') await refreshHistory();
  if (activeTab === 'diagnostics') await refreshDiagnostics();
}

function installActions() {
  // Hints must work by tap too — hover doesn't exist on the primary device.
  const closeAllHints = () => {
    document.querySelectorAll('.hint.hint-open').forEach((open) => {
      open.classList.remove('hint-open');
    });
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

  document.querySelectorAll('.tab-button').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });

  document.querySelectorAll('[data-command]').forEach((button) => {
    button.addEventListener('click', () => {
      button.disabled = true; // a double-click must not queue two fan cleans
      submitCommand(button.dataset.command)
        .catch((e) => toast(e.message, 'error'))
        .finally(() => { button.disabled = false; });
    });
  });

  document.querySelectorAll('[data-system]').forEach((button) => {
    button.addEventListener('click', () => {
      const command = button.dataset.system;
      if (!window.confirm(systemConfirmations[command])) return;
      button.disabled = true;
      submitCommand(command, { confirmed: true })
        .catch((e) => toast(e.message, 'error'))
        .finally(() => { button.disabled = false; });
    });
  });

  document.querySelectorAll('#range-presets button').forEach((button) => {
    button.addEventListener('click', () => {
      range = { mode: 'preset', hours: Number(button.dataset.hours) };
      document.querySelectorAll('#range-presets button').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      refreshHistory().catch((e) => toast(e.message, 'error'));
    });
  });

  document.getElementById('copy-text').addEventListener('click', (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    copyRangeAsText()
      .catch((e) => toast(e.message, 'error'))
      .finally(() => { button.disabled = false; });
  });

  document.getElementById('custom-range-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const fromValue = document.getElementById('range-from').value;
    const toValue = document.getElementById('range-to').value;
    if (!fromValue) { toast('Pick a start date first.', 'error'); return; }
    const from = Math.floor(new Date(fromValue).getTime() / 1000);
    const to = toValue ? Math.floor(new Date(toValue).getTime() / 1000) : Math.floor(Date.now() / 1000);
    range = { mode: 'custom', from, to };
    document.querySelectorAll('#range-presets button').forEach((item) => item.classList.remove('active'));
    refreshHistory().catch((e) => toast(e.message, 'error'));
  });

  document.getElementById('event-refresh').addEventListener('click', () => {
    refreshDiagnostics().catch((e) => toast(e.message, 'error'));
  });
  document.getElementById('event-level').addEventListener('change', () => {
    refreshDiagnostics().catch((e) => toast(e.message, 'error'));
  });
  document.getElementById('event-source').addEventListener('change', () => {
    refreshDiagnostics().catch((e) => toast(e.message, 'error'));
  });

  document.getElementById('sps30-auto-clean-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const value = Number(document.getElementById('auto-clean-value').value);
    const unit = document.getElementById('auto-clean-unit').value;
    const multipliers = { seconds: 1, minutes: 60, hours: 3600, days: 86400 };
    submitCommand('sps30_set_auto_cleaning_interval', { seconds: Math.round(value * multipliers[unit]) })
      .catch((e) => toast(e.message, 'error'));
  });

  document.getElementById('target-co2').addEventListener('input', renderCalibrationChecklist);
  document.getElementById('scd41-calibration-drift').addEventListener('change', renderCalibrationChecklist);

  document.getElementById('scd41-calibration-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submitCommand('scd41_force_calibration', {
      target_co2: Number(document.getElementById('target-co2').value),
      confirmed: document.getElementById('scd41-calibration-confirm').checked,
      allow_large_offset: document.getElementById('scd41-calibration-drift').checked,
      persist: document.getElementById('scd41-calibration-persist').checked,
    }).catch((e) => toast(e.message, 'error'));
  });

  document.getElementById('scd41-asc-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submitCommand('scd41_set_asc', {
      enabled: document.getElementById('scd41-asc-enabled').checked,
      persist: document.getElementById('scd41-asc-persist').checked,
    }).catch((e) => toast(e.message, 'error'));
  });

  document.getElementById('delete-history-button').addEventListener('click', () => {
    deleteHistory().catch((e) => toast(e.message, 'error'));
  });
}

function installRefreshLoop() {
  window.setInterval(() => {
    refreshSummary().catch(() => renderStatusStrip(['Station unreachable']));
  }, 10000);
  window.setInterval(() => {
    if (activeTab === 'history' && range.mode === 'preset') {
      refreshHistory().catch(() => { /* toast on manual actions only */ });
    }
    if (activeTab === 'live') {
      reloadPreview();
      refreshSparklines();
    }
    if (activeTab === 'diagnostics') refreshDiagnostics().catch(() => { /* quiet */ });
  }, 60000);
  // Phones background the tab and browsers throttle intervals; coming back
  // must show now, not a minute ago.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshAll().catch(() => {});
  });
}

window.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  startClock();
  ['chart-temp', 'chart-humid', 'chart-co2', 'chart-aqi', 'chart-pm25', 'chart-pm10'].forEach(installChartHover);
  upgradeSelects();
  installActions();
  installRefreshLoop();
  const initialTab = location.hash.slice(1);
  if (TAB_NAMES.includes(initialTab) && initialTab !== 'live') {
    switchTab(initialTab, false);
  } else {
    reloadPreview();
    refreshSparklines();
  }
  try {
    await refreshSummary();
  } catch (error) {
    toast(error.message, 'error');
  }
});
