from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import analytics
from .database import close_pool, get_pool, initialize_database

BASE_DIR = Path(__file__).resolve().parent.parent
PATTAYA_LATITUDE = 12.9236
PATTAYA_LONGITUDE = 100.8694
USER_AGENT = "PattayaSmartTourism/0.1 (academic prototype)"
THAI_MONTH_NAMES = (
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
)

# วันนี้ตามเวลาไทย ฐานข้อมูล Supabase ใช้เวลา UTC ถ้าใช้ CURRENT_DATE ตรง ๆ ช่วงตี 0-7 จะได้วันของเมื่อวาน
TODAY_BANGKOK = "(now() AT TIME ZONE 'Asia/Bangkok')::date"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    yield
    close_pool()


app = FastAPI(title="Pattaya Smart Tourism API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def query(sql: str, parameters: list | tuple = ()) -> list[dict]:
    try:
        return _fetch_all(sql, parameters)
    except psycopg.OperationalError:
        # connection ที่ Supabase ตัดทิ้งไประหว่างว่าง pool จะคัดออกเอง ลองใหม่ครั้งเดียวด้วย connection ใหม่
        return _fetch_all(sql, parameters)


def _fetch_all(sql: str, parameters: list | tuple) -> list[dict]:
    with get_pool().connection() as connection:
        # ไม่มีพารามิเตอร์ให้ส่ง None เพื่อไม่ให้ psycopg ตีความ % ใน SQL เป็น placeholder
        return connection.execute(sql, parameters or None).fetchall()


def query_one(sql: str, parameters: list | tuple = ()) -> dict | None:
    rows = query(sql, parameters)
    return rows[0] if rows else None


# แคชผลเรียก API ภายนอก เพื่อไม่ให้ทุกครั้งที่เปิดแดชบอร์ดยิงไปที่ Open-Meteo ใหม่
_external_cache: dict[str, tuple[float, Any]] = {}


def cached_fetch(key: str, url: str, ttl_seconds: int) -> Any:
    now = time.time()
    cached = _external_cache.get(key)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    _external_cache[key] = (now, payload)
    return payload


# แคชผลวิเคราะห์ ข้อมูลนำเข้าเปลี่ยนไม่บ่อย แต่การเทียบหลายวิธีต้องคำนวณซ้ำหลายรอบ
_analysis_cache: dict[str, tuple[float, Any]] = {}
ANALYSIS_TTL_SECONDS = 900


def cached_analysis(key: str, build) -> Any:
    now = time.time()
    cached = _analysis_cache.get(key)
    if cached and now - cached[0] < ANALYSIS_TTL_SECONDS:
        return cached[1]
    value = build()
    _analysis_cache[key] = (now, value)
    return value


def monthly_tourism_series() -> tuple[list[str], list[float]]:
    """อนุกรมจำนวนนักท่องเที่ยวรายเดือน คืน (ป้ายเดือน YYYY-MM, จำนวนคน) เรียงตามเวลา"""
    rows = query(
        """SELECT calendar_year, month_id, number_of_tourists
           FROM tourism_monthly ORDER BY calendar_year, month_id"""
    )
    labels = [f"{row['calendar_year']:04d}-{row['month_id']:02d}" for row in rows]
    return labels, [float(row["number_of_tourists"]) for row in rows]


def shift_month(label: str, steps: int) -> str:
    """เลื่อนป้ายเดือน YYYY-MM ไปข้างหน้า steps เดือน"""
    index = int(label[:4]) * 12 + int(label[5:7]) - 1 + steps
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def months_behind_today(label: str) -> int:
    """ข้อมูลล่าสุดช้ากว่าเดือนปัจจุบัน (เวลาไทย) กี่เดือน"""
    today = datetime.now(timezone(timedelta(hours=7)))
    return (today.year * 12 + today.month) - (int(label[:4]) * 12 + int(label[5:7]))


# ---------------------------------------------------------------- หน้าเว็บ


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


# ---------------------------------------------------------------- สภาพอากาศ (Open-Meteo)


@app.get("/api/weather/live")
def live_weather() -> dict:
    parameters = urlencode({
        "latitude": PATTAYA_LATITUDE,
        "longitude": PATTAYA_LONGITUDE,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "timezone": "Asia/Bangkok",
    })
    payload = cached_fetch("weather_live", f"https://api.open-meteo.com/v1/forecast?{parameters}", 600)
    current = payload["current"]
    return {
        "location": "พัทยา",
        "observed_at": current["time"],
        "temperature_c": current["temperature_2m"],
        "humidity_percent": current["relative_humidity_2m"],
        "precipitation_mm": current["precipitation"],
        "wind_speed_kmh": current["wind_speed_10m"],
        "weather_code": current["weather_code"],
        "source": "Open-Meteo",
    }


@app.get("/api/weather/forecast")
def weather_forecast(days: int = Query(default=7, ge=1, le=16)) -> list[dict]:
    """พยากรณ์รายวัน ใช้จับคู่กับวันหยุดเพื่อประเมินว่าช่วงวันหยุดที่จะถึงอากาศเอื้อหรือไม่"""
    parameters = urlencode({
        "latitude": PATTAYA_LATITUDE,
        "longitude": PATTAYA_LONGITUDE,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
        "timezone": "Asia/Bangkok",
        "forecast_days": days,
    })
    payload = cached_fetch(f"weather_forecast_{days}", f"https://api.open-meteo.com/v1/forecast?{parameters}", 1800)
    daily = payload["daily"]
    # Open-Meteo ส่งวันที่มาเป็นข้อความ จึงแปลงคอลัมน์ DATE เป็นข้อความก่อนจับคู่
    holiday_dates = {row["date"]: row["local_name"] or row["name"] for row in query(
        "SELECT date::text AS date, name, local_name FROM public_holidays"
    )}
    return [
        {
            "date": date,
            "weather_code": daily["weather_code"][index],
            "temperature_max_c": daily["temperature_2m_max"][index],
            "temperature_min_c": daily["temperature_2m_min"][index],
            "precipitation_mm": daily["precipitation_sum"][index],
            "precipitation_probability_percent": daily["precipitation_probability_max"][index],
            "holiday": holiday_dates.get(date),
        }
        for index, date in enumerate(daily["time"])
    ]


# ---------------------------------------------------------------- ภาพรวม


@app.get("/api/summary")
def summary() -> dict:
    """KPI จากข้อมูลจริงทั้งหมด ไม่มีค่าจำลอง"""
    latest_tourism = query_one(
        """SELECT calendar_year, month_id, month_name, number_of_tourists, location
           FROM tourism_monthly ORDER BY calendar_year DESC, month_id DESC LIMIT 1"""
    )
    tourism_year = query_one(
        """SELECT calendar_year, SUM(number_of_tourists) AS tourists
           FROM tourism_monthly GROUP BY calendar_year ORDER BY calendar_year DESC LIMIT 1"""
    )
    tourism_previous = query_one(
        """SELECT SUM(number_of_tourists) AS tourists FROM tourism_monthly
           WHERE calendar_year = (SELECT MAX(calendar_year) - 1 FROM tourism_monthly)"""
    )
    traffic_year = query_one(
        """SELECT calendar_year, SUM(vehicle_volume) AS vehicles,
                  COUNT(DISTINCT intersection_name) AS intersections
           FROM zone_traffic_volume GROUP BY calendar_year ORDER BY calendar_year DESC LIMIT 1"""
    )
    poi_total = query_one("SELECT COUNT(*) AS total, COUNT(DISTINCT category) AS categories FROM poi_businesses")
    population = query_one(
        """SELECT calendar_year, SUM(population) AS population FROM demographic_profiles
           WHERE category = 'เพศ' GROUP BY calendar_year ORDER BY calendar_year DESC LIMIT 1"""
    )
    next_holiday = query_one(
        f"""SELECT date, name, local_name FROM public_holidays
            WHERE date >= {TODAY_BANGKOK} ORDER BY date LIMIT 1"""
    )
    coverage = query_one(
        """SELECT COUNT(DISTINCT intersection_name) AS total,
                  COUNT(DISTINCT CASE WHEN latitude IS NOT NULL THEN intersection_name END) AS geocoded
           FROM zone_traffic_volume"""
    )

    tourists_this_year = (tourism_year or {}).get("tourists")
    tourists_last_year = (tourism_previous or {}).get("tourists")
    year_over_year = None
    if tourists_this_year and tourists_last_year:
        year_over_year = round((tourists_this_year - tourists_last_year) / tourists_last_year * 100, 1)

    return {
        "tourism": {
            "latest_month": latest_tourism,
            "year": (tourism_year or {}).get("calendar_year"),
            "tourists_year_total": tourists_this_year,
            "year_over_year_percent": year_over_year,
        },
        "traffic": traffic_year,
        "poi": poi_total,
        "population": population,
        "next_holiday": next_holiday,
        "geocode_coverage": coverage,
    }


@app.get("/api/sources")
def sources() -> list[dict]:
    """แหล่งที่มาของข้อมูลทุกชุดที่นำเข้า ใช้แสดงการอ้างอิงบนแดชบอร์ด"""
    # นำเข้าซ้ำได้หลายครั้ง จึงรายงานยอดของครั้งล่าสุดเท่านั้น ไม่ใช่ผลรวมสะสมของทุกครั้ง
    return query(
        """SELECT s.name, s.source_type, s.endpoint,
                  to_char(latest.imported_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI:SS')
                      AS last_imported_at,
                  latest.records_success AS records,
                  latest.records_failed AS records_failed
           FROM data_sources s
           LEFT JOIN imports latest ON latest.id = (
               SELECT id FROM imports WHERE data_source_id = s.id
               ORDER BY imported_at DESC, id DESC LIMIT 1
           )
           ORDER BY s.name"""
    )


# ---------------------------------------------------------------- โซน / แยก (แผนที่)

# รัศมีโดยประมาณรอบแยกที่นับว่าเป็น "ย่าน" เดียวกัน ใช้หาจำนวนสถานที่ท่องเที่ยว/ธุรกิจรอบแยก
# 0.005 องศา ~ 550 เมตร ที่ละติจูดของพัทยา
ZONE_RADIUS_DEGREES = 0.005


@app.get("/api/zones")
def zones(year: int | None = Query(default=None)) -> list[dict]:
    """แยกจริงจากชุดข้อมูลปริมาณรถ พร้อมจำนวนธุรกิจท่องเที่ยวรอบแยกจาก OpenStreetMap

    แทนที่ข้อมูลรายโซนจำลองเดิม ทุกค่าที่คืนมาอ้างอิงข้อมูลจริงทั้งหมด
    """
    clause = "WHERE calendar_year = %s" if year else ""
    parameters: list[Any] = [year] if year else []
    intersections = query(
        f"""SELECT intersection_name, latitude, longitude, geocode_method, geocode_detail,
                   SUM(vehicle_volume) AS vehicle_volume,
                   COUNT(DISTINCT calendar_year || '-' || month_id) AS months,
                   MIN(calendar_year) AS first_year, MAX(calendar_year) AS last_year
            FROM zone_traffic_volume {clause}
            GROUP BY intersection_name, latitude, longitude, geocode_method, geocode_detail
            ORDER BY vehicle_volume DESC""",
        parameters,
    )
    points = query("SELECT category, latitude, longitude FROM poi_businesses")
    for intersection in intersections:
        latitude, longitude = intersection["latitude"], intersection["longitude"]
        if latitude is None or longitude is None:
            intersection["poi_nearby"] = None
            intersection["poi_nearby_by_category"] = None
            continue
        nearby: dict[str, int] = {}
        for point in points:
            if (
                abs(point["latitude"] - latitude) <= ZONE_RADIUS_DEGREES
                and abs(point["longitude"] - longitude) <= ZONE_RADIUS_DEGREES
            ):
                nearby[point["category"]] = nearby.get(point["category"], 0) + 1
        intersection["poi_nearby"] = sum(nearby.values())
        intersection["poi_nearby_by_category"] = dict(
            sorted(nearby.items(), key=lambda item: item[1], reverse=True)[:5]
        )
        intersection["vehicle_volume_per_month"] = round(
            intersection["vehicle_volume"] / intersection["months"]
        ) if intersection["months"] else None
    return intersections


# ---------------------------------------------------------------- แนวโน้ม


@app.get("/api/trends")
def trends() -> list[dict]:
    """จำนวนนักท่องเที่ยวรายเดือนจาก Open Data เมืองพัทยา พร้อมจำนวนวันหยุดในเดือนนั้น"""
    rows = query(
        """SELECT to_char(make_date(calendar_year, month_id, 1), 'YYYY-MM-DD') AS date,
                  calendar_year, month_id, month_name, number_of_tourists AS visitors
           FROM tourism_monthly ORDER BY calendar_year, month_id"""
    )
    holidays_by_month: dict[str, int] = {}
    for row in query("SELECT to_char(date, 'YYYY-MM') AS ym, COUNT(*) AS total FROM public_holidays GROUP BY ym"):
        holidays_by_month[row["ym"]] = row["total"]
    for row in rows:
        row["holidays"] = holidays_by_month.get(row["date"][:7], 0)
    return rows


@app.get("/api/tourism/monthly")
def monthly_tourism(year: int | None = Query(default=None)) -> list[dict]:
    clause = "WHERE calendar_year = %s" if year else ""
    parameters: list[Any] = [year] if year else []
    return query(
        f"""SELECT calendar_year_be, calendar_year, month_id, month_name,
                   number_of_tourists, unit, location, source_file
            FROM tourism_monthly {clause} ORDER BY calendar_year, month_id""",
        parameters,
    )


@app.get("/api/traffic/zones")
def traffic_zones(year: int | None = Query(default=None), month: int | None = Query(default=None)) -> list[dict]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if year:
        clauses.append("calendar_year = %s")
        parameters.append(year)
    if month:
        clauses.append("month_id = %s")
        parameters.append(month)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return query(
        f"""SELECT intersection_name, latitude, longitude, geocode_method,
                   SUM(vehicle_volume) AS vehicle_volume
            FROM zone_traffic_volume {where}
            GROUP BY intersection_name, latitude, longitude, geocode_method
            ORDER BY vehicle_volume DESC""",
        parameters,
    )


@app.get("/api/traffic/trends")
def traffic_trends(intersection_name: str | None = Query(default=None)) -> list[dict]:
    clause = "WHERE intersection_name = %s" if intersection_name else ""
    parameters: list[Any] = [intersection_name] if intersection_name else []
    return query(
        f"""SELECT to_char(make_date(calendar_year, month_id, 1), 'YYYY-MM-DD') AS date,
                   calendar_year, month_id, month_name,
                   SUM(vehicle_volume) AS vehicle_volume,
                   COUNT(DISTINCT intersection_name) AS intersections
            FROM zone_traffic_volume {clause}
            GROUP BY calendar_year, month_id, month_name ORDER BY calendar_year, month_id""",
        parameters,
    )


# ---------------------------------------------------------------- ประชากร / สถานที่ / วันหยุด


@app.get("/api/demographics")
def demographics(
    year: int | None = Query(default=None),
    category: str | None = Query(default=None),
) -> list[dict]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if year:
        clauses.append("calendar_year = %s")
        parameters.append(year)
    if category:
        clauses.append("category = %s")
        parameters.append(category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return query(
        f"""SELECT calendar_year_be, calendar_year, category, segment, population, unit, location
            FROM demographic_profiles {where}
            ORDER BY calendar_year, category, population DESC""",
        parameters,
    )


@app.get("/api/poi")
def poi(category: str | None = Query(default=None), limit: int = Query(default=200, le=3000)) -> list[dict]:
    clause = "WHERE category = %s" if category else ""
    parameters: list[Any] = [category] if category else []
    parameters.append(limit)
    return query(
        f"""SELECT name, category, address, latitude, longitude, phone, website
            FROM poi_businesses {clause} ORDER BY name LIMIT %s""",
        parameters,
    )


@app.get("/api/poi/categories")
def poi_categories() -> list[dict]:
    return query(
        """SELECT category, COUNT(*) AS total FROM poi_businesses
           GROUP BY category ORDER BY total DESC"""
    )


@app.get("/api/holidays")
def holidays(year: int | None = Query(default=None)) -> list[dict]:
    clause = "WHERE EXTRACT(YEAR FROM date) = %s" if year else ""
    parameters: list[Any] = [year] if year else []
    return query(
        f"""SELECT date, name, local_name, holiday_type, country_code
            FROM public_holidays {clause} ORDER BY date""",
        parameters,
    )


@app.get("/api/holidays/upcoming")
def upcoming_holidays(limit: int = Query(default=5, ge=1, le=50)) -> list[dict]:
    return query(
        f"""SELECT date, name, local_name, holiday_type,
                   date - {TODAY_BANGKOK} AS days_away
            FROM public_holidays WHERE date >= {TODAY_BANGKOK} ORDER BY date LIMIT %s""",
        [limit],
    )


# ---------------------------------------------------------------- พยากรณ์ / ผลกระทบ

FORECAST_SOURCE = "Open Data เมืองพัทยา ชุดข้อมูลนักท่องเที่ยวเดินทางลงเกาะล้าน (172)"
FORECAST_CAVEATS = [
    "ข้อมูลมีเพียง 48 เดือน (2022-2025) ยังนับว่าน้อยสำหรับการพยากรณ์",
    "ครอบคลุมเฉพาะผู้เดินทางลงเกาะล้าน ไม่ใช่นักท่องเที่ยวทั้งเมืองพัทยา",
    "รูปแบบฤดูกาลยังไม่คงที่เพราะเพิ่งฟื้นจากโควิด ปี 2022 เดือนพีคสูงกว่าค่าเฉลี่ยทั้งปี 69% แต่ปี 2025 เหลือ 18%",
    "ช่วงประมาณกว้าง ใช้วางแผนกำลังคนแบบคร่าว ๆ ได้ แต่ไม่ควรใช้เป็นตัวเลขผูกพัน",
]
IMPACT_CAVEATS = [
    "เป็นความสัมพันธ์ ไม่ใช่การพิสูจน์เหตุและผล",
    "ข้อมูลนักท่องเที่ยวเป็นรายเดือน จึงดูผลของฝนรายวันหรือวันหยุดแต่ละวันไม่ได้",
    "วันหยุดในฐานข้อมูลมีตั้งแต่ปี 2024 การวิเคราะห์วันหยุดจึงใช้เดือนน้อยกว่าการวิเคราะห์ฝน",
    "ฝนเป็นข้อมูลวิเคราะห์ย้อนหลัง (ERA5) ของจุดเดียวกลางเมืองพัทยา ไม่ใช่ค่าเฉลี่ยทั้งเมือง",
]


@app.get("/api/forecast/tourism")
def tourism_forecast(months: int = Query(default=6, ge=1, le=12)) -> dict:
    """พยากรณ์นักท่องเที่ยวรายเดือน เลือกวิธีจากปีตรวจสอบ และรายงานความแม่นจากปีทดสอบที่ไม่ถูกใช้เลือกวิธี"""
    labels, series = monthly_tourism_series()
    if not series:
        return {"error": "ยังไม่มีข้อมูลนักท่องเที่ยวในฐานข้อมูล", "caveats": FORECAST_CAVEATS}
    result = cached_analysis(
        f"forecast:{months}:{len(series)}:{labels[-1]}",
        lambda: analytics.forecast_series(series, months),
    )
    if "error" in result:
        return {"error": result["error"], "caveats": FORECAST_CAVEATS}

    indices = analytics.seasonal_indices(series)
    first_month = int(labels[0][5:7])
    profile = sorted(
        ({"month_id": (first_month - 1 + position) % 12 + 1, "index": round(value, 3)}
         for position, value in enumerate(indices)),
        key=lambda item: item["month_id"],
    )
    # พยากรณ์เริ่มนับจากเดือนถัดจากข้อมูลล่าสุด ไม่ใช่เดือนปัจจุบัน ถ้าข้อมูลค้างจะเห็นได้จากค่านี้
    lag = months_behind_today(labels[-1])
    caveats = list(FORECAST_CAVEATS)
    if lag >= 2:
        caveats.insert(0, f"ข้อมูลล่าสุดคือเดือน {labels[-1]} ช้ากว่าปัจจุบัน {lag} เดือน พยากรณ์จึงเริ่มจากเดือนถัดจากนั้น")
    return {
        "history": [{"month": month, "visitors": round(value)} for month, value in zip(labels, series)],
        "forecast": [{"month": shift_month(labels[-1], point["step"]), **point} for point in result["points"]],
        "model": result["model"],
        "accuracy": result["accuracy"],
        "seasonal_profile": profile,
        "data": {"last_month": labels[-1], "months_behind_today": lag, "months_available": len(series)},
        "caveats": caveats,
        "source": FORECAST_SOURCE,
    }


def impact_analysis() -> dict:
    """วัดความสัมพันธ์ของฝนและวันหยุดกับจำนวนนักท่องเที่ยว หลังตัดฤดูกาลและแนวโน้มออกแล้ว"""
    labels, series = monthly_tourism_series()
    if len(series) < 2 * analytics.MONTHS_IN_YEAR:
        return {"error": "ข้อมูลนักท่องเที่ยวน้อยกว่า 24 เดือน ยังวิเคราะห์ผลกระทบไม่ได้", "caveats": IMPACT_CAVEATS}

    visitor_index = analytics.deseasonalized_index(series)
    rain_by_month = {row["month"]: float(row["rain_mm"] or 0) for row in query(
        """SELECT to_char(date, 'YYYY-MM') AS month, SUM(precipitation_mm) AS rain_mm
           FROM weather_daily GROUP BY 1"""
    )}
    holidays_by_month = {row["month"]: row["days"] for row in query(
        "SELECT to_char(date, 'YYYY-MM') AS month, COUNT(*) AS days FROM public_holidays GROUP BY 1"
    )}

    rain_months = [(month, index) for month, index in zip(labels, visitor_index) if month in rain_by_month]
    rain = analytics.rain_impact(
        [index for _, index in rain_months],
        [rain_by_month[month] for month, _ in rain_months],
        [month for month, _ in rain_months],
    ) if len(rain_months) >= analytics.MONTHS_IN_YEAR else None

    # วันหยุดเริ่มมีข้อมูลปี 2024 จึงตัดเฉพาะช่วงที่มีข้อมูลจริง ไม่นับเดือนที่ไม่มีข้อมูลว่า "ไม่มีวันหยุด"
    first_holiday_month = min(holidays_by_month) if holidays_by_month else None
    holiday_months = [
        (month, index) for month, index in zip(labels, visitor_index)
        if first_holiday_month and month >= first_holiday_month
    ]
    holidays = analytics.holiday_impact(
        [index for _, index in holiday_months],
        [holidays_by_month.get(month, 0) for month, _ in holiday_months],
        [month for month, _ in holiday_months],
    ) if len(holiday_months) >= analytics.MONTHS_IN_YEAR else None

    return {
        "method": (
            "ตัดฤดูกาล (ratio-to-moving-average) และแนวโน้มเชิงเส้นออกจากจำนวนนักท่องเที่ยวก่อน "
            "เหลือเป็นดัชนี 1.0 = เท่าที่ควรจะเป็น แล้ววัดความสัมพันธ์กับฝนที่มากกว่าปกติของเดือนนั้น และจำนวนวันหยุด"
        ),
        "rain": rain,
        "holidays": holidays,
        "caveats": IMPACT_CAVEATS,
        "sources": [FORECAST_SOURCE, "Open-Meteo Archive (ERA5)", "World Holidays API (TH)"],
    }


@app.get("/api/impact")
def impact() -> dict:
    labels, series = monthly_tourism_series()
    return cached_analysis(f"impact:{len(series)}:{labels[-1] if labels else '-'}", impact_analysis)


# ---------------------------------------------------------------- คำแนะนำ

# สมมติฐานของคำแนะนำเรื่องกำลังคนและสต็อก เขียนไว้ตรงนี้ให้เห็นชัดว่าไม่ได้มาจากข้อมูลธุรกิจจริง
# เพราะฐานข้อมูลไม่มีข้อมูลร้านค้า พนักงาน หรือยอดขาย
STAFFING_ASSUMPTION = "สมมติว่ากำลังคนที่ต้องใช้แปรผันตรงกับจำนวนนักท่องเที่ยว"
STOCK_ASSUMPTION = "สมมติว่าสต็อกที่ต้องเตรียมแปรผันตรงกับจำนวนนักท่องเที่ยว และเผื่อของขาดไว้ที่ขอบบนของช่วงประมาณ"
# ต่ำกว่านี้ถือว่าเปลี่ยนแปลงน้อยกว่าความคลาดเคลื่อนของแบบจำลอง ไม่ควรสั่งปรับกำลังคน
MATERIAL_CHANGE_PERCENT = 5


def _weather_signal() -> Optional[dict]:
    try:
        forecast = weather_forecast(days=7)
    except Exception:  # ไม่มีเน็ต/Open-Meteo ล่ม ให้ข้ามสัญญาณอากาศไปแทนที่จะพังทั้งหน้า
        return None
    rainy = [day for day in forecast if (day["precipitation_probability_percent"] or 0) >= 60]
    holidays_ahead = [day for day in forecast if day["holiday"]]
    return {"forecast": forecast, "rainy_days": rainy, "holidays_ahead": holidays_ahead}


@app.get("/api/recommendations")
def recommendations() -> list[dict]:
    """คำแนะนำที่อนุมานจากสัญญาณข้อมูลจริง ทุกข้อระบุตัวเลขและแหล่งที่มาที่ใช้ตัดสิน"""
    results: list[dict] = []

    forecast = tourism_forecast(months=6)
    if forecast.get("forecast"):
        next_month = forecast["forecast"][0]
        history = {row["month"]: row["visitors"] for row in forecast["history"]}
        accuracy = forecast["accuracy"]
        model_source = (
            f"แบบจำลอง{forecast['model']['label']} คลาดเคลื่อนในปีทดสอบ {accuracy['test_mape_percent']}% "
            f"(วิธีพื้นฐาน {accuracy['seasonal_naive_mape_percent']}%)"
        )
        baseline = history.get(shift_month(next_month["month"], -12))
        if baseline:
            change = (next_month["visitors"] / baseline - 1) * 100
            action = (
                f"ควร{'เพิ่ม' if change > 0 else 'ลด'}กำลังคนราว {abs(change):.0f}%"
                if abs(change) >= MATERIAL_CHANGE_PERCENT
                else "ยังไม่ต้องปรับกำลังคน เพราะเปลี่ยนแปลงน้อยกว่าความคลาดเคลื่อนของแบบจำลอง"
            )
            results.append({
                "title": f"วางแผนกำลังคนเดือน {next_month['month']} (เดือนถัดจากข้อมูลล่าสุด)",
                "detail": (
                    f"พยากรณ์ {next_month['visitors']:,} คน เทียบเดือนเดียวกันปีก่อนที่ {baseline:,} คน "
                    f"({change:+.1f}%) {action}"
                ),
                "signal": "พยากรณ์นักท่องเที่ยว",
                "assumption": STAFFING_ASSUMPTION,
                "source": model_source,
            })
        if next_month.get("upper"):
            buffer_percent = (next_month["upper"] / next_month["visitors"] - 1) * 100
            results.append({
                "title": f"เตรียมสต็อกเดือน {next_month['month']} ให้รองรับถึง {next_month['upper']:,} คน",
                "detail": (
                    f"ค่าพยากรณ์อยู่ที่ {next_month['visitors']:,} คน แต่ขอบบนของช่วงประมาณ 95% คือ "
                    f"{next_month['upper']:,} คน การเผื่อสต็อกอีก {buffer_percent:.0f}% จะกันของขาดกรณีคนมากกว่าคาด"
                ),
                "signal": "ช่วงความไม่แน่นอน",
                "assumption": STOCK_ASSUMPTION,
                "source": model_source,
            })
        profile = {item["month_id"]: item["index"] for item in forecast["seasonal_profile"]}
        if profile:
            peak_month = max(profile, key=lambda month_id: profile[month_id])
            results.append({
                "title": f"เดือน{THAI_MONTH_NAMES[peak_month - 1]}เป็นเดือนที่คนมากที่สุดตามรูปฤดูกาล",
                "detail": (
                    f"เฉลี่ย 4 ปีที่ผ่านมา สูงกว่าค่าเฉลี่ยทั้งปี {(profile[peak_month] - 1) * 100:.0f}% "
                    "ควรวางแผนกำลังคนและสต็อกล่วงหน้าก่อนถึงเดือนนี้"
                ),
                "signal": "รูปแบบฤดูกาล",
                "source": FORECAST_SOURCE,
            })

    lag = forecast.get("data", {}).get("months_behind_today", 0)
    if lag >= 2:
        results.append({
            "title": f"ข้อมูลนักท่องเที่ยวค้างอยู่ที่เดือน {forecast['data']['last_month']}",
            "detail": (
                f"ช้ากว่าปัจจุบัน {lag} เดือน คำแนะนำทั้งหมดที่อิงการพยากรณ์จึงเป็นการต่อยอดจากข้อมูลเก่า "
                "ควรนำเข้าข้อมูลเดือนล่าสุดจาก Open Data เมืองพัทยาก่อนนำไปใช้ตัดสินใจจริง"
            ),
            "signal": "คุณภาพข้อมูล",
            "source": FORECAST_SOURCE,
        })

    analysis = impact()
    rain_effect = (analysis or {}).get("rain")
    if rain_effect and rain_effect.get("correlation") is not None:
        if rain_effect["significant"]:
            detail = (
                f"เมื่อฝนมากกว่าปกติของเดือนนั้น 100 มม. จำนวนคนต่างไปจากที่ควรเป็น {rain_effect['effect_percent']:+.1f}% "
                f"(r = {rain_effect['correlation']}, ช่วงความเชื่อมั่น {rain_effect['correlation_ci_95']}, "
                f"{rain_effect['months_used']} เดือน) ควรเตรียมกิจกรรมในร่มไว้รองรับ"
            )
            title = "เดือนที่ฝนมากกว่าปกติมีคนน้อยกว่าที่ควรเป็น"
        else:
            detail = (
                f"ทดสอบแล้วยังไม่พบความสัมพันธ์ชัดเจน (r = {rain_effect['correlation']}, "
                f"ช่วงความเชื่อมั่นคร่อมศูนย์, {rain_effect['months_used']} เดือน) จึงยังไม่ควรใช้ฝนวางแผนกำลังคน"
            )
            title = "ยังสรุปผลของฝนต่อจำนวนนักท่องเที่ยวไม่ได้"
        results.append({
            "title": title,
            "detail": detail,
            "signal": "ผลกระทบจากฝน",
            "assumption": "เป็นความสัมพันธ์หลังตัดฤดูกาลออกแล้ว ไม่ใช่การพิสูจน์เหตุและผล",
            "source": "Open-Meteo Archive (ERA5) + " + FORECAST_SOURCE,
        })

    busiest = query(
        """SELECT intersection_name, latitude, longitude, SUM(vehicle_volume) AS vehicle_volume
           FROM zone_traffic_volume
           WHERE calendar_year = (SELECT MAX(calendar_year) FROM zone_traffic_volume)
           GROUP BY intersection_name, latitude, longitude
           ORDER BY vehicle_volume DESC LIMIT 3"""
    )
    for row in busiest:
        results.append({
            "title": f"จัดการจราจรและจุดบริการที่{row['intersection_name']}",
            "detail": f"ปริมาณรถสะสมปีล่าสุด {row['vehicle_volume']:,} คัน สูงติดอันดับต้นของเมือง",
            "signal": "ปริมาณรถสูง",
            "source": "Open Data เมืองพัทยา ชุดข้อมูลปริมาณรถ (112)",
            "latitude": row["latitude"],
            "longitude": row["longitude"],
        })

    peak_month = query_one(
        """SELECT month_name, calendar_year, number_of_tourists FROM tourism_monthly
           WHERE calendar_year = (SELECT MAX(calendar_year) FROM tourism_monthly)
           ORDER BY number_of_tourists DESC LIMIT 1"""
    )
    if peak_month:
        results.append({
            "title": f"เตรียมกำลังรับช่วงพีคเดือน{peak_month['month_name']}",
            "detail": (
                f"เดือน{peak_month['month_name']} {peak_month['calendar_year']} "
                f"มีนักท่องเที่ยวลงเกาะล้าน {peak_month['number_of_tourists']:,} คน สูงสุดของปี"
            ),
            "signal": "ฤดูกาลท่องเที่ยว",
            "source": "Open Data เมืองพัทยา ชุดข้อมูลนักท่องเที่ยวเดินทางลงเกาะล้าน (172)",
        })

    weather = _weather_signal()
    if weather and weather["holidays_ahead"]:
        upcoming = weather["holidays_ahead"][0]
        rain = upcoming["precipitation_probability_percent"] or 0
        results.append({
            "title": f"วันหยุด{upcoming['holiday']} ({upcoming['date']})",
            "detail": (
                f"พยากรณ์ฝน {rain}% อุณหภูมิสูงสุด {upcoming['temperature_max_c']}°C "
                + ("ควรเตรียมพื้นที่ในร่มและกิจกรรมสำรอง" if rain >= 60 else "อากาศเอื้อต่อกิจกรรมกลางแจ้ง")
            ),
            "signal": "วันหยุดใกล้ถึง",
            "source": "World Holidays API + Open-Meteo",
        })
    if weather and weather["rainy_days"]:
        days = weather["rainy_days"]
        results.append({
            "title": f"มีวันฝนตกหนัก {len(days)} วันใน 7 วันข้างหน้า",
            "detail": "วันที่ " + ", ".join(day["date"] for day in days) + " ควรสื่อสารกิจกรรมในร่มล่วงหน้า",
            "signal": "สภาพอากาศ",
            "source": "Open-Meteo",
        })

    dense = query(
        """SELECT category, COUNT(*) AS total FROM poi_businesses
           GROUP BY category ORDER BY total DESC LIMIT 1"""
    )
    if dense:
        results.append({
            "title": f"ธุรกิจกลุ่ม {dense[0]['category']} หนาแน่นที่สุด ({dense[0]['total']:,} แห่ง)",
            "detail": "ใช้เป็นฐานกำหนดมาตรการดูแลนักท่องเที่ยวและการกระจายกิจกรรมกลางคืน",
            "signal": "โครงสร้างธุรกิจ",
            "source": "OpenStreetMap (Overpass API)",
        })
    return results


# ---------------------------------------------------------------- รายงาน Excel


@app.get("/api/pattaya/report")
def pattaya_report(source_file: str | None = Query(default=None)) -> dict:
    parameters: list[Any] = ["พัทยา ชลบุรี"]
    source_filter = "WHERE sheet_name = %s"
    if source_file:
        source_filter += " AND source_file = %s"
        parameters.append(source_file)
    metrics = query(
        f"""SELECT metric_label, metric_value, unit, year, quarter, source_row, source_file
            FROM pattaya_report_metrics {source_filter}
            ORDER BY source_row, id LIMIT 250""",
        parameters,
    )
    raw = query_one(
        "SELECT COUNT(*) AS total FROM raw_excel_cells WHERE sheet_name = %s"
        + (" AND source_file = %s" if source_file else ""),
        parameters,
    )
    return {"source_file": source_file, "raw_cells": (raw or {}).get("total", 0), "metrics": metrics}
