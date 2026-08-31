const metricFormats = {
  co2: (value) => value == null ? '--' : `${Math.round(value)} ppm`,
  temp: (value) => value == null ? '--' : `${value.toFixed(1)} C`,
  humid: (value) => value == null ? '--' : `${value.toFixed(1)} %`,
  pm25: (value) => value == null ? '--' : `${value.toFixed(2)} ug/m3`,
  pm10: (value) => value == null ? '--' : `${value.toFixed(2)} ug/m3`,
  tps: (value) => value == null ? '--' : `${value.toFixed(2)} um`,
};

const chartState = new Map();
const weatherIconMap = {
  0: 'sun.png',
  1: 'sun.png',
  2: 'partly_cloudy.png',
  3: 'cloud.png',
  45: 'fog.png',
  48: 'fog.png',
  51: 'rain.png',
  53: 'rain.png',
  55: 'rain.png',
  56: 'rain.png',
  57: 'rain.png',
  61: 'rain.png',
  63: 'rain.png',
  65: 'rain.png',
  66: 'rain.png',
  67: 'rain.png',
  71: 'snow.png',
  73: 'snow.png',
  75: 'snow.png',
  77: 'snow.png',
  80: 'rain.png',
  81: 'rain.png',
  82: 'rain.png',
  85: 'snow.png',
  86: 'snow.png',
  95: 'storm.png',
  96: 'storm.png',
  99: 'storm.png',
};
const chartConfigs = {
  temp: {
    color: '#b85c38',
    formatter: (row) => `${row.temp.toFixed(1)} C`,
    bounds: (values) => {
      const min = Math.min(...values);
      const max = Math.max(...values);
      return {
        min: min < 0 ? Math.floor(min - 1) : 0,
        max: max > 40 ? Math.ceil(max + 1) : 40,
      };
    },
  },
  humid: {
    color: '#2b6f9e',
    formatter: (row) => `${row.humid.toFixed(1)} %`,
    bounds: () => ({ min: 0, max: 100 }),
  },
  co2: {
    color: '#1f5c4a',
    formatter: (row) => `${Math.round(row.co2)} ppm`,
    bounds: (values) => dynamicFromZero(values, 100),
  },
  aqi: {
    color: '#9e6f00',
    formatter: (row) => `${Math.round(row.aqi)}`,
    bounds: (values) => dynamicFromZero(values, 25),
  },
  pm25: {
    color: '#5b4b8a',
    formatter: (row) => `${row.pm25.toFixed(2)} ug/m3`,
    bounds: (values) => dynamicFromZero(values, 5),
  },
  pm10: {
    color: '#6f4a2a',
    formatter: (row) => `${row.pm10.toFixed(2)} ug/m3`,
    bounds: (values) => dynamicFromZero(values, 5),
  },
};
let selectedHours = 24;
let lastSummary = null;
let lastHistoryRows = null;

function dynamicFromZero(values, minSpan) {
  const rawMax = Math.max(...values, 0);
  const paddedMax = rawMax <= 0 ? minSpan : Math.ceil((rawMax * 1.1) / minSpan) * minSpan;
  return { min: 0, max: Math.max(minSpan, paddedMax) };
}

function formatTimestamp(value) {
  if (!value) {
    return '--';
  }
  const date = new Date(value);
  const two = (n) => String(n).padStart(2, '0');
  return `${two(date.getHours())}:${two(date.getMinutes())} ${two(date.getDate())}-${two(date.getMonth() + 1)}-${date.getFullYear()}`;
}

function formatAxisTimestampFromSeconds(seconds) {
  const date = new Date(seconds * 1000);
  const two = (n) => String(n).padStart(2, '0');
  return `${two(date.getHours())}:${two(date.getMinutes())}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function themeColors() {
  const styles = getComputedStyle(document.documentElement);
  return {
    chartGrid: styles.getPropertyValue('--chart-grid').trim(),
    chartGridSoft: styles.getPropertyValue('--chart-grid-soft').trim(),
    chartLabel: styles.getPropertyValue('--chart-label').trim(),
    paper: styles.getPropertyValue('--paper').trim(),
  };
}

function getLiveMetrics(summary) {
  return summary.latest_measurements?.value || summary.latest_measurement || {};
}

function sensorHealthSummary(collector) {
  const sensors = collector.sensors || {};
  const entries = ['scd41', 'sht41', 'sps30'].map((key) => sensors[key]).filter(Boolean);
  if (!entries.length) {
    return { headline: '--', detail: '--', pill: 'Sensors: --' };
  }

  const unhealthy = entries.filter((entry) => !entry.healthy);
  if (!unhealthy.length) {
    return {
      headline: 'Healthy',
      detail: 'SCD41, SHT41, and SPS30 are reporting normally.',
      pill: 'Sensors: healthy',
    };
  }

  const primary = unhealthy[0].last_error || 'One or more sensors need attention.';
  return {
    headline: `${unhealthy.length} issue${unhealthy.length === 1 ? '' : 's'}`,
    detail: primary,
    pill: `Sensors: ${unhealthy.length} issue${unhealthy.length === 1 ? '' : 's'}`,
  };
}

function formatInterval(seconds) {
  if (seconds == null) {
    return '--';
  }
  if (seconds === 0) {
    return 'disabled';
  }
  if (seconds % 86400 === 0) {
    return `${seconds / 86400} day(s)`;
  }
  if (seconds % 3600 === 0) {
    return `${seconds / 3600} hour(s)`;
  }
  if (seconds % 60 === 0) {
    return `${seconds / 60} minute(s)`;
  }
  return `${seconds} second(s)`;
}

function renderSummary(summary) {
  lastSummary = summary;
  const metrics = getLiveMetrics(summary);
  const aqi = summary.aqi || {};  // computed by the backend (single AQI source)
  const collector = summary.collector_status?.value || {};
  const health = sensorHealthSummary(collector);
  const calibration = summary.scd41_last_calibration?.value || {};
  const network = collector.sensors?.network || {};

  document.getElementById('metric-co2').textContent = metricFormats.co2(metrics.co2);
  document.getElementById('metric-temp').textContent = metricFormats.temp(metrics.temp);
  document.getElementById('metric-humid').textContent = metricFormats.humid(metrics.humid);
  document.getElementById('metric-pm25').textContent = metricFormats.pm25(metrics.pm25);
  document.getElementById('metric-pm10').textContent = metricFormats.pm10(metrics.pm10);
  document.getElementById('metric-tps').textContent = metricFormats.tps(metrics.tps);
  document.getElementById('metric-aqi').textContent = aqi.value == null ? '--' : String(aqi.value);
  document.getElementById('metric-aqi-label').textContent = aqi.category || '--';
  document.getElementById('metric-co2-label').textContent = aqi.co2_category || '--';
  document.getElementById('metric-status').textContent = health.headline;
  document.getElementById('metric-status-detail').textContent = health.detail;
  document.getElementById('latest-sample-time').textContent = `Dashboard sample: ${formatTimestamp(metrics.timestamp)}`;

  document.getElementById('collector-running').textContent = `Collector: ${collector.running ? 'running' : 'stopped'}`;
  document.getElementById('collector-asc').textContent = `ASC: ${collector.scd41_asc_enabled ? 'enabled' : 'disabled'}`;
  document.getElementById('collector-health').textContent = health.pill;
  document.getElementById('scd41-asc-enabled').checked = !!collector.scd41_asc_enabled;
  document.getElementById('database-path').textContent = collector.database_path || '--';
  document.getElementById('collector-log-file').textContent = collector.log_file || '--';
  document.getElementById('auto-clean-current').textContent = formatInterval(collector.sps30_auto_cleaning_interval_seconds);
  document.getElementById('scd41-last-calibration').textContent = formatTimestamp(calibration.calibrated_at || collector.sensors?.scd41?.last_calibration_at);
  document.getElementById('scd41-recent-samples').textContent = String(collector.scd41_recent_valid_samples ?? '--');

  document.getElementById('network-interface').textContent = network.interface || '--';
  document.getElementById('network-status').textContent = `healthy=${network.healthy ? 'yes' : 'no'} | operstate=${network.operstate || '--'} | carrier=${network.carrier || '--'}`;
  document.getElementById('network-signal').textContent = network.signal_level_dbm == null ? '--' : `${network.signal_level_dbm} dBm`;
  document.getElementById('network-last-success').textContent = formatTimestamp(network.last_success_at);
  document.getElementById('network-last-error').textContent = network.last_error || '--';

  const weather = summary.latest_weather?.value || {};
  document.getElementById('weather-updated').textContent = `Updated: ${formatTimestamp(summary.latest_weather?.updated_at)}`;
  renderWeather(weather);
  renderCommands(summary.recent_commands || []);
  renderEvents(summary.recent_events || []);
}

function renderWeather(weather) {
  const grid = document.getElementById('forecast-grid');
  grid.innerHTML = '';
  const entries = [weather[1], weather[2], weather[3], weather['1'], weather['2'], weather['3']].filter(Boolean).slice(0, 3);
  if (!entries.length) {
    grid.innerHTML = '<div class="empty-state">No forecast data yet.</div>';
    return;
  }
  for (const block of entries) {
    const card = document.createElement('article');
    card.className = 'forecast-card';
    const [windowLabel, maxTemp, minTemp, precip, code] = block;
    const icon = weatherIconMap[code] || 'sun.png';
    const tempText = (maxTemp != null && minTemp != null) ? `${maxTemp} / ${minTemp} C` : '-- / -- C';
    const rainText = precip != null ? `${precip}%` : '--%';
    card.innerHTML = `
      <p class="forecast-window">${escapeHtml(windowLabel)}</p>
      <div class="forecast-body">
        <img class="forecast-icon" src="/assets/icons/${icon}" alt="forecast icon">
        <div class="forecast-stats">
          <p class="forecast-stat">${escapeHtml(tempText)}</p>
          <p class="forecast-stat">Rain: ${escapeHtml(rainText)}</p>
        </div>
      </div>
    `;
    grid.appendChild(card);
  }
}

function prettyJson(value) {
  return JSON.stringify(value || {}, null, 2);
}

function renderCommands(commands) {
  const list = document.getElementById('command-list');
  list.innerHTML = '';
  if (!commands.length) {
    list.innerHTML = '<div class="empty-state">No commands recorded yet.</div>';
    return;
  }
  for (const command of commands) {
    const item = document.createElement('article');
    item.className = 'command-item';
    item.innerHTML = `
      <header>
        <span>${escapeHtml(command.command)}</span>
        <span class="command-status-${escapeHtml(command.status)}">${escapeHtml(command.status)}</span>
      </header>
      <p>Created: ${escapeHtml(formatTimestamp(command.created_at))}</p>
      <p>Payload:</p>
      <pre>${escapeHtml(prettyJson(command.payload))}</pre>
      <p>Result:</p>
      <pre>${escapeHtml(prettyJson(command.result))}</pre>
    `;
    list.appendChild(item);
  }
}

function renderEvents(events) {
  const list = document.getElementById('event-list');
  list.innerHTML = '';
  document.getElementById('events-status').textContent = events.length ? `Showing ${events.length} latest events.` : 'No events recorded yet.';
  if (!events.length) {
    list.innerHTML = '<div class="empty-state">No diagnostics recorded yet.</div>';
    return;
  }
  for (const event of events) {
    const item = document.createElement('article');
    item.className = 'event-item';
    item.innerHTML = `
      <header>
        <span>${escapeHtml(event.source)} / ${escapeHtml(event.event_type)}</span>
        <span class="event-level event-level-${escapeHtml(event.level)}">${escapeHtml(event.level)}</span>
      </header>
      <p>${escapeHtml(event.message)}</p>
      <p>${escapeHtml(formatTimestamp(event.created_at))}</p>
      <pre>${escapeHtml(prettyJson(event.details))}</pre>
    `;
    list.appendChild(item);
  }
}

function computeTicks(min, max, count) {
  if (count <= 1) {
    return [min];
  }
  const step = (max - min) / (count - 1);
  return Array.from({ length: count }, (_, index) => min + step * index);
}

function renderLineChart(svgId, rows, key, config) {
  const svg = document.getElementById(svgId);
  const tooltip = document.getElementById(`tooltip-${svgId}`);
  const rowsWithTime = rows.filter((row) => row.timestamp_ts != null);
  const points = rowsWithTime.filter((row) => row[key] != null);
  const colors = themeColors();

  if (!points.length) {
    svg.innerHTML = `<text x="24" y="40" fill="${colors.chartLabel}" font-size="16">No data yet</text>`;
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

  const coordinates = points.map((row) => {
    const x = padding.left + ((row.timestamp_ts - xMin) / (xMax - xMin)) * (width - padding.left - padding.right);
    const y = padding.top + (height - padding.top - padding.bottom) * (1 - ((row[key] - yBounds.min) / yRange));
    return { x, y, row };
  });

  const polyline = coordinates.map((point) => `${point.x},${point.y}`).join(' ');
  const yTicks = computeTicks(yBounds.min, yBounds.max, 5);
  const xTicks = computeTicks(xMin, xMax, 5);

  const horizontalGrid = yTicks.map((tick) => {
    const y = padding.top + (height - padding.top - padding.bottom) * (1 - ((tick - yBounds.min) / yRange));
    return `
      <line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="${colors.chartGrid}" stroke-dasharray="4 4" />
      <text x="8" y="${y + 4}" fill="${colors.chartLabel}" font-size="12">${tick.toFixed(1)}</text>
    `;
  }).join('');

  const verticalTicks = xTicks.map((tick) => {
    const x = padding.left + ((tick - xMin) / (xMax - xMin)) * (width - padding.left - padding.right);
    return `
      <line x1="${x}" y1="${padding.top}" x2="${x}" y2="${height - padding.bottom}" stroke="${colors.chartGridSoft}" />
      <text x="${x}" y="${height - 12}" text-anchor="middle" fill="${colors.chartLabel}" font-size="12">${formatAxisTimestampFromSeconds(tick)}</text>
    `;
  }).join('');

  svg.innerHTML = `
    <rect x="0" y="0" width="${width}" height="${height}" fill="transparent"></rect>
    ${horizontalGrid}
    ${verticalTicks}
    <line id="crosshair-${svgId}" x1="0" y1="${padding.top}" x2="0" y2="${height - padding.bottom}" stroke="${config.color}" stroke-width="1.5" stroke-dasharray="4 4" opacity="0"></line>
    <circle id="focus-${svgId}" cx="0" cy="0" r="5" fill="${config.color}" stroke="${colors.paper}" stroke-width="2" opacity="0"></circle>
    <polyline fill="none" stroke="${config.color}" stroke-width="3" points="${polyline}"></polyline>
  `;

  chartState.set(svgId, { coordinates, formatValue: config.formatter, width, height });
}

function installChartHover(svgId) {
  const svg = document.getElementById(svgId);
  const tooltip = document.getElementById(`tooltip-${svgId}`);

  svg.addEventListener('mousemove', (event) => {
    const state = chartState.get(svgId);
    if (!state || !state.coordinates.length) {
      return;
    }
    const rect = svg.getBoundingClientRect();
    const scaleX = state.width / rect.width;
    const cursorX = (event.clientX - rect.left) * scaleX;
    let nearest = state.coordinates[0];
    for (const point of state.coordinates) {
      if (Math.abs(point.x - cursorX) < Math.abs(nearest.x - cursorX)) {
        nearest = point;
      }
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

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  let data = {};
  try {
    data = await response.json();
  } catch (_error) {
    data = {};
  }
  if (!response.ok) {
    throw new Error(data.error || 'Request failed');
  }
  return data;
}

async function fetchSummary() {
  const data = await fetchJson('/api/summary');
  renderSummary(data);
}

function renderAllCharts(rows) {
  renderLineChart('chart-temp', rows, 'temp', chartConfigs.temp);
  renderLineChart('chart-humid', rows, 'humid', chartConfigs.humid);
  renderLineChart('chart-co2', rows, 'co2', chartConfigs.co2);
  renderLineChart('chart-aqi', rows, 'aqi', chartConfigs.aqi);
  renderLineChart('chart-pm25', rows, 'pm25', chartConfigs.pm25);
  renderLineChart('chart-pm10', rows, 'pm10', chartConfigs.pm10);
}

async function fetchHistory() {
  const data = await fetchJson(`/api/history?hours=${selectedHours}`);
  lastHistoryRows = data.rows || [];  // rows carry backend-computed `aqi`
  renderAllCharts(lastHistoryRows);
}

async function submitCommand(command, payload = {}) {
  const data = await fetchJson('/api/commands', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command, payload }),
  });
  document.getElementById('command-status').textContent = `Queued command #${data.id}`;
  await refreshAll();
}

async function deleteHistory() {
  const confirmed = window.confirm('Delete all stored history measurements? This cannot be undone.');
  if (!confirmed) {
    return;
  }

  const data = await fetchJson('/api/history', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: 'delete' }),
  });
  document.getElementById('command-status').textContent = data.status;
  await refreshAll();
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  window.localStorage.setItem('airmonitor-theme', theme);
  document.getElementById('theme-toggle').textContent = theme === 'dark' ? 'Light mode' : 'Dark mode';
  if (lastHistoryRows) {
    renderAllCharts(lastHistoryRows);
  }
}

function initTheme() {
  const preferred = window.localStorage.getItem('airmonitor-theme')
    || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  setTheme(preferred);
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    setTheme(nextTheme);
  });
}

function installActions() {
  document.querySelectorAll('[data-command]').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        await submitCommand(button.dataset.command);
      } catch (error) {
        document.getElementById('command-status').textContent = error.message;
      }
    });
  });

  document.querySelectorAll('.range-switch button').forEach((button) => {
    button.addEventListener('click', async () => {
      selectedHours = Number(button.dataset.hours);
      document.querySelectorAll('.range-switch button').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      try {
        await fetchHistory();
      } catch (error) {
        document.getElementById('command-status').textContent = error.message;
      }
    });
  });

  document.getElementById('sps30-auto-clean-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const value = Number(document.getElementById('auto-clean-value').value);
    const unit = document.getElementById('auto-clean-unit').value;
    const multipliers = { seconds: 1, minutes: 60, hours: 3600, days: 86400 };
    const seconds = Math.round(value * multipliers[unit]);
    try {
      await submitCommand('sps30_set_auto_cleaning_interval', { seconds });
    } catch (error) {
      document.getElementById('command-status').textContent = error.message;
    }
  });

  document.getElementById('scd41-calibration-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const target_co2 = Number(document.getElementById('target-co2').value);
    const confirmed = document.getElementById('scd41-calibration-confirm').checked;
    const persist = document.getElementById('scd41-calibration-persist').checked;
    try {
      await submitCommand('scd41_force_calibration', { target_co2, confirmed, persist });
    } catch (error) {
      document.getElementById('command-status').textContent = error.message;
    }
  });

  document.getElementById('scd41-asc-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const enabled = document.getElementById('scd41-asc-enabled').checked;
    const persist = document.getElementById('scd41-asc-persist').checked;
    try {
      await submitCommand('scd41_set_asc', { enabled, persist });
    } catch (error) {
      document.getElementById('command-status').textContent = error.message;
    }
  });

  document.getElementById('delete-history-button').addEventListener('click', async () => {
    try {
      await deleteHistory();
    } catch (error) {
      document.getElementById('command-status').textContent = error.message;
    }
  });
}

async function refreshAll() {
  await Promise.all([fetchSummary(), fetchHistory()]);
}

function installRefreshLoop() {
  window.setInterval(async () => {
    try {
      await refreshAll();
    } catch (error) {
      document.getElementById('command-status').textContent = error.message;
    }
  }, 10000);
}

window.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  ['chart-temp', 'chart-humid', 'chart-co2', 'chart-aqi', 'chart-pm25', 'chart-pm10'].forEach(installChartHover);
  installActions();
  try {
    await refreshAll();
  } catch (error) {
    document.getElementById('command-status').textContent = error.message;
  }
  installRefreshLoop();
});
