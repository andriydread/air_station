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
  window.setTimeout(refreshSummary, 3000);
  // A display command's visible effect is the panel redraw; refresh the
  // preview once the collector has had time to render.
  if (command.startsWith('display_')) window.setTimeout(reloadPreview, 8000);
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

let activeTab = 'live';

function switchTab(name) {
  activeTab = name;
  document.querySelectorAll('.tab-button').forEach((button) => {
    button.classList.toggle('active', button.dataset.tab === name);
  });
  document.querySelectorAll('.tab').forEach((section) => {
    section.classList.toggle('active', section.id === `tab-${name}`);
  });
  if (name === 'history') refreshHistory().catch((e) => toast(e.message, 'error'));
  if (name === 'diagnostics') refreshDiagnostics().catch((e) => toast(e.message, 'error'));
  if (name === 'live') {
    reloadPreview();
    refreshSparklines();
  }
}

// ---------------------------------------------------------------------------
// Live tab
// ---------------------------------------------------------------------------

let lastSummary = null;

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
  const ages = live.ages || {};
  const aqi = summary.aqi || {};
  const collector = summary.collector_status?.value || {};
  const maxAge = collector.measurement_max_age_seconds || 45;
  const health = sensorHealthSummary(collector);
  const network = collector.sensors?.network || {};
  const power = collector.sensors?.power || {};
  const calibration = summary.scd41_last_calibration?.value || {};

  for (const metric of ['co2', 'temp', 'humid', 'pm25', 'pm10', 'tps']) {
    document.getElementById(`metric-${metric}`).textContent = metricFormats[metric](metrics[metric]);
    setBadge(metric, ages[metric], maxAge);
  }
  document.getElementById('metric-tps-note').textContent = describeTps(metrics.tps);
  document.getElementById('metric-aqi').textContent = aqi.value == null ? '--' : String(aqi.value);
  document.getElementById('metric-aqi-label').textContent = aqi.category || '--';
  setBadge('aqi', ages.pm25, maxAge);
  document.getElementById('metric-co2-label').textContent = aqi.co2_category || '--';

  document.getElementById('sample-age').textContent =
    metrics.timestamp ? `Last sample: ${formatTimestamp(metrics.timestamp)}` : 'No samples yet';

  const problems = [];
  if (!collector.running) problems.push('Collector stopped');
  if (!health.ok && health.headline !== '--') problems.push(health.headline);
  if (network.healthy === false) problems.push('Network offline');
  if (power.available !== false && power.healthy === false) problems.push('Power issue');
  renderStatusStrip(problems);

  // Diagnostics-side details rendered from the same summary
  document.getElementById('network-interface').textContent = network.interface || '--';
  document.getElementById('network-status').textContent =
    `healthy=${network.healthy ? 'yes' : 'no'} | operstate=${network.operstate || '--'} | carrier=${network.carrier || '--'}`;
  document.getElementById('network-signal').textContent =
    network.signal_level_dbm == null ? '--' : `${network.signal_level_dbm} dBm`;
  document.getElementById('network-last-success').textContent = formatTimestamp(network.last_success_at);
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
  document.getElementById('scd41-recent-samples').textContent =
    String(collector.scd41_recent_valid_samples ?? '--');
  document.getElementById('database-path').textContent = collector.database_path || '--';
  document.getElementById('collector-log-file').textContent = collector.log_file || '--';
  document.getElementById('scd41-asc-enabled').checked = !!collector.scd41_asc_enabled;
  document.getElementById('scd41-asc-state').textContent =
    collector.scd41_asc_enabled == null ? '--' : (collector.scd41_asc_enabled ? 'on' : 'off');

  const weather = summary.latest_weather?.value || {};
  document.getElementById('weather-updated').textContent =
    `Updated: ${formatTimestamp(summary.latest_weather?.updated_at)}`;
  renderWeather(weather);
  renderCommands(summary.recent_commands || []);
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
  }
}

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
  const pad = 2;
  const xs = points.map((row) => row.timestamp_ts);
  const ys = points.map((row) => row[key]);
  const xMin = Math.min(...xs);
  const xSpan = (Math.max(...xs) - xMin) || 1;
  const yMin = Math.min(...ys);
  const ySpan = (Math.max(...ys) - yMin) || 1;
  const line = points.map((row) => {
    const x = pad + ((row.timestamp_ts - xMin) / xSpan) * (width - 2 * pad);
    const y = pad + (1 - ((row[key] - yMin) / ySpan)) * (height - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  svg.innerHTML = `<polyline fill="none" stroke="currentColor" stroke-width="2" points="${line}"></polyline>`;
}

function reloadPreview() {
  const image = document.getElementById('display-preview');
  const note = document.getElementById('preview-note');
  const probe = new Image();
  probe.onload = () => {
    image.src = probe.src;
    image.hidden = false;
    note.hidden = true;
  };
  probe.onerror = () => {
    image.hidden = true;
    note.hidden = false;
  };
  probe.src = `/api/display-preview.png?t=${Date.now()}`;
}

// ---------------------------------------------------------------------------
// History tab
// ---------------------------------------------------------------------------

let range = { mode: 'preset', hours: 24 };
let lastHistoryRows = null;

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

async function refreshHistory() {
  const data = await fetchJson(`/api/history?${rangeQuery()}`);
  lastHistoryRows = data.rows || [];
  renderAllCharts(lastHistoryRows);
  renderStats(data.stats || {}, data.from_ts, data.to_ts);
  document.getElementById('export-csv').href = `/api/export.csv?${rangeQuery()}`;
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
    row.innerHTML = `<td>${escapeHtml(label)}</td><td>${format(entry.min)}</td><td>${format(entry.avg)}</td><td>${format(entry.max)}</td>`;
    body.appendChild(row);
  }
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

  const coordinates = points.map((row) => ({
    x: padding.left + ((row.timestamp_ts - xMin) / (xMax - xMin)) * (width - padding.left - padding.right),
    y: padding.top + (height - padding.top - padding.bottom) * (1 - ((row[key] - yBounds.min) / yRange)),
    row,
  }));

  const polyline = coordinates.map((point) => `${point.x},${point.y}`).join(' ');
  const horizontalGrid = computeTicks(yBounds.min, yBounds.max, 5).map((tick) => {
    const y = padding.top + (height - padding.top - padding.bottom) * (1 - ((tick - yBounds.min) / yRange));
    return `
      <line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="${colors.chartGrid}" stroke-dasharray="4 4" />
      <text x="8" y="${y + 4}" fill="${colors.chartLabel}" font-size="12">${tick.toFixed(1)}</text>`;
  }).join('');
  const verticalTicks = computeTicks(xMin, xMax, 5).map((tick) => {
    const x = padding.left + ((tick - xMin) / (xMax - xMin)) * (width - padding.left - padding.right);
    return `
      <line x1="${x}" y1="${padding.top}" x2="${x}" y2="${height - padding.bottom}" stroke="${colors.chartGridSoft}" />
      <text x="${x}" y="${height - 12}" text-anchor="middle" fill="${colors.chartLabel}" font-size="12">${formatAxisTimestamp(tick, span)}</text>`;
  }).join('');

  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="transparent"></rect>
    ${horizontalGrid}
    ${verticalTicks}
    <line id="crosshair-${svgId}" x1="0" y1="${padding.top}" x2="0" y2="${height - padding.bottom}" stroke="${config.color}" stroke-width="1.5" stroke-dasharray="4 4" opacity="0"></line>
    <circle id="focus-${svgId}" cx="0" cy="0" r="5" fill="${config.color}" stroke="${colors.paper}" stroke-width="2" opacity="0"></circle>
    <polyline fill="none" stroke="${config.color}" stroke-width="3" points="${polyline}"></polyline>`;

  chartState.set(svgId, { coordinates, formatValue: config.formatter, width, height });
}

function installChartHover(svgId) {
  const svg = document.getElementById(svgId);
  const tooltip = document.getElementById(`tooltip-${svgId}`);

  svg.addEventListener('mousemove', (event) => {
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
  });

  svg.addEventListener('mouseleave', () => {
    const crosshair = document.getElementById(`crosshair-${svgId}`);
    const focus = document.getElementById(`focus-${svgId}`);
    if (crosshair) crosshair.setAttribute('opacity', '0');
    if (focus) focus.setAttribute('opacity', '0');
    tooltip.style.opacity = '0';
  });
}

// ---------------------------------------------------------------------------
// Diagnostics tab
// ---------------------------------------------------------------------------

async function refreshDiagnostics() {
  const level = document.getElementById('event-level').value;
  const source = document.getElementById('event-source').value;
  const query = new URLSearchParams({ limit: 100 });
  if (level) query.set('level', level);
  if (source) query.set('source', source);
  const [events, flags] = await Promise.all([
    fetchJson(`/api/events?${query}`),
    fetchJson('/api/flags?limit=30'),
  ]);
  renderEvents(events.events || []);
  renderFlagged(flags.flagged || []);
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
      <p class="event-time">${escapeHtml(formatTimestamp(event.created_at))}</p>
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
      <p class="event-time">${escapeHtml(formatTimestamp(item.timestamp))}</p>`;
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
        <span class="command-status-${escapeHtml(command.status)}">${escapeHtml(command.status)}</span>
      </header>
      <p class="event-time">${escapeHtml(formatTimestamp(command.created_at))}</p>
      ${result}`;
    list.appendChild(item);
  }
}

// ---------------------------------------------------------------------------
// Custom dropdowns
// ---------------------------------------------------------------------------
// Native <select> popups commit on mouse-release on the operator's system
// (press-and-hold semantics), which made the menus unusable. The native
// select stays in the DOM as the value store — existing change listeners
// and .value reads keep working — and if this upgrade ever fails the page
// falls back to the native control.

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
    toggle.textContent = selected ? labelOf(selected) : '--';
  };

  let focusIndex = -1;

  const highlight = () => {
    menu.querySelectorAll('li').forEach((item, index) => {
      item.classList.toggle('focused', index === focusIndex);
    });
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
      item.addEventListener('click', () => choose(index));
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
  document.querySelectorAll('.hint[data-hint]').forEach((hint) => {
    hint.setAttribute('aria-label', hint.dataset.hint);
  });

  document.querySelectorAll('.tab-button').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });

  document.querySelectorAll('[data-command]').forEach((button) => {
    button.addEventListener('click', () => {
      submitCommand(button.dataset.command).catch((e) => toast(e.message, 'error'));
    });
  });

  document.querySelectorAll('[data-system]').forEach((button) => {
    button.addEventListener('click', () => {
      const command = button.dataset.system;
      if (!window.confirm(systemConfirmations[command])) return;
      submitCommand(command, { confirmed: true }).catch((e) => toast(e.message, 'error'));
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

  document.getElementById('preview-reload').addEventListener('click', reloadPreview);
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

  document.getElementById('scd41-calibration-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submitCommand('scd41_force_calibration', {
      target_co2: Number(document.getElementById('target-co2').value),
      confirmed: document.getElementById('scd41-calibration-confirm').checked,
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
}

window.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  startClock();
  ['chart-temp', 'chart-humid', 'chart-co2', 'chart-aqi', 'chart-pm25', 'chart-pm10'].forEach(installChartHover);
  upgradeSelects();
  installActions();
  installRefreshLoop();
  reloadPreview();
  refreshSparklines();
  try {
    await refreshSummary();
  } catch (error) {
    toast(error.message, 'error');
  }
});
