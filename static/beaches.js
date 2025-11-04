'use strict';
/* [CONFIG & STATE] Konfigurasi tampilan dan state global*/

/* Mode ring pada summary:
 'prob' : tampilkan rata-rata probabilitas ML (%)
 'days' : tampilkan rasio hari layak (x/5) */
const SUMMARY_MODE = 'days';

/* Keliling stroke untuk progress ring (r = 54)*/
const TOTAL_STROKE = 339.3;

/* Aktivitas saat ini (sinkron dengan radio input)*/
let currentActivity = 'pantai';

/* Cache response per aktivitas agar switching cepat dan hemat request*/
const cachedData = Object.create(null);

/* [DOM HELPERS] Utilitas untuk akses elemen*/
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => root.querySelectorAll(sel);
const byId = (id) => document.getElementById(id);

/* [UTILITIES] Fungsi kecil lintas-komponen*/

/**
 * Tentukan kelas badge berdasarkan probabilitas (0..1)
 * @param {number} p
 * @returns {'badge-excellent'|'badge-poor'}
 */
function getBadgeClass(p) {
  return p >= 0.6 ? 'badge-excellent' : 'badge-poor';
}

/**
 * Format ISO date ke "Hari, DD MMM" (ID)
 * @param {string} isoStr
 * @returns {string}
 */
function formatDate(isoStr) {
  const d = new Date(isoStr);
  const days = ['Min','Sen','Sel','Rab','Kam','Jum','Sab'];
  const m = ['Jan','Feb','Mar','Apr','Mei','Jun','Jul','Agu','Sep','Okt','Nov','Des'];
  return `${days[d.getDay()]}, ${d.getDate()} ${m[d.getMonth()]}`;
}

/* [RENDER SUMMARY CARDS] Progress ring per lokasi */

/**
 * Render kartu ringkasan (progress ring) per lokasi.
 * Mengutamakan field `summary` dari backend agar selaras dengan tabel detail.
 * @param {Array<Object>} locations
 */
function populateSummaryCards(locations) {
  const c = byId('summary-cards');
  if (!c) return;
  c.innerHTML = '';

  const gradients = [
    'linear-gradient(135deg,#667eea 0%,#764ba2 100%)',
    'linear-gradient(135deg,#f093fb 0%,#f5576c 100%)',
    'linear-gradient(135deg,#4facfe 0%,#00f2fe 100%)',
    'linear-gradient(135deg,#43e97b 0%,#38f9d7 100%)'
  ];

  locations.forEach((loc, i) => {
    // Kartu error per lokasi bila backend gagal untuk lokasi tsb
    if (!loc?.ok) {
      c.insertAdjacentHTML('beforeend', `
        <div class="col-md-6 col-lg-3">
          <div class="summary-card error-card text-center">
            <h6 class="fw-bold">${loc?.beach ?? '-'}</h6>
            <p class="text-danger small mb-0">${loc?.error || 'Data tidak tersedia'}</p>
          </div>
        </div>`);
      return;
    }

    // Sumber kebenaran utama: summary dari backend
    const totalDays = loc.summary?.days_total ?? (loc.days?.length || 0);
    let layakDays   = loc.summary?.days_ok ?? 0;
    let avgProbaPct = loc.summary ? Math.round((loc.summary.avg_proba || 0) * 100) : 0;

    // Fallback jika backend lama
    if (loc.summary == null) {
      let sumProba = 0, nProba = 0;
      layakDays = 0;
      (loc.days || []).forEach(d => {
        const p = d.ml_predictions?.find(x => x.label?.toLowerCase() === currentActivity);
        if (p) {
          sumProba += (p.proba_1 || 0);
          nProba += 1;
          if (p.pred === 1) layakDays += 1;
        }
      });
      avgProbaPct = nProba ? Math.round((sumProba / nProba) * 100) : 0;
    }

    const daysPct   = totalDays ? Math.round((layakDays / totalDays) * 100) : 0;
    const ringPct   = (SUMMARY_MODE === 'days') ? daysPct : avgProbaPct; // (dipakai untuk animasi teks)
    const centerTxt = (SUMMARY_MODE === 'days') ? `${layakDays}/${totalDays}` : `${avgProbaPct}%`;
    const centerLbl = (SUMMARY_MODE === 'days') ? 'Hari Layak' : 'Rata-rata ML';
    const badgeTxt  = (SUMMARY_MODE === 'days') ? `${daysPct}% Cocok` : `${layakDays}/${totalDays} Hari Layak`;

    c.insertAdjacentHTML('beforeend', `
      <div class="col-md-6 col-lg-3">
        <div class="summary-card text-center" style="--card-gradient:${gradients[i % 4]}">
          <div class="card-glow"></div>
          <h6 class="location-name">${loc.beach}</h6>

          <div class="score-circle" data-ring="${ringPct}">
            <svg class="progress-ring" width="120" height="120" aria-label="Persentase Kecocokan">
              <defs>
                <linearGradient id="progressGradient${i}" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" stop-color="#667eea"/><stop offset="100%" stop-color="#764ba2"/>
                </linearGradient>
              </defs>
              <circle class="progress-ring-bg" cx="60" cy="60" r="54"></circle>
              <circle class="progress-ring-fill" cx="60" cy="60" r="54"
                      style="stroke-dasharray:0 ${TOTAL_STROKE};stroke:url(#progressGradient${i});"></circle>
            </svg>
            <div class="score-text">
              <div class="score-number">${centerTxt}</div>
              <div class="score-label">${centerLbl}</div>
            </div>
          </div>

          <div class="percentage-badge">${badgeTxt}</div>
        </div>
      </div>`);
  });

  // Animasi ring berdasarkan teks di tengah  
  requestAnimationFrame(() => {
    c.querySelectorAll('.summary-card').forEach(card => {
      const txt = card.querySelector('.score-number')?.textContent?.trim() ?? '';
      let pct = 0;
      if (txt.includes('%')) {
        pct = parseInt(txt, 10) || 0;
      } else if (txt.includes('/')) {
        const [num, den] = txt.split('/').map(Number);
        pct = den ? Math.round((num / den) * 100) : 0;
      }
      const val = Math.min(pct * 3.393, TOTAL_STROKE);
      const fill = card.querySelector('.progress-ring-fill');
      if (fill) {
        fill.style.transition = 'stroke-dasharray 900ms cubic-bezier(.22,.61,.36,1)';
        fill.style.strokeDasharray = `${val} ${TOTAL_STROKE}`;
      }
    });
  });

  // Update statistik kecil di atas
  const locOk = locations.find(l => l?.summary?.days_total || (l?.days?.length > 0));
  byId('stat-locations').textContent   = String(locations.length);
  byId('stat-days').textContent        = String(locOk?.summary?.days_total ?? 5);
  byId('stat-predictions').textContent = String(locations.length * (locOk?.summary?.days_total ?? 5) * 4); // 4 label ML
}

/* [RENDER HEADER TANGGAL] Konsistensi kolom "HARI INI"*/

/* Ambil array ISO date untuk header H0..H4 dari lokasi pertama yang valid */
function getHeaderDates(locations) {
  const first = locations.find(l => l?.ok && Array.isArray(l.days) && l.days.length);
  return first ? first.days.slice(0, 5).map(d => d.date_iso) : [];
}

/* Cari index "HARI INI" dari lokasi pertama yang valid (fallback 0) */
function getTodayIndex(locations) {
  const first = locations.find(l => l?.ok && Array.isArray(l.days));
  const idx = first ? first.days.findIndex(d => d && d.is_today) : -1;
  return idx >= 0 ? idx : 0;
}

/* Template header tanggal */
function headerLabel(iso, isToday) {
  const d = new Date(iso);
  const days = ['Min','Sen','Sel','Rab','Kam','Jum','Sab'];
  const months = ['Jan','Feb','Mar','Apr','Mei','Jun','Jul','Agu','Sep','Okt','Nov','Des'];
  const tanggal = `${days[d.getDay()]}, ${d.getDate()} ${months[d.getMonth()]}`;
  return isToday
    ? `<div class="th-content"><strong>HARI INI</strong><br><small>${tanggal}</small></div>`
    : `<div class="th-content">${tanggal}</div>`;
}

/* [RENDER TABEL HARIAN] Cell & Baris per lokasi */

/** Buat cell tabel harian (prediksi + ringkas cuaca + tombol Detail)
 * @param {Object} day
 * @param {boolean} isToday
 * @returns {string} HTML string
 */
function createWeatherCell(day, isToday = false) {
  if (!day) return '<td class="text-center text-muted">-</td>';

  const activityPred = day.ml_predictions?.find(
    p => p.label?.toLowerCase() === currentActivity
  );
  const proba = activityPred ? activityPred.proba_1 : 0;
  const isLayak = activityPred ? activityPred.pred === 1 : false;
  const badgeClass = isLayak ? 'badge-excellent' : 'badge-poor';
  const probaPercent = (proba * 100).toFixed(0);
  const todayClass = isToday ? 'today-cell' : '';

  return `
    <td class="day-cell ${todayClass}">
      ${isToday ? '<div class="today-badge">HARI INI</div>' : ''}
      <div class="cell-content">
        <div class="prediction-badge ${badgeClass}">
          <span class="badge-icon">${isLayak ? '✓' : '✗'}</span>
          <span class="badge-text">${probaPercent}%</span>
        </div>
        <div class="weather-info">
          <div class="weather-item"><i class="bi bi-thermometer-half"></i><span>${day.temp_avg}°C</span></div>
          <div class="weather-item"><i class="bi bi-cloud-rain"></i><span>${day.rain_mm}mm</span></div>
          <div class="weather-item"><i class="bi bi-sun"></i><span>${day.sunshine_h}h</span></div>
        </div>
        <!-- NOTE: JSON.stringify di-escape " menjadi &quot; agar aman pada atribut -->
        <button class="detail-btn" onclick="showDayDetails('${day.date_iso}', ${JSON.stringify(day).replace(/"/g,'&quot;')})">
          <i class="bi bi-info-circle"></i> Detail
        </button>
      </div>
    </td>`;
}

/** Render tabel perbandingan: sejajarkan header & kolom "HARI INI"
 * @param {Array<Object>} locations
 */
function populateComparisonTable(locations) {
  const tbody = byId('table-body');
  if (!tbody) return;
  tbody.innerHTML = '';

  // Header konsisten berdasarkan tanggal dari lokasi pertama yang valid
  const dates = getHeaderDates(locations);
  const todayIdx = getTodayIndex(locations);
  for (let i = 0; i < 5; i++) {
    const th = byId(`day-header-${i}`);
    if (!th) continue;
    th.classList.toggle('today-highlight', i === todayIdx);
    if (dates[i]) th.innerHTML = headerLabel(dates[i], i === todayIdx);
  }

  // Baris per lokasi
  locations.forEach((location) => {
    let row = `
      <tr class="location-row">
        <td class="location-cell">
          <div class="location-info">
            <div class="location-name-badge fw-bold">${location.beach}</div>
            ${location.ok ? `<div class="location-meta">☀️ ${location.sunrise} - ${location.sunset}</div>` : ''}
          </div>
        </td>
    `;

    if (!location.ok) {
      row += `<td colspan="5" class="text-center text-danger">Error: ${location.error || 'Data tidak tersedia'}</td>`;
    } else {
      for (let i = 0; i < 5; i++) {
        const day = location.days?.[i];
        row += createWeatherCell(day, i === todayIdx);
      }
    }

    row += '</tr>';
    tbody.insertAdjacentHTML('beforeend', row);
  });
}

/* [MODAL]  Detail harian */

/** Buka modal detail untuk 1 hari terpilih
 * @param {string} dateIso
 * @param {Object} dayData
 */
function showDayDetails(dateIso, dayData) {
  const modal = new bootstrap.Modal(byId('detailModal'));
  byId('modal-title').textContent = `Detail: ${formatDate(dateIso)}`;

  const content = `
    <div class="detail-section">
      <h6 class="fw-bold mb-3">Kondisi Cuaca</h6>
      <div class="row g-3 row-cols-2 row-cols-md-5 align-items-stretch">
        <div class="col">
          <div class="weather-stat h-100">
            <i class="bi bi-thermometer-half"></i>
            <div class="stat-value">${dayData.temp_min}°C - ${dayData.temp_max}°C</div>
            <div class="stat-label">Suhu Rata-Rata: ${dayData.temp_avg}°C</div>
          </div>
        </div>
        <div class="col">
          <div class="weather-stat">
            <i class="bi bi-droplet"></i>
            <div class="stat-value">${dayData.humidity_avg}%</div>
            <div class="stat-label">Kelembaban Rata-Rata</div>
          </div>
        </div>
        <div class="col">
          <div class="weather-stat">
            <i class="bi bi-wind"></i>
            <div class="stat-value">${dayData.wind_kmh_avg} km/h</div>
            <div class="stat-label">Kecepatan Angin Rata-Rata</div>
          </div>
        </div>
        <div class="col">
          <div class="weather-stat">
            <i class="bi bi-cloud-rain"></i>
            <div class="stat-value">${dayData.rain_mm} mm</div>
            <div class="stat-label">Curah Hujan (total)</div>
          </div>
        </div>
        <div class="col">
          <div class="weather-stat">
            <i class="bi bi-brightness-high"></i>
            <div class="stat-value">${dayData.sunshine_h} h</div>
            <div class="stat-label">Lama Penyinaran Matahari</div>
          </div>
        </div>
      </div>
    </div>`;
  byId('modal-body').innerHTML = content;
  modal.show();
}
// Expose agar bisa dipanggil dari onclick HTML
window.showDayDetails = showDayDetails;

/* [DATA] Fetch & orchestrate render */

/** Ambil data dari backend untuk aktivitas saat ini, perlihatkan loading/error state, lalu render komponen */
async function loadBeachesData() {
  const loadingEl = byId('loading-state');
  const errorEl   = byId('error-state');
  const mainEl    = byId('main-content');

  try {
    const response = await fetch(`/api/beaches-forecast?activity=${currentActivity}`);
    const data = await response.json();

    if (!response.ok || !data?.ok) {
      throw new Error(data?.error || 'Failed to load data');
    }

    // Simpan ke cache dan tampilkan konten
    cachedData[currentActivity] = data;
    loadingEl.classList.add('d-none');
    errorEl.classList.add('d-none');
    mainEl.classList.remove('d-none');

    renderAllComponents(data.locations);
  } catch (err) {
    console.error('Error:', err);
    loadingEl.classList.add('d-none');
    mainEl.classList.add('d-none');
    errorEl.classList.remove('d-none');
    byId('error-message').textContent = String(err.message || err);
  }
}

/** Render ulang semua komponen UI yang bergantung pada data lokasi
 * @param {Array<Object>} [locations]
 */
function renderAllComponents(locations) {
  if (!locations) {
    // fallback ke cache bila ada
    if (cachedData[currentActivity]) locations = cachedData[currentActivity].locations;
    else return;
  }
  populateSummaryCards(locations);
  populateComparisonTable(locations);
}

/* [INIT] Hook awal saat DOM siap */

document.addEventListener('DOMContentLoaded', () => {
  // Fetch pertama
  loadBeachesData();

  // Ganti aktivitas: render dari cache bila ada, atau fetch baru
  $$('input[name="activity"]').forEach(radio => {
    radio.addEventListener('change', async function () {
      currentActivity = this.value;
      if (cachedData[currentActivity]) {
        renderAllComponents(cachedData[currentActivity].locations);
      } else {
        byId('main-content').classList.add('d-none');
        byId('loading-state').classList.remove('d-none');
        await loadBeachesData();
      }
    });
  });
});