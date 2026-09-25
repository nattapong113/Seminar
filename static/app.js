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
const ACTUAL_COLOR = '#ee745b';
const FORECAST_COLOR = '#1f7fa3';

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
  const showZone = document.querySelector('#layer-zone').checked;
  const showPoi = document.querySelector('#layer-poi').checked;
  const showTraffic = document.querySelector('#layer-traffic').checked;
  if (window.zoneLayer) showZone ? window.zoneLayer.addTo(window.map) : window.map.removeLayer(window.zoneLayer);
  if (window.poiHeat) showPoi ? window.poiHeat.addTo(window.map) : window.map.removeLayer(window.poiHeat);
  if (window.trafficLayer) showTraffic ? window.trafficLayer.addTo(window.map) : window.map.removeLayer(window.trafficLayer);
}

// ---------------------------------------------------------------- โซนย่อย (Micro-Zoning)

// ไล่สีตามความหนาแน่นธุรกิจต่อ ตร.กม. เทียบกับย่านที่หนาแน่นที่สุด
function zoneColor(density, maxDensity) {
  const ratio = maxDensity ? density / maxDensity : 0;
  if (ratio > 0.66) return '#c33f2c';
  if (ratio > 0.33) return '#e5b84d';
  if (ratio > 0.08) return '#5da17d';
  return '#4b90a9';
}

function renderZoneLayer(zones) {
  const maxDensity = Math.max(...zones.map((zone) => zone.poi_density_per_km2), 1);
  if (window.zoneLayer) window.map.removeLayer(window.zoneLayer);
  window.zoneLayer = L.layerGroup(zones.map((zone) => {
    const color = zoneColor(zone.poi_density_per_km2, maxDensity);
    const categories = Object.entries(zone.top_categories)
      .map(([name, count]) => `${name} ${count}`).join(' · ');
    return L.circle([zone.latitude, zone.longitude], {
      // รัศมีของ L.circle เป็นเมตร ส่วน API ส่งมาเป็นกิโลเมตร
      radius: zone.radius_km * 1000,
      color, weight: 2, dashArray: '5 5', fillColor: color, fillOpacity: 0.07,
    }).bindPopup(
      `<strong>${zone.name}</strong><br>` +
      `ธุรกิจท่องเที่ยว ${numberFormat.format(zone.poi_total)} แห่ง ` +
      `(${zone.poi_share_percent}% ของทั้งเมือง)<br>` +
      `ความหนาแน่น ${zone.poi_density_per_km2} แห่ง/ตร.กม.<br>` +
      `ปริมาณรถสะสม ${numberFormat.format(zone.vehicle_volume)} คัน จาก ${zone.intersection_count} แยก<br>` +
      (categories ? `<small>${categories}</small>` : '')
    );
  }));
}

function renderZoneTable(zones) {
  const maxDensity = Math.max(...zones.map((zone) => zone.poi_density_per_km2), 1);
  document.querySelector('#zone-table').innerHTML = zones.map((zone) => {
    const color = zoneColor(zone.poi_density_per_km2, maxDensity);
    return `
    <div class="zone-row">
      <span class="zone-swatch" style="background:${color}"></span>
      <div>
        <div class="zone-name">${zone.name}</div>
        <span class="zone-sub">${zone.intersection_count} แยก · ${zone.dominant_category || 'ไม่มีข้อมูลธุรกิจ'}</span>
        <div class="zone-bar"><i style="width:${Math.max(2, zone.poi_density_per_km2 / maxDensity * 100)}%;background:${color}"></i></div>
      </div>
      <div class="zone-metric">${numberFormat.format(zone.poi_total)}<span>แห่ง (${zone.poi_share_percent}%)</span></div>
      <div class="zone-metric">${zone.poi_density_per_km2}<span>ต่อ ตร.กม.</span></div>
    </div>`;
  }).join('');
}

function renderZoneChart(zones) {
  const maxDensity = Math.max(...zones.map((zone) => zone.poi_density_per_km2), 1);
  charts.zone?.destroy();
  charts.zone = new Chart(document.querySelector('#zone-chart'), {
    type: 'bar',
    data: {
      labels: zones.map((zone) => zone.name),
      datasets: [{
        data: zones.map((zone) => zone.poi_density_per_km2),
        backgroundColor: zones.map((zone) => zoneColor(zone.poi_density_per_km2, maxDensity)),
        borderWidth: 0,
      }],
    },
    options: {
      indexAxis: 'y',
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (item) => `${item.parsed.x} แห่ง/ตร.กม.` } },
      },
      scales: {
        x: { grid: { color: '#eef2f0' }, ticks: { font: { size: 9 } } },
        y: { grid: { display: false }, ticks: { font: { size: 9 } } },
      },
    },
  });
}

function renderMicroZones(data) {
  state.microZones = data;
  const zones = data.zones;
  renderZoneLayer(zones);
  renderZoneTable(zones);
  renderZoneChart(zones);
  const covered = zones.reduce((sum, zone) => sum + zone.poi_total, 0);
  const total = covered + data.outside.poi_total;
  setText('zone-note', `${zones.length} ย่าน · ครอบคลุมธุรกิจ ${(covered / total * 100).toFixed(1)}%`);
  setText('zone-caveats', data.caveats.join(' · '));
  applyLayerVisibility();
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
      ${item.assumption ? `<span class="assumption">${item.assumption}</span>` : ''}
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
      <span class="holiday-name">${holiday.local_name || holiday.name}${
        // วันสำคัญที่ราชการไม่ได้หยุด ต้องแยกให้เห็น เพราะไม่ถูกนับเป็นวันหยุดในการวิเคราะห์
        holiday.holiday_type === 'observance' ? ' <em class="holiday-tag">วันสำคัญ</em>' : ''
      }</span>
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

// ---------------------------------------------------------------- พยากรณ์ / ผลกระทบ

const monthLabel = (month) => `${THAI_MONTHS[Number(month.slice(5, 7)) - 1]} ${String(month.slice(0, 4)).slice(2)}`;

function renderTourismForecast(data) {
  if (data.error) {
    document.querySelector('#forecast-model').textContent = data.error;
    return;
  }
  // แสดงย้อนหลัง 24 เดือนพอให้เห็นรูปฤดูกาล ถ้าใส่ทั้ง 48 เดือนแกนจะแน่นจนอ่านไม่ออก
  const history = data.history.slice(-24);
  const labels = [...history, ...data.forecast].map((row) => monthLabel(row.month));
  const pad = new Array(history.length - 1).fill(null);
  const actual = [...history.map((row) => row.visitors), ...data.forecast.map(() => null)];
  const forecast = [...pad, history[history.length - 1].visitors, ...data.forecast.map((row) => row.visitors)];
  const upper = [...pad, history[history.length - 1].visitors, ...data.forecast.map((row) => row.upper)];
  const lower = [...pad, history[history.length - 1].visitors, ...data.forecast.map((row) => row.lower)];

  charts.forecast?.destroy();
  charts.forecast = new Chart(document.querySelector('#forecast-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'ช่วงประมาณ 95%', data: upper, borderWidth: 0, pointRadius: 0, fill: '+1',
          backgroundColor: '#1f7fa31f', order: 3 },
        { label: 'ขอบล่าง', data: lower, borderWidth: 0, pointRadius: 0, fill: false, order: 3 },
        { label: 'จำนวนจริง', data: actual, borderColor: ACTUAL_COLOR, backgroundColor: ACTUAL_COLOR,
          borderWidth: 2, pointRadius: 0, tension: 0.3, order: 1 },
        { label: 'พยากรณ์', data: forecast, borderColor: FORECAST_COLOR, backgroundColor: FORECAST_COLOR,
          borderWidth: 2, borderDash: [5, 4], pointRadius: 3, tension: 0.3, order: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          labels: {
            font: { family: 'IBM Plex Sans Thai', size: 10 }, boxWidth: 10,
            filter: (item) => item.text !== 'ขอบล่าง',
          },
        },
        tooltip: {
          callbacks: {
            label: (context) => (context.parsed.y === null ? null
              : `${context.dataset.label}: ${numberFormat.format(Math.round(context.parsed.y))} คน`),
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 9 }, maxRotation: 0, autoSkipPadding: 14 } },
        y: { grid: { color: '#edf1ef' }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 9 }, callback: (value) => compactFormat.format(value) } },
      },
    },
  });

  const accuracy = data.accuracy;
  document.querySelector('#forecast-model').textContent = `วิธี: ${data.model.label}`;
  document.querySelector('#forecast-accuracy').innerHTML = `
    <div class="accuracy-item ${accuracy.beats_baseline ? 'win' : 'lose'}">
      <span>คลาดเคลื่อนในปีทดสอบ</span><b>${accuracy.test_mape_percent}%</b>
    </div>
    <div class="accuracy-item"><span>วิธีพื้นฐาน (เดือนเดียวกันปีก่อน)</span><b>${accuracy.seasonal_naive_mape_percent}%</b></div>
    <div class="accuracy-item"><span>ทายล่วงหน้า 1 เดือน</span><b>${accuracy.one_step_mape_percent}%</b></div>
    <div class="accuracy-item"><span>ข้อมูลที่ใช้</span><b>${data.data.months_available} เดือน</b></div>`;

  document.querySelector('#forecast-table').innerHTML = data.forecast.map((row) => `
    <div class="forecast-row">
      <span class="month">${monthLabel(row.month)}</span>
      <b>${numberFormat.format(row.visitors)} คน</b>
      <span class="range">${row.lower === null ? '' : `${compactFormat.format(row.lower)}–${compactFormat.format(row.upper)}`}</span>
    </div>`).join('');

  document.querySelector('#forecast-caveats').textContent = data.caveats.map((text) => `• ${text}`).join('  ');
}

function renderImpact(data) {
  const tiles = [];
  if (data.rain) {
    tiles.push(`
      <div class="impact-tile ${data.rain.significant ? '' : 'is-weak'}">
        <span>ฝนมากกว่าปกติ 100 มม.</span>
        <b>${data.rain.effect_percent > 0 ? '+' : ''}${data.rain.effect_percent}%</b>
        <em>${data.rain.significant ? 'ความสัมพันธ์ชัดเจน' : 'ยังสรุปไม่ได้'} · r = ${data.rain.correlation} · ${data.rain.months_used} เดือน</em>
      </div>`);
  }
  if (data.holidays) {
    tiles.push(`
      <div class="impact-tile ${data.holidays.significant ? '' : 'is-weak'}">
        <span>วันหยุดเพิ่ม 1 วัน</span>
        <b>${data.holidays.effect_percent > 0 ? '+' : ''}${data.holidays.effect_percent}%</b>
        <em>${data.holidays.significant ? 'ความสัมพันธ์ชัดเจน' : 'ยังสรุปไม่ได้'} · r = ${data.holidays.correlation} · ${data.holidays.months_used} เดือน</em>
      </div>`);
  }
  document.querySelector('#impact-tiles').innerHTML = tiles.join('') || '<div class="loading">ยังวิเคราะห์ไม่ได้</div>';

  if (!data.rain) return;
  const points = data.rain.monthly.map((row) => ({ x: row.rain_anomaly_mm, y: row.visitor_index, month: row.month }));
  // เส้นแนวโน้มคำนวณด้วยกำลังสองน้อยสุดจากจุดเดียวกับที่วาด
  const meanX = points.reduce((sum, p) => sum + p.x, 0) / points.length;
  const meanY = points.reduce((sum, p) => sum + p.y, 0) / points.length;
  const denominator = points.reduce((sum, p) => sum + (p.x - meanX) ** 2, 0);
  const slope = denominator ? points.reduce((sum, p) => sum + (p.x - meanX) * (p.y - meanY), 0) / denominator : 0;
  const xs = [Math.min(...points.map((p) => p.x)), Math.max(...points.map((p) => p.x))];
  const trend = xs.map((x) => ({ x, y: meanY + slope * (x - meanX) }));

  charts.impact?.destroy();
  charts.impact = new Chart(document.querySelector('#impact-chart'), {
    data: {
      datasets: [
        { type: 'scatter', label: 'เดือน', data: points, backgroundColor: '#1f7fa3', pointRadius: 4,
          borderColor: '#ffffff', borderWidth: 2 },
        { type: 'line', label: 'เส้นแนวโน้ม', data: trend, borderColor: ACTUAL_COLOR, borderWidth: 2,
          pointRadius: 0, fill: false },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { font: { family: 'IBM Plex Sans Thai', size: 10 }, boxWidth: 10 } },
        tooltip: {
          callbacks: {
            label: (context) => {
              const point = context.raw;
              return point.month
                ? `${monthLabel(point.month)}: ฝนต่างจากปกติ ${Math.round(point.x)} มม. · ดัชนีคน ${point.y.toFixed(2)}`
                : `แนวโน้ม: ${point.y.toFixed(2)}`;
            },
          },
        },
      },
      scales: {
        x: { title: { display: true, text: 'ฝนมากกว่า/น้อยกว่าปกติของเดือนนั้น (มม.)', color: '#8a9699', font: { family: 'IBM Plex Sans Thai', size: 9 } },
             grid: { color: '#edf1ef' }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 9 } } },
        y: { title: { display: true, text: 'ดัชนีคน (1.0 = ตามที่คาด)', color: '#8a9699', font: { family: 'IBM Plex Sans Thai', size: 9 } },
             grid: { color: '#edf1ef' }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 9 } } },
      },
    },
  });

  document.querySelector('#impact-caveats').textContent = data.caveats.map((text) => `• ${text}`).join('  ');
}

// ---------------------------------------------------------------- โหลดทั้งหมด

async function loadDashboard() {
  const [summary, zones, micro, trends, traffic, recommendations, points, categories, demographics, upcoming, sources, forecast, impact] =
    await Promise.all([
      api('/api/summary'),
      api('/api/zones'),
      api('/api/zones/micro'),
      api('/api/trends'),
      api('/api/traffic/trends'),
      api('/api/recommendations'),
      api('/api/poi?limit=3000'),
      api('/api/poi/categories'),
      api('/api/demographics'),
      api('/api/holidays/upcoming?limit=4'),
      api('/api/sources'),
      api('/api/forecast/tourism?months=6'),
      api('/api/impact'),
    ]);

  renderSummary(summary);
  renderRanges(trends, traffic);
  renderMap(zones, points);
  renderMicroZones(micro);
  renderRanking(zones);
  renderTrendChart(trends, traffic);
  renderRecommendations(recommendations);
  renderPoiChart(categories);
  renderDemographics(demographics);
  renderHolidays(upcoming);
  renderSources(sources);
  renderTourismForecast(forecast);
  renderImpact(impact);

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

document.querySelector('#layer-zone').addEventListener('change', applyLayerVisibility);
document.querySelector('#layer-poi').addEventListener('change', applyLayerVisibility);
document.querySelector('#layer-traffic').addEventListener('change', applyLayerVisibility);

// ตอนพิมพ์ แดชบอร์ดถูกซ่อน แผนที่จึงกว้าง 0 แล้ว leaflet.heat จะโยน error ตอนวาดใหม่
// ถอดเลเยอร์ความร้อนออกชั่วคราวระหว่างพิมพ์ แล้วค่อยคืนตามสถานะ checkbox เดิม
window.addEventListener('beforeprint', () => {
  if (window.poiHeat && window.map.hasLayer(window.poiHeat)) window.map.removeLayer(window.poiHeat);
});
window.addEventListener('afterprint', applyLayerVisibility);

document.querySelectorAll('.nav-item').forEach((item) => {
  item.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach((other) => other.classList.remove('active'));
    item.classList.add('active');
  });
});

// ---------------------------------------------------------------- รายงานผู้บริหาร

// กันไม่ให้ข้อความจากฐานข้อมูล (ชื่อแยก ชื่อวันหยุด คำแนะนำ) ที่มีอักขระ HTML ทำให้รายงานเพี้ยน
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"]/g, (character) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[character]
));

// กราฟบนแดชบอร์ดวาดด้วย canvas จึงแปลงเป็นรูปฝังลงรายงานได้ตรง ๆ
function chartImage(chart, alt) {
  if (!chart) return '';
  return `<img class="pr-chart" alt="${alt}" src="${chart.toBase64Image()}">`;
}

function reportTable(headers, rows) {
  const head = headers.map((header) => `<th${header.num ? ' class="num"' : ''}>${header.label}</th>`).join('');
  const body = rows.map((row) => `<tr>${row.map((cell, index) => (
    `<td${headers[index].num ? ' class="num"' : ''}>${cell}</td>`
  )).join('')}</tr>`).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function buildReport({ summary, sources, forecast, impact, micro, recommendations }) {
  const sections = [];

  sections.push(`
    <div class="pr-head">
      <h1>รายงานสรุปผู้บริหาร — ระบบคลังข้อมูลการท่องเที่ยวเมืองพัทยา</h1>
      <div class="pr-meta">สร้างเมื่อ ${new Date().toLocaleString('th-TH')} · ข้อมูลจริงจาก Open Data ทั้งหมด ไม่มีข้อมูลจำลอง</div>
    </div>`);

  sections.push(`
    <div class="pr-section">
      <h2>ตัวชี้วัดภาพรวม</h2>
      <div class="pr-kpi">
        <div><b>${numberFormat.format(summary.tourism.tourists_year_total)}</b><span>นักท่องเที่ยวลงเกาะล้าน ปี ${summary.tourism.year}${
          summary.tourism.year_over_year_percent === null ? '' : ` (${summary.tourism.year_over_year_percent}% เทียบปีก่อน)`
        }</span></div>
        <div><b>${compactFormat.format(summary.traffic.vehicles)}</b><span>ปริมาณรถ ปี ${summary.traffic.calendar_year} จาก ${summary.traffic.intersections} แยก</span></div>
        <div><b>${numberFormat.format(summary.poi.total)}</b><span>ธุรกิจท่องเที่ยว ${summary.poi.categories} ประเภท (OpenStreetMap)</span></div>
        <div><b>${numberFormat.format(summary.population.population)}</b><span>ประชากรแฝง ปี ${summary.population.calendar_year}</span></div>
      </div>
    </div>`);

  if (micro?.zones?.length) {
    sections.push(`
      <div class="pr-section">
        <h2>การกระจายตัวรายย่าน (Micro-Zoning)</h2>
        ${reportTable(
          [{ label: 'ย่าน' }, { label: 'ธุรกิจ', num: true }, { label: 'ส่วนแบ่ง', num: true },
           { label: 'ต่อ ตร.กม.', num: true }, { label: 'แยก', num: true }, { label: 'ปริมาณรถสะสม', num: true }, { label: 'ประเภทเด่น' }],
          micro.zones.map((zone) => [
            escapeHtml(zone.name),
            numberFormat.format(zone.poi_total),
            `${zone.poi_share_percent}%`,
            zone.poi_density_per_km2,
            zone.intersection_count,
            numberFormat.format(zone.vehicle_volume),
            escapeHtml(zone.dominant_category || '—'),
          ])
        )}
      </div>`);
  }

  if (forecast?.forecast?.length) {
    sections.push(`
      <div class="pr-section">
        <h2>พยากรณ์นักท่องเที่ยว</h2>
        <p style="margin:0 0 6px">วิธีที่เลือกอัตโนมัติ: <b>${escapeHtml(forecast.model.label)}</b> —
          คลาดเคลื่อนในปีทดสอบ ${forecast.accuracy.test_mape_percent}%
          (วิธีพื้นฐาน "เดือนเดียวกันปีก่อน" ${forecast.accuracy.seasonal_naive_mape_percent}%)</p>
        ${reportTable(
          [{ label: 'เดือน' }, { label: 'พยากรณ์ (คน)', num: true }, { label: 'ช่วงประมาณ 95%', num: true }],
          forecast.forecast.map((row) => [
            monthLabel(row.month),
            numberFormat.format(row.visitors),
            `${numberFormat.format(row.lower)} – ${numberFormat.format(row.upper)}`,
          ])
        )}
        ${chartImage(charts.forecast, 'กราฟพยากรณ์นักท่องเที่ยว')}
      </div>`);
  }

  if (impact && !impact.error) {
    const impactRow = (label, result) => [
      label,
      `${result.effect_percent > 0 ? '+' : ''}${result.effect_percent}%`,
      `${result.correlation}${result.correlation_ci_95 ? ` [${result.correlation_ci_95.join(', ')}]` : ''}`,
      `${result.months_used} เดือน`,
      result.significant ? 'ความสัมพันธ์ชัดเจน' : 'ยังสรุปไม่ได้ (ช่วงความเชื่อมั่นคร่อมศูนย์)',
    ];
    const rows = [];
    if (impact.rain) rows.push(impactRow('ฝนมากกว่าปกติของเดือนนั้น 100 มม.', impact.rain));
    if (impact.holidays) rows.push(impactRow('วันหยุดราชการเพิ่มขึ้น 1 วัน', impact.holidays));
    if (rows.length) {
      sections.push(`
        <div class="pr-section">
          <h2>ผลของฝนและวันหยุด</h2>
          <p style="margin:0 0 6px">${escapeHtml(impact.method)}</p>
          ${reportTable([{ label: 'ปัจจัย' }, { label: 'ผลต่อนักท่องเที่ยว', num: true },
            { label: 'r [ช่วงเชื่อมั่น 95%]', num: true }, { label: 'ข้อมูลที่ใช้', num: true },
            { label: 'ข้อสรุป' }], rows)}
        </div>`);
    }
  }

  if (recommendations?.length) {
    sections.push(`
      <div class="pr-section">
        <h2>ข้อเสนอแนะเชิงกลยุทธ์</h2>
        ${recommendations.slice(0, 6).map((item) => `
          <div class="pr-rec">
            <b>${escapeHtml(item.title)}</b>
            <p>${escapeHtml(item.detail)}</p>
            <cite>${escapeHtml(item.signal)} · ${escapeHtml(item.source)}${
              item.assumption ? ` · สมมติฐาน: ${escapeHtml(item.assumption)}` : ''
            }</cite>
          </div>`).join('')}
      </div>`);
  }

  sections.push(`
    <div class="pr-section">
      <h2>แหล่งข้อมูลและเวลานำเข้าล่าสุด</h2>
      ${reportTable(
        [{ label: 'ชุดข้อมูล' }, { label: 'จำนวนระเบียน', num: true }, { label: 'นำเข้าล่าสุด' }],
        sources.map((source) => [
          escapeHtml(source.name),
          source.records ? numberFormat.format(source.records) : '—',
          escapeHtml(source.last_imported_at || 'ยังไม่นำเข้า'),
        ])
      )}
    </div>`);

  const caveats = [...(forecast?.caveats || []), ...(impact?.caveats || []), ...(micro?.caveats || [])];
  sections.push(`
    <div class="pr-foot">
      <b>ข้อจำกัดที่ต้องอ่านประกอบ:</b><br>
      ${caveats.map((caveat) => `• ${escapeHtml(caveat)}`).join('<br>')}
    </div>`);

  return sections.join('');
}

document.querySelector('#export-btn').addEventListener('click', async () => {
  const button = document.querySelector('#export-btn');
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span>◷</span> กำลังสร้างรายงาน...';
  try {
    const [summary, sources, forecast, impact, recommendations] = await Promise.all([
      api('/api/summary'),
      api('/api/sources'),
      api('/api/forecast/tourism?months=6'),
      api('/api/impact'),
      api('/api/recommendations'),
    ]);
    document.querySelector('#print-report').innerHTML = buildReport({
      summary, sources, forecast, impact, recommendations, micro: state.microZones,
    });
    window.print();
  } catch (error) {
    console.error('สร้างรายงานไม่สำเร็จ:', error);
    alert('สร้างรายงานไม่สำเร็จ กรุณาลองใหม่อีกครั้ง');
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
});

loadDashboard().catch((error) => {
  console.error('Dashboard loading failed:', error);
  document.querySelector('#stream-status').textContent = 'โหลดข้อมูลไม่สำเร็จ';
});
