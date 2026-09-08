const api = (path) => fetch(path).then((response) => response.json());
let trendChart;

const numberFormat = new Intl.NumberFormat('th-TH');
const bahtFormat = new Intl.NumberFormat('th-TH', { notation: 'compact', maximumFractionDigits: 1 });

function updateSummary(data) {
  document.querySelector('#visitors-kpi').textContent = numberFormat.format(data.visitors);
  document.querySelector('#spending-kpi').textContent = `฿${bahtFormat.format(data.spending)}`;
  document.querySelector('#stay-kpi').textContent = `${data.avg_stay} ชม.`;
  document.querySelector('#zones-kpi').textContent = data.zones;
  document.querySelector('#map-zones').textContent = data.zones;
}

function updateRanking(zones) {
  const maxVisitors = zones[0]?.visitors || 1;
  document.querySelector('#ranking-list').innerHTML = zones.map((zone, index) => `
    <div class="ranking-item">
      <span class="rank">0${index + 1}</span>
      <div><div class="rank-name">${zone.zone}</div><div class="rank-bar"><i style="width:${Math.max(12, zone.visitors / maxVisitors * 100)}%"></i></div></div>
      <div class="rank-value">${numberFormat.format(zone.visitors)}<span class="rank-unit">ผู้เยี่ยมชม</span></div>
    </div>`).join('');
}

function updateRecommendations(items) {
  document.querySelector('#recommendation-list').innerHTML = items.map((item) => `
    <div class="recommendation-item"><span class="signal">${item.signal}</span><strong>${item.zone}</strong><p>${item.action}</p></div>`).join('');
}

function weatherDescription(code) {
  if (code === 0) return 'ท้องฟ้าแจ่มใส';
  if ([1, 2, 3].includes(code)) return 'มีเมฆบางส่วน';
  if ([51, 53, 55, 61, 63, 65, 80, 81, 82].includes(code)) return 'มีฝน';
  if ([95, 96, 99].includes(code)) return 'พายุฝนฟ้าคะนอง';
  return 'สภาพอากาศเปลี่ยนแปลง';
}

function updateLiveWeather(data) {
  document.querySelector('#weather-live').textContent =
    `${data.location} ${data.temperature_c}°C · ${weatherDescription(data.weather_code)} · ฝน ${data.precipitation_mm} มม.`;
}

function updateMap(zones) {
  if (window.zoneLayer) window.zoneLayer.clearLayers();
  window.zoneLayer = L.layerGroup().addTo(window.map);
  const maxVisitors = zones[0]?.visitors || 1;
  zones.forEach((zone) => {
    const intensity = zone.visitors / maxVisitors;
    const color = intensity > .78 ? '#e66e5d' : intensity > .55 ? '#e6b24f' : '#63a9a7';
    L.circleMarker([zone.latitude, zone.longitude], { radius: 10 + intensity * 15, color, fillColor: color, fillOpacity: .68, weight: 2 })
      .bindPopup(`<strong>${zone.zone}</strong><br>ผู้เยี่ยมชม ${numberFormat.format(zone.visitors)} คน<br>ใช้จ่าย ฿${numberFormat.format(Math.round(zone.spending))}`)
      .addTo(window.zoneLayer);
  });
}

function updateChart(trends) {
  const context = document.querySelector('#trend-chart');
  if (trendChart) trendChart.destroy();
  trendChart = new Chart(context, { type: 'line', data: { labels: trends.map((item) => item.date.slice(8)), datasets: [{ data: trends.map((item) => item.visitors), borderColor: '#ee745b', backgroundColor: '#ee745b18', fill: true, tension: .35, pointRadius: 2, pointBackgroundColor: '#ee745b', borderWidth: 2 }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: (context) => `${numberFormat.format(context.parsed.y)} คน` } } }, scales: { x: { grid: { display: false }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 10 } } }, y: { grid: { color: '#edf1ef' }, ticks: { color: '#9aa5a7', font: { family: 'DM Mono', size: 10 }, callback: (value) => `${Math.round(value / 1000)}k` } } } } });
}

async function loadDashboard(weather = '') {
  const [summary, zones, trends, recommendations, liveWeather] = await Promise.all([
    api('/api/summary'), api(`/api/zones${weather ? `?weather=${encodeURIComponent(weather)}` : ''}`), api('/api/trends'), api('/api/recommendations'), api('/api/weather/live').catch(() => null)
  ]);
  updateSummary(summary); updateRanking(zones); updateMap(zones); updateChart(trends); updateRecommendations(recommendations);
  if (liveWeather) updateLiveWeather(liveWeather);
}

window.map = L.map('map', { zoomControl: false }).setView([12.926, 100.879], 13);
L.control.zoom({ position: 'bottomright' }).addTo(window.map);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '&copy; OpenStreetMap' }).addTo(window.map);

document.querySelector('#weather-filter').addEventListener('change', (event) => loadDashboard(event.target.value));
document.querySelector('#export-btn').addEventListener('click', () => {
  const report = `PATTAYA SMART TOURISM\nรายงานสรุปผู้บริหาร\nสร้างเมื่อ: ${new Date().toLocaleString('th-TH')}\n\nระบบต้นแบบพร้อมเชื่อมต่อข้อมูลจริง`;
  const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([report], { type: 'text/plain' })); link.download = 'pattaya-tourism-report.txt'; link.click(); URL.revokeObjectURL(link.href);
});
loadDashboard().catch((error) => console.error('Dashboard loading failed:', error));
