from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import concurrent.futures
import os
import pickle
import requests
import pandas as pd
from dotenv import load_dotenv

# APLIKASI & KONFIGURASI GLOBAL

# Memuat variabel lingkungan dari .env (API key, dll)
load_dotenv()

# Inisialisasi aplikasi Flask
app = Flask(__name__)

# OpenWeather config
API_KEY = os.getenv("OPENWEATHER_API_KEY", "aef2fdd2aa915a9a7a7a7e3835c66468")
UNITS = "metric"      # gunakan °C, m/s dari OpenWeather
LANG = "id"           # respons deskriptif bahasa Indonesia
DEFAULT_PLACE = os.getenv("DEFAULT_PLACE", "Bali")

# Mode perhitungan sunshine
# "effective": jam siang * (1 - cloud) per slot 3 jam
# "daylength": panjang siang murni
SUN_MODE = "effective"

# Konfigurasi Model ML
MODEL_PATH = "multi_rf_model.pkl"
LABEL_NAMES = os.getenv("ML_LABEL_NAMES", "pantai,hiking,snorkeling,rafting").split(",")

# Model & fitur
_model = None
_model_feature_names = None

# 1. DATA LOKASI

# Kumpulan koordinat untuk setiap kategori aktivitas (dipakai oleh endpoint)
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

# Pemetaan aktivitas -> daftar lokasi (dipilih dari radio button di UI)
ACTIVITY_LOCATIONS = {
    "pantai": PANTAI_LOCATIONS,
    "hiking": HIKING_LOCATIONS,
    "snorkeling": SNORKELING_LOCATIONS,
    "rafting": RAFTING_LOCATIONS,
}

# 2. UTILITAS WAKTU & SUNSHINE

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
        clouds = (a.get("clouds", {}) or {}).get("all", 50)  # default awan 50% bila kosong
        daylight = overlap_daylight(a["_start"], a["_end"], sr_day, ss_day)
        sun_hours += daylight * max(0.0, 1.0 - clouds / 100.0)
    return sun_hours

# 3. FETCH FORECAST (OpenWeather 5-day/3-hour) + ringkasan harian


def fetch_forecast(lat: float, lon: float) -> dict:
    # Validasi API key
    if not API_KEY:
        return {"ok": False, "error": "OPENWEATHER_API_KEY belum di-set"}

    # Panggil endpoint forecast
    r = requests.get(
        "https://api.openweathermap.org/data/2.5/forecast",
        params={"lat": lat, "lon": lon, "appid": API_KEY, "units": UNITS, "lang": LANG},
        timeout=10,
    )
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

    data = r.json()
    city = data.get("city", {})
    place = city.get("name", DEFAULT_PLACE)

    # Siapkan zona waktu lokal berdasarkan offset OpenWeather
    tz_offset = city.get("timezone", 0)
    tz_local = timezone(timedelta(seconds=tz_offset))

    # Sunrise/sunset UTC untuk lokasi kota
    sunrise_utc = city.get("sunrise")
    sunset_utc = city.get("sunset")
    if not sunrise_utc or not sunset_utc:
        return {"ok": False, "place": place, "error": "Data sunrise/sunset tidak ditemukan dari API"}

    # Konversi ke jam lokal
    sunrise_local = datetime.fromtimestamp(sunrise_utc, tz=tz_local).replace(tzinfo=None)
    sunset_local = datetime.fromtimestamp(sunset_utc, tz=tz_local).replace(tzinfo=None)

    # Bucket semua slot 3-jam ke dalam 'hari lokal'
    buckets = defaultdict(list)
    for it in data.get("list", []):
        dt_local = datetime.fromtimestamp(it["dt"], tz=tz_local).replace(tzinfo=None)
        it["_start"] = dt_local
        it["_end"] = dt_local + timedelta(hours=3)
        buckets[dt_local.date().isoformat()].append(it)

    # Ringkasan statistik per-hari
    days = []
    for day, arr in sorted(buckets.items()):
        temps = [a["main"]["temp"] for a in arr if "main" in a]
        hums = [a["main"]["humidity"] for a in arr if "main" in a]
        winds = [a.get("wind", {}).get("speed", 0) for a in arr]
        rains_3h = [(a.get("rain", {}) or {}).get("3h", 0.0) for a in arr]

        # Bentuk rentang siang untuk tanggal 'day'
        dt_day = datetime.fromisoformat(day)
        sr_day = dt_day.replace(hour=sunrise_local.hour, minute=sunrise_local.minute, second=0, microsecond=0)
        ss_day = dt_day.replace(hour=sunset_local.hour, minute=sunset_local.minute, second=0, microsecond=0)

        # Sunshine harian (mengikuti SUN_MODE)
        sunshine_h = round(compute_sunshine_for_day(arr, sr_day, ss_day, mode=SUN_MODE), 1)

        days.append({
            "date_iso": day,
            "temp_min": round(min(temps)) if temps else None,
            "temp_max": round(max(temps)) if temps else None,
            "temp_avg": round(sum(temps) / len(temps)) if temps else None,
            "humidity_avg": round(sum(hums) / len(hums)) if hums else None,
            "wind_kmh_avg": round((sum(winds) / len(winds)) * 3.6, 1) if winds else None,  # m/s -> km/h
            "rain_mm": round(sum(rains_3h), 1),   # total hujan harian
            "sunshine_h": sunshine_h,             # jam penyinaran efektif
        })

    return {
        "ok": True,
        "place": place,
        "sunrise": sunrise_local.strftime("%H:%M"),
        "sunset": sunset_local.strftime("%H:%M"),
        "days": days,
    }

# 4. ML: LOADING MODEL, PREPROCESS, & PREDIKSI PER HARI

def _load_model():
    global _model, _model_feature_names
    if _model is not None:
        return _model

    if not os.path.exists(MODEL_PATH):
        raise FileNotFoundError(f"Model file tidak ditemukan: {MODEL_PATH}")

    with open(MODEL_PATH, "rb") as f:
        _model = pickle.load(f)

    # Coba ambil feature_names_in_ dari salah satu estimator (MultiOutput)
    try:
        _model_feature_names = list(_model.estimators_[0].feature_names_in_)
    except Exception:
        _model_feature_names = list(getattr(_model, "feature_names_in_", []))

    # Fallback ke ENV bila metadata fitur tidak tersedia
    if not _model_feature_names:
        env_feats = os.getenv("ML_FEATURE_NAMES", "")
        if not env_feats:
            raise RuntimeError(
                "Gagal mendeteksi nama fitur dari model. "
                "Set ENV ML_FEATURE_NAMES atau latih ulang sklearn>=1.0."
            )
        _model_feature_names = [c.strip() for c in env_feats.split(",") if c.strip()]

    return _model

def _preprocess_payload_to_df(payload: dict):
    payload = {str(k).lower(): v for k, v in payload.items()}

    base_keys = ["rr", "ss", "tn", "tx", "tavg", "rh_avg", "ff_x", "ff_avg_kmh", "temp_range"]
    for k in base_keys:
        payload.setdefault(k, 0)

    df_raw = pd.DataFrame([payload])

    # Pastikan semua numeric & tanpa NaN
    for col in base_keys:
        df_raw[col] = pd.to_numeric(df_raw[col], errors="coerce").fillna(0.0)

    # Turunan: ff_avg (m/s) dari km/h bila diminta oleh model
    if "ff_avg" in _model_feature_names and "ff_avg" not in df_raw.columns:
        df_raw["ff_avg"] = df_raw["ff_avg_kmh"] / 3.6

    # Turunan: temp_range bila 0 namun tn/tx tersedia
    if "temp_range" in _model_feature_names and "temp_range" in df_raw.columns:
        need_fill = (df_raw["temp_range"] == 0).all()
        if need_fill and ("tn" in df_raw.columns) and ("tx" in df_raw.columns):
            df_raw["temp_range"] = (df_raw["tx"] - df_raw["tn"]).fillna(df_raw["temp_range"])

    # Lengkapi kolom agar urut sesuai training
    for feat in _model_feature_names:
        if feat not in df_raw.columns:
            df_raw[feat] = 0.0

    X = df_raw[_model_feature_names].copy()
    return X, df_raw

def _predict_with_proba(X: pd.DataFrame):
    mdl = _load_model()
    y_pred = mdl.predict(X)[0].tolist()

    probas = []
    try:
        prob_list = mdl.predict_proba(X)  # list: 1 array per label
        for i, p in enumerate(prob_list):
            if hasattr(p, "shape") and p.shape[1] == 2:
                probas.append(float(p[0, 1]))
            else:
                probas.append(float(y_pred[i]))  # fallback bila non-biner
    except Exception:
        probas = [float(v) for v in y_pred]

    return y_pred, probas

def predict_for_day_data(day_data: dict) -> dict:
    try:
        _load_model()
        payload = {
            "rr": day_data.get("rain_mm", 0),
            "ss": day_data.get("sunshine_h", 0),
            "tn": day_data.get("temp_min", 0),
            "tx": day_data.get("temp_max", 0),
            "tavg": day_data.get("temp_avg", 0),
            "rh_avg": day_data.get("humidity_avg", 0),
            "ff_x": 0,
            "ff_avg_kmh": day_data.get("wind_kmh_avg", 0),
            "temp_range": (day_data.get("temp_max", 0) - day_data.get("temp_min", 0)),
        }
        X, _ = _preprocess_payload_to_df(payload)
        y_pred, probas = _predict_with_proba(X)

        predictions = []
        for i, name in enumerate(LABEL_NAMES):
            predictions.append({
                "label": name.strip(),
                "pred": int(y_pred[i]) if i < len(y_pred) else None,
                "proba_1": round(float(probas[i]), 4) if i < len(probas) else None,
            })
        return {"ok": True, "predictions": predictions}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

# 5. PARAREL FETCH PER LOKASI + SUNTIKAN HASIL ML KE HARIAN

def fetch_beach_forecast_parallel(loc: dict) -> dict:
    """
    Ambil forecast untuk 1 lokasi (maks 5 hari), lakukan prediksi ML untuk tiap hari,
    serta tandai 'is_today' (membantu penyorotan kolom HARI INI pada UI).
    """
    try:
        forecast_data = fetch_forecast(loc["lat"], loc["lon"])
        if not forecast_data.get("ok"):
            return {"beach": loc["name"], "ok": False, "error": forecast_data.get("error", "Unknown error")}

        # Ambil maksimal 5 hari (Hari H → H+4) agar sinkron dengan UI
        all_days = forecast_data["days"]
        days = all_days[:5] if len(all_days) >= 5 else all_days

        # Tandai hari ini & injeksikan hasil ML
        today_iso = datetime.now().date().isoformat()
        for day in days:
            ml_result = predict_for_day_data(day)
            preds = ml_result.get("predictions", []) or []
            day["ml_predictions"] = preds
            day["ml_ok"] = ml_result.get("ok", False)
            # Map cepat untuk akses per-aktivitas di frontend
            day["ml"] = {p["label"].lower(): {"pred": p.get("pred"), "proba": p.get("proba_1")} for p in preds}
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
        return {"beach": loc["name"], "ok": False, "error": str(e)}

# 6. API ENDPOINTS

@app.route("/api/beaches-forecast")
def api_beaches_forecast():
    try:
        activity = request.args.get("activity", "pantai").lower()
        if activity not in ACTIVITY_LOCATIONS:
            return jsonify({
                "ok": False,
                "error": f"Invalid activity. Must be one of: {', '.join(ACTIVITY_LOCATIONS.keys())}"
            }), 400

        locations = ACTIVITY_LOCATIONS[activity]

        # Paralel fetch per lokasi agar respons cepat
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_beach_forecast_parallel, loc): loc for loc in locations}
            results = [future.result() for future in concurrent.futures.as_completed(futures)]

        # Urutkan hasil supaya stabil di UI
        results.sort(key=lambda x: x.get("beach", ""))

        # Tambahkan ringkasan (count-based) per lokasi dari label ML untuk aktivitas saat ini
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

        return jsonify({
            "ok": True,
            "activity": activity,
            "updated_at": datetime.now().isoformat(),
            "locations": results
        }), 200

    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500

# 7. ROUTE UI (Single Page)

@app.route("/")
def home():
    return render_template(
        "beaches.html",
        today=id_date(datetime.now()),
        nowtime=datetime.now().strftime("%H.%M.%S"),
        activity_locations=ACTIVITY_LOCATIONS,
    )

# 8. API PREDICT (manual testing/debugging)

@app.route("/api/predict", methods=["POST", "GET"])
def api_predict():
    try:
        _load_model()

        # Parsing payload sesuai metode
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
        else:
            payload = {
                k: request.args.get(k)
                for k in ["rr", "ss", "tn", "tx", "tavg", "rh_avg", "ff_x", "ff_avg_kmh", "temp_range"]
                if request.args.get(k) is not None
            }

        # Validasi input kosong
        if not payload:
            return {
                "ok": False,
                "error": "Tidak ada payload. Kirim JSON atau query params sesuai spesifikasi.",
                "expected_fields": ["rr", "ss", "tn", "tx", "tavg", "rh_avg", "ff_x", "ff_avg_kmh", "temp_range"],
            }, 400

        # Preprocess dan prediksi
        X, df_raw = _preprocess_payload_to_df(payload)
        y_pred, probas = _predict_with_proba(X)

        # Susun hasil agar mudah dibaca
        results = []
        for i, name in enumerate(LABEL_NAMES):
            results.append({
                "label": name.strip(),
                "pred": int(y_pred[i]) if i < len(y_pred) else None,
                "proba_1": round(float(probas[i]), 4) if i < len(probas) else None,
            })

        return {
            "ok": True,
            "model_path": MODEL_PATH,
            "features_used": _model_feature_names,
            "input_received": df_raw.iloc[0].to_dict(),
            "X_for_model": {k: (float(v) if v is not None else None) for k, v in X.iloc[0].to_dict().items()},
            "predictions": results,
        }, 200

    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}, 500
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500

# 9. MAIN

if __name__ == "__main__":
    # use_reloader=False agar tidak dobel run di Windows
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)