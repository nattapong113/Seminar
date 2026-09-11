const api = (path) => fetch(path).then((response) => {
  if (!response.ok) throw new Error(`${path} -> ${response.status}`);
  return response.json();
});

const numberFormat = new Intl.NumberFormat('th-TH');
const compactFormat = new Intl.NumberFormat('th-TH', { notation: 'compact', maximumFractionDigits: 2 });
const charts = {};
const state = { demographics: [] };

const THAI_MONTHS = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.', 'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.'];
const PALETTE = ['#ee745b', '#4b90a9', '#e5b84d', '#5da17d', '#9b7fb5', '#c4826a', '#6aaeb0', '#b0616e'];

const setText = (id, value) => { document.querySelector(`#${id}`).textContent = value; };

function weatherDescription(code) {
  if (code === 0) return 'ท้องฟ้าแจ่มใส';
  if ([1, 2, 3].includes(code)) return 'มีเมฆบางส่วน';
  if ([45, 48].includes(code)) return 'หมอก';
  if ([51, 53, 55, 56, 57].includes(code)) return 'ฝนละออง';
  if ([61, 63, 65, 66, 67, 80, 81, 82].includes(code)) return 'มีฝน';
  if ([95, 96, 99].includes(code)) return 'พายุฝนฟ้าคะนอง';
  return 'สภาพอากาศเปลี่ยนแปลง';
}

function weatherIcon(code) {
  if (code === 0) return '☀';
  if ([1, 2, 3].includes(code)) return '⛅';
  if ([45, 48].includes(code)) return '☁';
  if ([95, 96, 99].includes(code)) return '⛈';
  return '☂';
}

// ---------------------------------------------------------------- KPI

function renderSummary(summary) {
  const tourism = summary.tourism;
  setText('kpi-tourists', numberFormat.format(tourism.tourists_year_total));
  const delta = document.querySelector('#kpi-tourists-delta');
  if (tourism.year_over_year_percent === null) {
    delta.textContent = '—';
  } else {
    const positive = tourism.year_over_year_percent >= 0;
    delta.textContent = `${positive ? '+' : ''}${tourism.year_over_year_percent}%`;
    delta.className = positive ? 'positive' : 'negative';
  }
  setText('kpi-tourists-note', `รวมทั้งปี ${tourism.year} เทียบปีก่อนหน้า`);

  setText('kpi-traffic', compactFormat.format(summary.traffic.vehicles) + ' คัน');
  setText('kpi-traffic-note', `ปี ${summary.traffic.calendar_year} จาก ${summary.traffic.intersections} แยก`);

  setText('kpi-poi', numberFormat.format(summary.poi.total));
  setText('kpi-poi-note', `${summary.poi.categories} ประเภท จาก OpenStreetMap`);

  setText('kpi-population', numberFormat.format(summary.population.population));
  setText('kpi-population-note', `ข้อมูลปี ${summary.population.calendar_year}`);

  const holiday = summary.next_holiday;
  setText('holiday-pill', holiday ? `วันหยุดถัดไป ${holiday.date} · ${holiday.local_name || holiday.name}` : 'ไม่มีวันหยุดในฐานข้อมูล');
  setText('map-zone-count', `${summary.geocode_coverage.geocoded}/${summary.geocode_coverage.total}`);
  setText('map-poi-count', numberFormat.format(summary.poi.total));
  setText('stream-status', `นำเข้าแล้ว ${numberFormat.format(summary.poi.total)} สถานที่ · ${summary.geocode_coverage.total} แยก`);
}

function renderRanges(trends, traffic) {
  if (trends.length) {
    const first = trends[0];
    const last = trends[trends.length - 1];
    setText('tourism-range', `${THAI_MONTHS[first.month_id - 1]} ${first.calendar_year} — ${THAI_MONTHS[last.month_id - 1]} ${last.calendar_year}`);
  }
  if (traffic.length) {
    const first = traffic[0];
    const last = traffic[traffic.length - 1];
    setText('traffic-range', `${THAI_MONTHS[first.month_id - 1]} ${first.calendar_year} — ${THAI_MONTHS[last.month_id - 1]} ${last.calendar_year}`);
  }
}

// ---------------------------------------------------------------- แผนที่

function renderMap(zones, points) {
  const located = zones.filter((zone) => zone.latitude !== null && zone.longitude !== null);
  const maxVolume = Math.max(...located.map((zone) => zone.vehicle_volume), 1);

  if (window.poiHeat) window.map.removeLayer(window.poiHeat);
  // ค่า max สูงกว่า 1 เพราะจุดธุรกิจกระจุกกันมากบริเวณชายหาด ถ้าใช้ค่าเริ่มต้นภาพจะอิ่มตัวเป็นสีแดงทั้งเมือง
  window.poiHeat = L.heatLayer(points.map((point) => [point.latitude, point.longitude, 1]), {
    radius: 16, blur: 20, max: 5, minOpacity: 0.3, maxZoom: 17,
    gradient: { 0.25: '#4b90a9', 0.5: '#5da17d', 0.7: '#e5b84d', 0.88: '#ee745b', 1: '#a8301c' },
  });

  if (window.trafficLayer) window.map.removeLayer(window.trafficLayer);
  window.trafficLayer = L.layerGroup(located.map((zone) => {
    const intensity = zone.vehicle_volume / maxVolume;
    const color = intensity > 0.66 ? '#c33f2c' : intensity > 0.33 ? '#e5b84d' : '#4b90a9';
    const nearby = zone.poi_nearby_by_category
      ? Object.entries(zone.poi_nearby_by_category).map(([key, value]) => `${key} ${value}`).join(' · ')
      : '';
    return L.circleMarker([zone.latitude, zone.longitude], {
      radius: 7 + intensity * 17, color, fillColor: color, fillOpacity: 0.45, weight: 2,
    }).bindPopup(
      `<strong>${zone.intersection_name}</strong><br>` +
      `ปริมาณรถสะสม ${numberFormat.format(zone.vehicle_volume)} คัน<br>` +
      `ธุรกิจท่องเที่ยวรอบแยก ${numberFormat.format(zone.poi_nearby)} แห่ง<br>` +
      (nearby ? `<small>${nearby}</small><br>` : '') +
      `<small>พิกัดจาก OSM: ${zone.geocode_method === 'osm_junction_name' ? 'โหนดแยกที่มีชื่อ' : 'จุดตัดถนน ' + (zone.geocode_detail || '')}</small>`
    );
  }));

  applyLayerVisibility();
}

function applyLayerVisibility() {
  const showPoi = document.querySelector('#layer-poi').checked;
  const showTraffic = document.querySelector('#layer-traffic').checked;
  if (window.poiHeat) showPoi ? window.poiHeat.addTo(window.map) : window.map.removeLayer(window.poiHeat);
  if (window.trafficLayer) showTraffic ? window.trafficLayer.addTo(window.map) : window.map.removeLayer(window.trafficLayer);
}

// ---------------------------------------------------------------- อันดับ / คำแนะนำ

function renderRanking(zones) {
  const top = zones.slice(0, 8);
  const maxVolume = top[0]?.vehicle_volume || 1;
  document.querySelector('#ranking-list').innerHTML = top.map((zone, index) => `
    <div class="ranking-item">
      <span class="rank">${String(index + 1).padStart(2, '0')}</span>
      <div>
        <div class="rank-name">${zone.intersection_name}</div>
        <div class="rank-bar"><i style="width:${Math.max(8, zone.vehicle_volume / maxVolume * 100)}%"></i></div>
      </div>
      <div class="rank-value">${compactFormat.format(zone.vehicle_volume)}<span class="rank-unit">คัน สะสม</span></div>
    </div>`).join('');
}

function renderRecommendations(items) {
  document.querySelector('#recommendation-list').innerHTML = items.map((item) => `
    <div class="recommendation-item">
      <span class="signal">${item.signal}</span>
      <strong>${item.title}</strong>
      <p>${item.detail}</p>
      <cite>${item.source}</cite>
    </div>`).join('');
}

// ---------------------------------------------------------------- กราฟ

function renderTrendChart(trends, traffic) {
  const trafficByDate = new Map(traffic.map((row) => [row.date, row.vehicle_volume]));
  const labels = trends.map((row) => `${THAI_MONTHS[row.month_id - 1]} ${String(row.calendar_year).slice(2)}`);
  charts.trend?.destroy();
  charts.trend = new Chart(document.querySelector('#trend-chart'), {
    data: {
      labels,
      datasets: [
        {
          type: 'line', label: 'นักท่องเที่ยวลงเกาะล้าน (คน)', yAxisID: 'y',
          data: trends.map((row) => row.visitors),
          borderColor: '#ee745b', backgroundColor: '#ee745b18', fill: true, tension: 0.35,
          pointRadius: 2, pointBackgroundColor: '#ee745b', borderWidth: 2,
        },
        {
          type: 'bar', label: 'ปริมาณรถทั้งเมือง (คัน)', yAxisID: 'y1',
          data: trends.map((row) => trafficByDate.get(row.date) ?? null),
          backgroundColor: '#4b90a955', borderColor: '#4b90a9', borderWidth: 1,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { font: { family: 'IBM Plex Sans Thai', size: 10 }, boxWidth: 10 } },
        tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${numberFormat.format(context.parsed.y)}` } },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 9 }, maxRotation: 0, autoSkipPadding: 12 } },
        y: { position: 'left', grid: { color: '#edf1ef' }, ticks: { color: '#ee745b', font: { family: 'DM Mono', size: 9 }, callback: (value) => compactFormat.format(value) } },
        y1: { position: 'right', grid: { display: false }, ticks: { color: '#4b90a9', font: { family: 'DM Mono', size: 9 }, callback: (value) => compactFormat.format(value) } },
      },
    },
  });
}

function renderPoiChart(categories) {
  const top = categories.slice(0, 8);
  charts.poi?.destroy();
  charts.poi = new Chart(document.querySelector('#poi-chart'), {
    type: 'bar',
    data: {
      labels: top.map((row) => row.category),
      datasets: [{ data: top.map((row) => row.total), backgroundColor: PALETTE, borderWidth: 0 }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (context) => `${numberFormat.format(context.parsed.x)} แห่ง` } } },
      scales: {
        x: { grid: { color: '#edf1ef' }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 9 } } },
        y: { grid: { display: false }, ticks: { color: '#617078', font: { family: 'DM Mono', size: 10 } } },
      },
    },
  });
}

function renderDemographicChart(category) {
  const latestYear = Math.max(...state.demographics.map((row) => row.calendar_year));
  const rows = state.demographics.filter((row) => row.category === category && row.calendar_year === latestYear);
  charts.demographic?.destroy();
  charts.demographic = new Chart(document.querySelector('#demographic-chart'), {
    type: 'bar',
    data: {
      labels: rows.map((row) => row.segment),
      datasets: [{ data: rows.map((row) => row.population), backgroundColor: PALETTE, borderWidth: 0 }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        title: { display: true, text: `ข้อมูลปี ${latestYear}`, color: '#9aa5a7', font: { family: 'DM Mono', size: 10 } },
        tooltip: { callbacks: { label: (context) => `${numberFormat.format(context.parsed.x)} คน` } },
      },
      scales: {
        x: { grid: { color: '#edf1ef' }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 9 }, callback: (value) => compactFormat.format(value) } },
        y: { grid: { display: false }, ticks: { color: '#617078', font: { family: 'IBM Plex Sans Thai', size: 10 } } },
      },
    },
  });
}

function renderDemographics(rows) {
  state.demographics = rows;
  const categories = [...new Set(rows.map((row) => row.category))];
  const select = document.querySelector('#demographic-category');
  select.innerHTML = categories.map((category) => `<option value="${category}">${category}</option>`).join('');
  select.addEventListener('change', (event) => renderDemographicChart(event.target.value));
  if (categories.length) renderDemographicChart(categories[0]);
}

// ---------------------------------------------------------------- อากาศ / วันหยุด / แหล่งข้อมูล

function renderLiveWeather(weather) {
  document.querySelector('#weather-live').textContent =
    `${weather.location} ${weather.temperature_c}°C · ${weatherDescription(weather.weather_code)} · ฝน ${weather.precipitation_mm} มม. · ความชื้น ${weather.humidity_percent}%`;
}

function renderForecast(forecast) {
  document.querySelector('#forecast-strip').innerHTML = forecast.map((day) => {
    const date = new Date(day.date);
    return `
      <div class="forecast-day${day.holiday ? ' is-holiday' : ''}">
        <span class="forecast-date">${date.getDate()} ${THAI_MONTHS[date.getMonth()]}</span>
        <span class="forecast-icon">${weatherIcon(day.weather_code)}</span>
        <span class="forecast-temp">${Math.round(day.temperature_max_c)}°</span>
        <span class="forecast-rain">${day.precipitation_probability_percent ?? 0}%</span>
      </div>`;
  }).join('');
}

function renderHolidays(holidays) {
  document.querySelector('#holiday-list').innerHTML = holidays.map((holiday) => `
    <div class="holiday-item">
      <span class="holiday-date">${holiday.date}</span>
      <span class="holiday-name">${holiday.local_name || holiday.name}</span>
      <span class="holiday-away">อีก ${holiday.days_away} วัน</span>
    </div>`).join('');
}

function renderSources(sources) {
  document.querySelector('#source-list').innerHTML = sources.map((source) => `
    <div class="source-item">
      <div>
        <strong>${source.name}</strong>
        <span>${source.endpoint || 'ไฟล์ CSV จาก data.pattaya.go.th'}</span>
      </div>
      <div class="source-meta">
        <b>${source.records ? numberFormat.format(source.records) : '—'}</b>
        <span>${source.last_imported_at ? source.last_imported_at.slice(0, 10) : 'ยังไม่นำเข้า'}</span>
      </div>
    </div>`).join('');
}

// ---------------------------------------------------------------- โหลดทั้งหมด

async function loadDashboard() {
  const [summary, zones, trends, traffic, recommendations, points, categories, demographics, upcoming, sources] =
    await Promise.all([
      api('/api/summary'),
      api('/api/zones'),
      api('/api/trends'),
      api('/api/traffic/trends'),
      api('/api/recommendations'),
      api('/api/poi?limit=3000'),
      api('/api/poi/categories'),
      api('/api/demographics'),
      api('/api/holidays/upcoming?limit=4'),
      api('/api/sources'),
    ]);

  renderSummary(summary);
  renderRanges(trends, traffic);
  renderMap(zones, points);
  renderRanking(zones);
  renderTrendChart(trends, traffic);
  renderRecommendations(recommendations);
  renderPoiChart(categories);
  renderDemographics(demographics);
  renderHolidays(upcoming);
  renderSources(sources);

  // สภาพอากาศเรียก API ภายนอก แยกออกมาเพื่อไม่ให้ทั้งแดชบอร์ดพังถ้าเน็ตมีปัญหา
  api('/api/weather/live').then(renderLiveWeather).catch(() => {
    document.querySelector('#weather-live').textContent = 'เชื่อมต่อ Open-Meteo ไม่ได้';
  });
  api('/api/weather/forecast?days=7').then(renderForecast).catch(() => {
    document.querySelector('#forecast-strip').innerHTML = '<div class="loading">เชื่อมต่อพยากรณ์อากาศไม่ได้</div>';
  });
}

window.map = L.map('map', { zoomControl: false }).setView([12.933, 100.885], 13);
L.control.zoom({ position: 'bottomright' }).addTo(window.map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap', maxZoom: 19,
}).addTo(window.map);

document.querySelector('#layer-poi').addEventListener('change', applyLayerVisibility);
document.querySelector('#layer-traffic').addEventListener('change', applyLayerVisibility);

document.querySelectorAll('.nav-item').forEach((item) => {
  item.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach((other) => other.classList.remove('active'));
    item.classList.add('active');
  });
});

document.querySelector('#export-btn').addEventListener('click', async () => {
  const summary = await api('/api/summary');
  const sources = await api('/api/sources');
  const report = [
    'PATTAYA SMART TOURISM — รายงานสรุปผู้บริหาร',
    `สร้างเมื่อ: ${new Date().toLocaleString('th-TH')}`,
    '',
    `นักท่องเที่ยวลงเกาะล้าน ปี ${summary.tourism.year}: ${numberFormat.format(summary.tourism.tourists_year_total)} คน (${summary.tourism.year_over_year_percent}% เทียบปีก่อน)`,
    `ปริมาณรถ ปี ${summary.traffic.calendar_year}: ${numberFormat.format(summary.traffic.vehicles)} คัน จาก ${summary.traffic.intersections} แยก`,
    `สถานที่ท่องเที่ยว/ธุรกิจ: ${numberFormat.format(summary.poi.total)} แห่ง ${summary.poi.categories} ประเภท`,
    `ประชากรแฝง ปี ${summary.population.calendar_year}: ${numberFormat.format(summary.population.population)} คน`,
    `แยกที่มีพิกัด: ${summary.geocode_coverage.geocoded}/${summary.geocode_coverage.total}`,
    '',
    'แหล่งข้อมูล:',
    ...sources.map((source) => `- ${source.name} (${source.records || 0} รายการ, นำเข้า ${source.last_imported_at || '-'})`),
  ].join('\n');
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([report], { type: 'text/plain;charset=utf-8' }));
  link.download = 'pattaya-tourism-report.txt';
  link.click();
  URL.revokeObjectURL(link.href);
});

loadDashboard().catch((error) => {
  console.error('Dashboard loading failed:', error);
  document.querySelector('#stream-status').textContent = 'โหลดข้อมูลไม่สำเร็จ';
});
