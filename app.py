import logging
from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import concurrent.futures
import os
import pickle
import joblib
import requests
import pandas as pd
import numpy as np
from dotenv import load_dotenv


load_dotenv()
app = Flask(__name__)


LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
logger.info("Flask app starting, log level = %s", LOG_LEVEL)



API_KEY = os.getenv("OPENWEATHER_API_KEY", "aef2fdd2aa915a9a7a7a7e3835c66468")
UNITS = "metric"
LANG = "id"
DEFAULT_PLACE = os.getenv("DEFAULT_PLACE", "Bali")



SUN_MODE = "effective" 



MODEL_PATH = "model_random_forest.pkl"
LABEL_NAMES = os.getenv("ML_LABEL_NAMES", "pantai,hiking,snorkeling,rafting").split(",")



_model = None
_model_feature_names = None


PANTAI_LOCATIONS = [
    {"name": "Pantai Kuta", "lat": -8.7184, "lon": 115.1686},
    {"name": "Pantai Sanur", "lat": -8.7069, "lon": 115.2625},
    {"name": "Pantai Nusa Dua", "lat": -8.8014, "lon": 115.2303},
    {"name": "Pantai Seminyak", "lat": -8.6919, "lon": 115.1680},
]
HIKING_LOCATIONS = [
    {"name": "Gunung Batur", "lat": -8.2425, "lon": 115.3751},
    {"name": "Gunung Agung", "lat": -8.3429, "lon": 115.5079},
    {"name": "Campuhan Ridge Walk", "lat": -8.5069, "lon": 115.2625},
    {"name": "Gunung Batukaru", "lat": -8.3644, "lon": 115.0933},
]
SNORKELING_LOCATIONS = [
    {"name": "Pantai Amed", "lat": -8.3469, "lon": 115.6636},
    {"name": "Pulau Menjangan", "lat": -8.1561, "lon": 114.5139},
    {"name": "Blue Lagoon (Padangbai)", "lat": -8.5392, "lon": 115.5061},
    {"name": "Tulamben (USAT Liberty)", "lat": -8.2750, "lon": 115.5967},
]
RAFTING_LOCATIONS = [
    {"name": "Sungai Ayung", "lat": -8.5500, "lon": 115.2639},
    {"name": "Sungai Telaga Waja", "lat": -8.4969, "lon": 115.4881},
    {"name": "Sungai Melangit", "lat": -8.3975, "lon": 115.3669},
    {"name": "Sungai Unda", "lat": -8.5411, "lon": 115.4833},
]



ACTIVITY_LOCATIONS = {
    "pantai": PANTAI_LOCATIONS,
    "hiking": HIKING_LOCATIONS,
    "snorkeling": SNORKELING_LOCATIONS,
    "rafting": RAFTING_LOCATIONS,
}


def id_date(dt: datetime) -> str:
    bulan = [
        "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember",
    ][dt.month - 1]
    hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][dt.weekday()]
    return f"{hari}, {dt.day:02d} {bulan} {dt.year}"




def overlap_daylight(start: datetime, end: datetime, sunrise: datetime, sunset: datetime) -> float:
    a = max(start, sunrise)
    b = min(end, sunset)
    return max(0.0, (b - a).total_seconds() / 3600.0)




def compute_sunshine_for_day(arr_3h, sr_day: datetime, ss_day: datetime, mode: str = "effective") -> float:
    if mode == "daylength":
        return max(0.0, (ss_day - sr_day).total_seconds() / 3600.0)



    sun_hours = 0.0
    for a in arr_3h:
        clouds = (a.get("clouds", {}) or {}).get("all", 50)
        daylight = overlap_daylight(a["_start"], a["_end"], sr_day, ss_day)
        sun_hours += daylight * max(0.0, 1.0 - clouds / 100.0)
    return sun_hours


N_DAY = 12.0  


TC_TABLE = [
    (38.0, float("inf"), -10),
    (36.0, 37.9, -5),
    (34.0, 35.9, 0),
    (33.0, 33.9, 1),
    (32.0, 32.9, 2),
    (31.0, 31.9, 3),
    (30.0, 30.9, 4),
    (29.0, 29.9, 5),
    (28.0, 28.9, 6),
    (27.0, 27.9, 7),
    (25.0, 26.9, 8),
    (23.0, 24.49, 9),
    (24.5, 24.9, 10),
    (-float("inf"), 22.0, -10),
]



A_TABLE = [
    (0.0,   0.9,   8),
    (1.0,   14.9,  9),
    (15.0,  25.9, 10),
    (26.0,  35.9,  9),
    (36.0,  45.9,  8),
    (46.0,  55.9,  7),
    (56.0,  65.9,  6),
    (66.0,  75.9,  5),
    (76.0,  85.9,  4),
    (86.0,  95.9,  3),
    (96.0,  float("inf"), 2),
]



P_TABLE = [
    (0.00,  0.00, 10),
    (0.01,  2.99,  9),
    (3.00,  5.99,  8),
    (6.00,  8.99,  6),
    (9.00, 11.99,  4),
    (12.00, 24.99,  0),
    (25.00, float("inf"), -1),
]



W_TABLE = [
    (0.0,   0.5,   8),
    (0.6,   9.9,  10),
    (10.0, 19.9,   9),
    (20.0, 29.9,   8),
    (30.0, 39.9,   6),
    (40.0, 49.9,   3),
    (50.0, 69.9,   0),
    (70.0, float("inf"), -10),
]



def lookup_score(value, table):
    for lower, upper, rating in table:
        if lower <= value <= upper:
            return rating
    return 0



def ss_to_cc_equiv(ss_hours):
    """SS (jam) -> CC_equiv (%), pakai clip 0..100 seperti notebook."""
    cc = 100.0 * (1.0 - (float(ss_hours) / N_DAY))
    return float(np.clip(cc, 0.0, 100.0))



W_THI, W_S, W_P, W_W = 0.367, 0.519, 0.085, 0.028



def rate_from_table(x, table):
    for low, high, r in table:
        if (low is None or x >= low) and (high is None or x <= high):
            return r
    return 0



def clamp(x, lo, hi):
    return max(lo, min(hi, x))



def calc_thi(tavg, rh_avg):
    return 0.8 * tavg + (rh_avg * tavg) / 500.0



THI_TABLE = [
    (28.0,  None, 0), (None, 14.9, 0),
    (27.5,  27.9, 3), (17.0, 17.9, 3),
    (27.0,  27.4, 5), (19.0, 19.9, 5),
    (26.5,  26.9, 6), (20.0, 20.9, 6),
    (26.0,  26.4, 7), (21.0, 21.9, 7),
    (25.5,  25.9, 8), (22.0, 22.9, 8),
    (25.0,  25.4, 9), (23.0, 23.9, 9),
    (24.0,  24.9, 10),
    (18.0,  18.9, 4),
    (16.0,  16.9, 2),
    (15.0,  15.9, 1),
]



def ss_to_cc_snork(ss_hours):
    return clamp(100.0 * (1.0 - (ss_hours / 12.0)), 0.0, 100.0)



S_TABLE_CC = [
    (None, 16.7, 10),
    (16.8, 24.9, 9),
    (25.1, 33.3, 8),
    (33.4, 41.7, 7),
    (41.8, 50.0, 6),
    (50.1, 58.3, 5),
    (58.4, 66.7, 4),
    (66.8, 75.0, 3),
    (75.1, 83.3, 2),
    (83.5, 91.7, 1),
    (91.8, None, 0),
]



P_TABLE_SNORK = [
    (0.0,   0.0, 10),
    (0.1,   1.9, 9),
    (2.0,   2.9, 8),
    (3.0,   3.9, 7),
    (4.0,   4.9, 6),
    (5.0,   5.9, 5),
    (6.0,   6.9, 4),
    (7.0,   7.9, 3),
    (8.0,   8.9, 2),
    (9.0,   9.9, 1),
    (10.0,  None, 0),
]



W_TABLE_SNORK = [
    (6.0,  11.9, 10),
    (1.0,   5.9,  9), (12.0, 15.9, 9),
    (None,  0.99, 8), (16.0, 19.9, 8),
    (20.0, 24.9, 7),
    (25.0, 28.9, 6),
    (29.0, 33.9, 5),
    (34.0, 38.9, 4),
    (39.0, 43.9, 3),
    (44.0, 49.9, 2),
    (50.0, 55.9, 1),
    (56.0, None, 0),
]



def compute_ctci_bali_scalar(tavg, rh_avg, rr, ss, ff_avg_kmh):
    thi = calc_thi(tavg, rh_avg)
    thi_rating = rate_from_table(thi, THI_TABLE)



    cc = ss_to_cc_snork(ss)
    s_rating = rate_from_table(cc, S_TABLE_CC)



    p_rating = rate_from_table(rr, P_TABLE_SNORK)
    w_rating = rate_from_table(ff_avg_kmh, W_TABLE_SNORK)



    ctci = (
        W_THI * thi_rating +
        W_S   * s_rating   +
        W_P   * p_rating   +
        W_W   * w_rating
    )
    return float(ctci)



def generate_layak_explanation(activity: str, features: dict, pred: int, proba: float) -> dict:
    """
    Generate explanation mengapa suatu aktivitas layak/tidak layak
    berdasarkan rule-based criteria untuk masing-masing aktivitas.
    
    Returns:
        {
            "status": "Layak" | "Tidak Layak",
            "proba_pct": int,
            "reasons": [{"factor": str, "value": str, "status": "baik"|"buruk", "detail": str}],
            "summary": str
        }
    """
    tavg = features.get("TAVG", 0)
    rh_avg = features.get("RH_AVG", 0)
    rr = features.get("RR", 0)
    ss = features.get("SS", 0)
    ff_kmh = features.get("FF_AVG_kmh", 0)
    
    status = "Layak" if pred == 1 else "Tidak Layak"
    proba_pct = int(proba * 100)
    reasons = []
    
    activity = activity.lower()
    
    # === PANTAI ===
    if activity == "pantai":
        # Suhu ideal: 25-32°C
        if 25 <= tavg <= 32:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "baik",
                "detail": "Suhu ideal untuk aktivitas pantai (25-32°C)"
            })
        elif tavg > 32:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "buruk",
                "detail": "Suhu terlalu panas (>32°C), berisiko heat stress"
            })
        else:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "buruk",
                "detail": "Suhu terlalu dingin (<25°C) untuk aktivitas pantai"
            })
        
        # Hujan: ideal <3mm
        if rr == 0:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "baik",
                "detail": "Tidak ada hujan, kondisi cerah"
            })
        elif rr < 3:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "baik",
                "detail": "Hujan ringan, masih aman untuk aktivitas"
            })
        elif rr < 6:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "buruk",
                "detail": "Hujan sedang (3-6mm), mengurangi kenyamanan"
            })
        else:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "buruk",
                "detail": "Hujan lebat (>6mm), tidak disarankan untuk pantai"
            })
        
        # Sunshine: ideal >6h
        if ss >= 6:
            reasons.append({
                "factor": "Penyinaran Matahari",
                "value": f"{ss}h",
                "status": "baik",
                "detail": "Cukup cerah (≥6 jam), kondisi ideal"
            })
        elif ss >= 3:
            reasons.append({
                "factor": "Penyinaran Matahari",
                "value": f"{ss}h",
                "status": "buruk",
                "detail": "Berawan (3-6 jam), kurang ideal"
            })
        else:
            reasons.append({
                "factor": "Penyinaran Matahari",
                "value": f"{ss}h",
                "status": "buruk",
                "detail": "Sangat berawan (<3 jam), kondisi kurang baik"
            })
        
        # Angin: ideal 1-20 km/h
        if 1 <= ff_kmh <= 20:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "baik",
                "detail": "Angin sepoi-sepoi, nyaman untuk aktivitas"
            })
        elif ff_kmh < 1:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "baik",
                "detail": "Tenang, kondisi sangat baik"
            })
        elif ff_kmh <= 30:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "buruk",
                "detail": "Angin cukup kencang (20-30 km/h), kurang nyaman"
            })
        else:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "buruk",
                "detail": "Angin kencang (>30 km/h), berisiko ombak besar"
            })
    
    elif activity == "hiking":
        if 18 <= tavg <= 28:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "baik",
                "detail": "Suhu ideal untuk hiking (18-28°C)"
            })
        elif tavg > 28:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "buruk",
                "detail": "Terlalu panas (>28°C), berisiko dehidrasi"
            })
        else:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "buruk",
                "detail": "Terlalu dingin (<18°C) untuk hiking"
            })
        
        if rr == 0:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "baik",
                "detail": "Tidak ada hujan, jalur kering dan aman"
            })
        elif rr < 5:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "baik",
                "detail": "Hujan ringan, jalur masih cukup aman"
            })
        else:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "buruk",
                "detail": "Hujan lebat (≥5mm), jalur licin dan berbahaya"
            })
        
        if 50 <= rh_avg <= 80:
            reasons.append({
                "factor": "Kelembaban",
                "value": f"{rh_avg}%",
                "status": "baik",
                "detail": "Kelembaban nyaman (50-80%)"
            })
        elif rh_avg > 80:
            reasons.append({
                "factor": "Kelembaban",
                "value": f"{rh_avg}%",
                "status": "buruk",
                "detail": "Terlalu lembab (>80%), tidak nyaman"
            })
        else:
            reasons.append({
                "factor": "Kelembaban",
                "value": f"{rh_avg}%",
                "status": "buruk",
                "detail": "Terlalu kering (<50%), berisiko dehidrasi"
            })
        
        if ff_kmh < 25:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "baik",
                "detail": "Angin tenang, aman untuk hiking"
            })
        else:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "buruk",
                "detail": "Angin kencang (≥25 km/h), berbahaya di ketinggian"
            })
    
    elif activity == "snorkeling":
        if 24 <= tavg <= 30:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "baik",
                "detail": "Suhu air nyaman untuk snorkeling (24-30°C)"
            })
        elif tavg > 30:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "buruk",
                "detail": "Terlalu panas (>30°C), kurang nyaman di air"
            })
        else:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "buruk",
                "detail": "Terlalu dingin (<24°C) untuk snorkeling"
            })
        
        if rr == 0:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "baik",
                "detail": "Tidak ada hujan, visibilitas air jernih"
            })
        elif rr < 4:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "baik",
                "detail": "Hujan ringan, visibilitas masih baik"
            })
        else:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "buruk",
                "detail": "Hujan lebat (≥4mm), air keruh, visibilitas buruk"
            })
        
        if ss >= 4:
            reasons.append({
                "factor": "Penyinaran Matahari",
                "value": f"{ss}h",
                "status": "baik",
                "detail": "Cukup cerah (≥4 jam), visibilitas bawah air baik"
            })
        else:
            reasons.append({
                "factor": "Penyinaran Matahari",
                "value": f"{ss}h",
                "status": "buruk",
                "detail": "Terlalu berawan (<4 jam), visibilitas kurang"
            })
        
        if 6 <= ff_kmh <= 20:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "baik",
                "detail": "Angin ideal (6-20 km/h), arus laut tenang"
            })
        elif ff_kmh < 6:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "baik",
                "detail": "Sangat tenang, kondisi sempurna"
            })
        else:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "buruk",
                "detail": "Angin kencang (>20 km/h), arus berbahaya"
            })
    
    elif activity == "rafting":
        if 20 <= tavg <= 32:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "baik",
                "detail": "Suhu nyaman untuk aktivitas air (20-32°C)"
            })
        else:
            reasons.append({
                "factor": "Suhu",
                "value": f"{tavg}°C",
                "status": "buruk",
                "detail": "Suhu kurang ideal untuk rafting"
            })
        
        if 1 <= rr <= 8:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "baik",
                "detail": "Debit sungai ideal (1-8mm), arus menantang tapi aman"
            })
        elif rr == 0:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "buruk",
                "detail": "Tidak ada hujan, debit sungai mungkin rendah"
            })
        else:
            reasons.append({
                "factor": "Hujan",
                "value": f"{rr}mm",
                "status": "buruk",
                "detail": "Hujan sangat lebat (>8mm), arus berbahaya"
            })
        
        if ff_kmh < 20:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "baik",
                "detail": "Angin tenang, aman untuk rafting"
            })
        else:
            reasons.append({
                "factor": "Kecepatan Angin",
                "value": f"{ff_kmh} km/h",
                "status": "buruk",
                "detail": "Angin kencang (≥20 km/h), kurang aman"
            })
        
        if rh_avg < 90:
            reasons.append({
                "factor": "Kelembaban",
                "value": f"{rh_avg}%",
                "status": "baik",
                "detail": "Kelembaban dalam batas normal"
            })
    
    baik_count = sum(1 for r in reasons if r["status"] == "baik")
    buruk_count = sum(1 for r in reasons if r["status"] == "buruk")
    
    if pred == 1:
        summary = f"Aktivitas {activity} diprediksi LAYAK dengan {baik_count} kondisi mendukung"
        if buruk_count > 0:
            summary += f" dan {buruk_count} kondisi kurang ideal"
    else:
        summary = f"Aktivitas {activity} diprediksi TIDAK LAYAK karena {buruk_count} kondisi tidak mendukung"
    
    logger.debug("Generated explanation for %s: status=%s, reasons=%d", activity, status, len(reasons))
    
    return {
        "status": status,
        "proba_pct": proba_pct,
        "reasons": reasons,
        "summary": summary
    }



def fetch_forecast(lat: float, lon: float) -> dict:
    logger.debug("Fetching forecast for lat=%s lon=%s", lat, lon)
    if not API_KEY:
        logger.error("OPENWEATHER_API_KEY not set")
        return {"ok": False, "error": "OPENWEATHER_API_KEY belum di-set"}



    try:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "appid": API_KEY, "units": UNITS, "lang": LANG},
            timeout=15,
        )
    except Exception as e:
        logger.exception("Request to OpenWeather failed")
        return {"ok": False, "error": f"Request error: {e}"}



    if r.status_code != 200:
        logger.error("OpenWeather HTTP %s: %s", r.status_code, r.text[:200])
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}



    data = r.json()
    city = data.get("city", {})
    place = city.get("name", DEFAULT_PLACE)
    logger.debug("Forecast city=%s", place)



    tz_offset = city.get("timezone", 0)
    tz_local = timezone(timedelta(seconds=tz_offset))



    sunrise_utc = city.get("sunrise")
    sunset_utc = city.get("sunset")
    if not sunrise_utc or not sunset_utc:
        logger.error("No sunrise/sunset in API response")
        return {"ok": False, "place": place, "error": "Data sunrise/sunset tidak ditemukan dari API"}



    sunrise_local = datetime.fromtimestamp(sunrise_utc, tz=tz_local).replace(tzinfo=None)
    sunset_local = datetime.fromtimestamp(sunset_utc, tz=tz_local).replace(tzinfo=None)



    buckets = defaultdict(list)
    for it in data.get("list", []):
        dt_local = datetime.fromtimestamp(it["dt"], tz=tz_local).replace(tzinfo=None)
        it["_start"] = dt_local
        it["_end"] = dt_local + timedelta(hours=3)
        buckets[dt_local.date().isoformat()].append(it)



    days = []
    for day, arr in sorted(buckets.items()):
        temps = [a["main"]["temp"] for a in arr if "main" in a]
        hums = [a["main"]["humidity"] for a in arr if "main" in a]
        winds = [a.get("wind", {}).get("speed", 0) for a in arr]
        rains_3h = [(a.get("rain", {}) or {}).get("3h", 0.0) for a in arr]



        dt_day = datetime.fromisoformat(day)
        sr_day = dt_day.replace(hour=sunrise_local.hour, minute=sunrise_local.minute, second=0, microsecond=0)
        ss_day = dt_day.replace(hour=sunset_local.hour, minute=sunset_local.minute, second=0, microsecond=0)



        sunshine_h = round(compute_sunshine_for_day(arr, sr_day, ss_day, mode=SUN_MODE), 1)



        days.append({
            "date_iso": day,
            "temp_min": round(min(temps)) if temps else None,
            "temp_max": round(max(temps)) if temps else None,
            "temp_avg": round(sum(temps) / len(temps)) if temps else None,
            "humidity_avg": round(sum(hums) / len(hums)) if hums else None,
            "wind_kmh_avg": round((sum(winds) / len(winds)) * 3.6, 1) if winds else None,
            "rain_mm": round(sum(rains_3h), 1),
            "sunshine_h": sunshine_h,
        })



    logger.debug("Forecast days=%d", len(days))
    return {
        "ok": True,
        "place": place,
        "sunrise": sunrise_local.strftime("%H:%M"),
        "sunset": sunset_local.strftime("%H:%M"),
        "timezone_offset": tz_offset,
        "days": days,
    }



def _load_model():
    global _model, _model_feature_names
    if _model is not None:
        return _model



    logger.info("Loading model from %s", MODEL_PATH)
    if not os.path.exists(MODEL_PATH):
        logger.error("Model file not found: %s", MODEL_PATH)
        raise FileNotFoundError(f"Model file tidak ditemukan: {MODEL_PATH}")



    _model = joblib.load(MODEL_PATH)



    try:
        _model_feature_names = list(_model.estimators_[0].feature_names_in_)
    except Exception:
        _model_feature_names = list(getattr(_model, "feature_names_in_", []))



    if not _model_feature_names:
        logger.error("feature_names_in_ tidak ditemukan di model")
        raise RuntimeError("Tidak bisa membaca feature_names_in_ dari model. Pastikan sklearn>=1.0 saat training.")



    logger.info("Model loaded. n_features=%d", len(_model_feature_names))
    logger.debug("Feature names: %s", _model_feature_names)
    return _model



def build_features_from_meteo(tavg, rh_avg, rr, ss, ff_kmh):
    """
    Replikasi persis pipeline di notebook.
    """
    tavg = float(tavg or 0.0)
    rh_avg = float(rh_avg or 0.0)
    rr = float(rr or 0.0)
    ss = float(ss or 0.0)
    ff_kmh = float(ff_kmh or 0.0)



    ff_avg = ff_kmh / 3.6  # m/s



    cc_equiv = ss_to_cc_equiv(ss)



    TC = lookup_score(tavg, TC_TABLE)
    A = lookup_score(cc_equiv, A_TABLE)
    P = lookup_score(rr, P_TABLE)
    Wv = lookup_score(ff_kmh, W_TABLE)
    hci_beach = 2 * TC + 4 * A + 3 * P + Wv
    hci_beach = float(np.round(hci_beach, 0))



    cc_from_ss = 100.0 * (1.0 - ss / 12.0)



    ctci_bali = compute_ctci_bali_scalar(tavg, rh_avg, rr, ss, ff_kmh)



    features = {
        "TAVG": tavg,
        "RH_AVG": rh_avg,
        "RR": rr,
        "SS": ss,
        "FF_AVG": ff_avg,
        "FF_AVG_kmh": ff_kmh,
        "CC_equiv": cc_equiv,
        "HCI_beach_0_100": hci_beach,
        "CC_from_SS": cc_from_ss,
        "CTCI_Bali": ctci_bali,
    }
    logger.debug("Built features from meteo: %s", features)
    return features



def _predict_with_features_dict(feat_dict: dict):
    mdl = _load_model()



    df_raw = pd.DataFrame([feat_dict])
    for c in df_raw.columns:
        df_raw[c] = pd.to_numeric(df_raw[c], errors="coerce").fillna(0.0)



    for feat in _model_feature_names:
        if feat not in df_raw.columns:
            df_raw[feat] = 0.0



    X = df_raw[_model_feature_names].copy()
    logger.debug("X_for_model=%s", X.iloc[0].to_dict())



    y_pred = mdl.predict(X)[0].tolist()
    logger.debug("Raw y_pred=%s", y_pred)



    probas = []
    try:
        prob_list = mdl.predict_proba(X)
        for i, p in enumerate(prob_list):
            if hasattr(p, "shape") and p.shape[1] == 2:
                probas.append(float(p[0, 1]))
            else:
                probas.append(float(y_pred[i]))
    except Exception:
        logger.exception("predict_proba gagal, fallback pakai y_pred sebagai proba")
        probas = [float(v) for v in y_pred]



    logger.debug("Probabilities=%s", probas)
    return y_pred, probas, df_raw, X



def predict_for_day_data(day_data: dict) -> dict:
    try:
        logger.debug("Predict for day_data=%s", day_data)



        tavg = day_data.get("temp_avg", 0)
        rh = day_data.get("humidity_avg", 0)
        rr = day_data.get("rain_mm", 0)
        ss = day_data.get("sunshine_h", 0)
        ff_kmh = day_data.get("wind_kmh_avg", 0)



        feat = build_features_from_meteo(tavg, rh, rr, ss, ff_kmh)
        y_pred, probas, df_raw, X = _predict_with_features_dict(feat)



        predictions = []
        for i, name in enumerate(LABEL_NAMES):
            pred_val = int(y_pred[i]) if i < len(y_pred) else 0
            proba_val = float(probas[i]) if i < len(probas) else 0.0
            
            explanation = generate_layak_explanation(
                name.strip(), 
                feat, 
                pred_val, 
                proba_val
            )
            
            predictions.append({
                "label": name.strip(),
                "pred": pred_val,
                "proba_1": round(proba_val, 4),
                "explanation": explanation
            })



        logger.debug("Predictions=%s", predictions)
        return {
            "ok": True,
            "predictions": predictions,
            "features_used": df_raw.iloc[0].to_dict(),
        }
    except Exception as e:
        logger.exception("Error in predict_for_day_data")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}




def fetch_beach_forecast_parallel(loc: dict) -> dict:
    logger.info("Processing location %s", loc["name"])
    try:
        forecast_data = fetch_forecast(loc["lat"], loc["lon"])
        if not forecast_data.get("ok"):
            logger.error("Forecast error for %s: %s", loc["name"], forecast_data.get("error"))
            return {"beach": loc["name"], "ok": False, "error": forecast_data.get("error", "Unknown error")}



        all_days = forecast_data["days"]
        days = all_days[:5] if len(all_days) >= 5 else all_days



        # PERBAIKAN TIMEZONE: Gunakan timezone yang sama dengan data forecast
        tz_offset = forecast_data.get("timezone_offset", 28800)  # default 28800 = UTC+8 (WITA Bali)
        tz_local = timezone(timedelta(seconds=tz_offset))
        today_iso = datetime.now(tz_local).date().isoformat()
        
        logger.debug("Today in local timezone (%s): %s", tz_local, today_iso)
        
        for day in days:
            ml_result = predict_for_day_data(day)
            preds = ml_result.get("predictions", []) or []
            day["ml_predictions"] = preds
            day["ml_ok"] = ml_result.get("ok", False)
            day["features"] = ml_result.get("features_used", {})



            day["ml"] = {
                p["label"].lower(): {"pred": p.get("pred"), "proba": p.get("proba_1")}
                for p in preds
            }
            day["is_today"] = (day["date_iso"] == today_iso)



        return {
            "beach": loc["name"],
            "lat": loc["lat"],
            "lon": loc["lon"],
            "ok": True,
            "sunrise": forecast_data.get("sunrise"),
            "sunset": forecast_data.get("sunset"),
            "days": days,
        }
    except Exception as e:
        logger.exception("Error in fetch_beach_forecast_parallel for %s", loc["name"])
        return {"beach": loc["name"], "ok": False, "error": str(e)}



@app.route("/api/beaches-forecast")
def api_beaches_forecast():
    activity = request.args.get("activity", "pantai").lower()
    logger.info("GET /api/beaches-forecast activity=%s", activity)
    try:
        if activity not in ACTIVITY_LOCATIONS:
            logger.warning("Invalid activity=%s", activity)
            return jsonify({
                "ok": False,
                "error": f"Invalid activity. Must be one of: {', '.join(ACTIVITY_LOCATIONS.keys())}"
            }), 400



        locations = ACTIVITY_LOCATIONS[activity]



        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_beach_forecast_parallel, loc): loc for loc in locations}
            results = [future.result() for future in concurrent.futures.as_completed(futures)]



        results.sort(key=lambda x: x.get("beach", ""))



        for result in results:
            if result.get("ok") and result.get("days"):
                ok_days, probs = 0, []
                for day in result["days"]:
                    act = (day.get("ml") or {}).get(activity, {})
                    if act.get("pred") == 1:
                        ok_days += 1
                    if act.get("proba") is not None:
                        probs.append(float(act["proba"]))
                total_days = len(result["days"])
                result["summary"] = {
                    "days_ok": ok_days,
                    "days_total": total_days,
                    "avg_proba": round(sum(probs) / len(probs), 3) if probs else 0.0,
                }



        logger.info("Returning %d locations for activity=%s", len(results), activity)
        return jsonify({
            "ok": True,
            "activity": activity,
            "updated_at": datetime.now().isoformat(),
            "locations": results
        }), 200



    except Exception as e:
        logger.exception("Error in /api/beaches-forecast")
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500



@app.route("/")
def home():
    logger.info("GET / (home)")
    return render_template(
        "beaches.html",
        today=id_date(datetime.now()),
        nowtime=datetime.now().strftime("%H.%M.%S"),
        activity_locations=ACTIVITY_LOCATIONS,
    )



@app.route("/api/predict", methods=["POST", "GET"])
def api_predict():
    logger.info("Hit /api/predict method=%s", request.method)
    try:
        _load_model()



        if request.method == "POST":
            src = request.get_json(silent=True) or {}
        else:
            src = request.args or {}
        logger.debug("Raw payload=%s", src)



        tavg = src.get("TAVG") or src.get("tavg") or src.get("temp_avg")
        rh = src.get("RH_AVG") or src.get("rh_avg") or src.get("humidity_avg")
        rr = src.get("RR") or src.get("rr") or src.get("rain_mm")
        ss = src.get("SS") or src.get("ss") or src.get("sunshine_h")
        ff_kmh = src.get("FF_AVG_kmh") or src.get("ff_avg_kmh") or src.get("wind_kmh_avg")



        if tavg is None or rh is None or rr is None or ss is None or ff_kmh is None:
            logger.warning("Missing minimal fields in /api/predict")
            return {
                "ok": False,
                "error": "Field minimal: TAVG, RH_AVG, RR, SS, FF_AVG_kmh (atau nama ekuivalen).",
            }, 400



        feat = build_features_from_meteo(tavg, rh, rr, ss, ff_kmh)
        y_pred, probas, df_raw, X = _predict_with_features_dict(feat)



        results = []
        for i, name in enumerate(LABEL_NAMES):
            pred_val = int(y_pred[i]) if i < len(y_pred) else 0
            proba_val = float(probas[i]) if i < len(probas) else 0.0
            
            explanation = generate_layak_explanation(
                name.strip(), 
                feat, 
                pred_val, 
                proba_val
            )
            
            results.append({
                "label": name.strip(),
                "pred": pred_val,
                "proba_1": round(proba_val, 4),
                "explanation": explanation
            })



        logger.info("Prediction success for /api/predict")
        return {
            "ok": True,
            "model_path": MODEL_PATH,
            "features_used": _model_feature_names,
            "input_received": df_raw.iloc[0].to_dict(),
            "X_for_model": {k: float(v) for k, v in X.iloc[0].to_dict().items()},
            "predictions": results,
        }, 200



    except FileNotFoundError as e:
        logger.exception("Model file not found in /api/predict")
        return {"ok": False, "error": str(e)}, 500
    except Exception as e:
        logger.exception("Unhandled error in /api/predict")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500



if __name__ == "__main__":
    logger.info("Running Flask development server on 127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)