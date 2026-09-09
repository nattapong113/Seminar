from pathlib import Path
import sqlite3
from urllib.parse import urlencode
from urllib.request import urlopen
import json
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import get_connection, initialize_database

BASE_DIR = Path(__file__).resolve().parent.parent
app = FastAPI(title="Pattaya Smart Tourism API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

initialize_database()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def rows_to_dict(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


@app.get("/api/weather/live")
def live_weather() -> dict:
    query = urlencode({
        "latitude": 12.9236,
        "longitude": 100.8694,
        "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "timezone": "Asia/Bangkok",
    })
    with urlopen(f"https://api.open-meteo.com/v1/forecast?{query}", timeout=8) as response:
        payload = json.load(response)
    current = payload["current"]
    return {
        "location": "พัทยา",
        "observed_at": current["time"],
        "temperature_c": current["temperature_2m"],
        "humidity_percent": current["relative_humidity_2m"],
        "precipitation_mm": current["precipitation"],
        "wind_speed_kmh": current["wind_speed_10m"],
        "weather_code": current["weather_code"],
    }


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/summary")
def summary() -> dict:
    connection = get_connection()
    row = connection.execute(
        """
        SELECT SUM(visitors) AS visitors, SUM(spending_thb) AS spending,
               AVG(avg_stay_hours) AS avg_stay, COUNT(DISTINCT zone) AS zones
        FROM tourism_observations
        WHERE observed_on >= date((SELECT MAX(observed_on) FROM tourism_observations), '-6 days')
        """
    ).fetchone()
    connection.close()
    return {
        "visitors": row["visitors"],
        "spending": round(row["spending"]),
        "avg_stay": round(row["avg_stay"], 1),
        "zones": row["zones"],
        "period": "7 วันล่าสุด",
    }


@app.get("/api/zones")
def zones(weather: str | None = Query(default=None)) -> list[dict]:
    connection = get_connection()
    weather_filter = "WHERE weather = ?" if weather else ""
    parameters = [weather] if weather else []
    rows = connection.execute(
        f"""
        SELECT zone, ROUND(AVG(latitude), 5) AS latitude, ROUND(AVG(longitude), 5) AS longitude,
               SUM(visitors) AS visitors, SUM(spending_thb) AS spending,
               ROUND(AVG(avg_stay_hours), 1) AS avg_stay
        FROM tourism_observations {weather_filter}
        GROUP BY zone ORDER BY visitors DESC
        """,
        parameters,
    ).fetchall()
    connection.close()
    return rows_to_dict(rows)


@app.get("/api/trends")
def trends() -> list[dict]:
    connection = get_connection()
    monthly_count = connection.execute("SELECT COUNT(*) FROM tourism_monthly").fetchone()[0]
    if monthly_count:
        rows = connection.execute(
            """
            SELECT printf('%04d-%02d-01', calendar_year, month_id) AS date,
                   number_of_tourists AS visitors, 0 AS spending
            FROM tourism_monthly
            ORDER BY calendar_year, month_id
            """
        ).fetchall()
        connection.close()
        return rows_to_dict(rows)
    rows = connection.execute(
        """
        SELECT observed_on AS date, SUM(visitors) AS visitors, SUM(spending_thb) AS spending
        FROM tourism_observations GROUP BY observed_on ORDER BY observed_on
        """
    ).fetchall()
    connection.close()
    return rows_to_dict(rows)


@app.get("/api/tourism/monthly")
def monthly_tourism(year: int | None = Query(default=None)) -> list[dict]:
    connection = get_connection()
    query = """
        SELECT calendar_year_be, calendar_year, month_id, month_name,
               number_of_tourists, unit, location, source_file
        FROM tourism_monthly
    """
    parameters: list[int] = []
    if year:
        query += " WHERE calendar_year = ?"
        parameters.append(year)
    query += " ORDER BY calendar_year, month_id"
    rows = connection.execute(query, parameters).fetchall()
    connection.close()
    return rows_to_dict(rows)


@app.get("/api/demographics")
def demographics(
    year: int | None = Query(default=None),
    category: str | None = Query(default=None),
) -> list[dict]:
    connection = get_connection()
    clauses: list[str] = []
    parameters: list[object] = []
    if year:
        clauses.append("calendar_year = ?")
        parameters.append(year)
    if category:
        clauses.append("category = ?")
        parameters.append(category)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        "SELECT calendar_year_be, calendar_year, category, segment, population, unit, location "
        f"FROM demographic_profiles{where} ORDER BY calendar_year, category, population DESC",
        parameters,
    ).fetchall()
    connection.close()
    return rows_to_dict(rows)


@app.get("/api/recommendations")
def recommendations() -> list[dict]:
    connection = get_connection()
    rows = connection.execute(
        """
        SELECT zone, SUM(visitors) AS visitors, SUM(spending_thb) AS spending,
               AVG(avg_stay_hours) AS avg_stay
        FROM tourism_observations
        WHERE observed_on >= date((SELECT MAX(observed_on) FROM tourism_observations), '-6 days')
        GROUP BY zone ORDER BY visitors DESC LIMIT 3
        """
    ).fetchall()
    connection.close()
    recommendations = []
    for index, row in enumerate(rows):
        action = [
            "เพิ่มกำลังคนและจุดบริการช่วง 18:00-22:00",
            "จัดโปรโมชันเชื่อมร้านค้าและกิจกรรมริมหาด",
            "กระจายการสื่อสารไปยังนักท่องเที่ยวกลุ่มพักค้างคืน",
        ][index]
        recommendations.append({"zone": row["zone"], "action": action, "signal": "โอกาสสูง", "visitors": row["visitors"]})
    return recommendations


@app.get("/api/traffic/zones")
def traffic_zones(year: int | None = Query(default=None), month: int | None = Query(default=None)) -> list[dict]:
    connection = get_connection()
    clauses: list[str] = []
    parameters: list[int] = []
    if year:
        clauses.append("calendar_year = ?")
        parameters.append(year)
    if month:
        clauses.append("month_id = ?")
        parameters.append(month)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"""
        SELECT intersection_name, ROUND(AVG(latitude), 5) AS latitude, ROUND(AVG(longitude), 5) AS longitude,
               SUM(vehicle_volume) AS vehicle_volume
        FROM zone_traffic_volume{where}
        GROUP BY intersection_name ORDER BY vehicle_volume DESC
        """,
        parameters,
    ).fetchall()
    connection.close()
    return rows_to_dict(rows)


@app.get("/api/traffic/trends")
def traffic_trends(intersection_name: str | None = Query(default=None)) -> list[dict]:
    connection = get_connection()
    clause = "WHERE intersection_name = ?" if intersection_name else ""
    parameters = [intersection_name] if intersection_name else []
    rows = connection.execute(
        f"""
        SELECT printf('%04d-%02d-01', calendar_year, month_id) AS date,
               SUM(vehicle_volume) AS vehicle_volume
        FROM zone_traffic_volume {clause}
        GROUP BY calendar_year, month_id ORDER BY calendar_year, month_id
        """,
        parameters,
    ).fetchall()
    connection.close()
    return rows_to_dict(rows)


@app.get("/api/holidays")
def holidays(year: int | None = Query(default=None)) -> list[dict]:
    connection = get_connection()
    query = "SELECT date, name, local_name, holiday_type, country_code FROM public_holidays"
    parameters: list[int] = []
    if year:
        query += " WHERE date LIKE ?"
        parameters.append(f"{year}-%")
    query += " ORDER BY date"
    rows = connection.execute(query, parameters).fetchall()
    connection.close()
    return rows_to_dict(rows)


@app.get("/api/poi")
def poi(category: str | None = Query(default=None), limit: int = Query(default=200, le=1000)) -> list[dict]:
    connection = get_connection()
    query = "SELECT name, category, address, latitude, longitude, phone, website FROM poi_businesses"
    parameters: list[object] = []
    if category:
        query += " WHERE category = ?"
        parameters.append(category)
    query += " ORDER BY name LIMIT ?"
    parameters.append(limit)
    rows = connection.execute(query, parameters).fetchall()
    connection.close()
    return rows_to_dict(rows)


@app.get("/api/pattaya/report")
def pattaya_report(source_file: str | None = Query(default=None)) -> dict:
    connection = get_connection()
    source_filter = "WHERE sheet_name = ?"
    parameters: list[str] = ["พัทยา ชลบุรี"]
    if source_file:
        source_filter += " AND source_file = ?"
        parameters.append(source_file)
    rows = connection.execute(
        f"""
        SELECT metric_label, metric_value, unit, year, quarter, source_row, source_file
        FROM pattaya_report_metrics {source_filter}
        ORDER BY source_row, id LIMIT 250
        """,
        parameters,
    ).fetchall()
    raw_count = connection.execute(
        "SELECT COUNT(*) FROM raw_excel_cells WHERE sheet_name = ?" +
        (" AND source_file = ?" if source_file else ""),
        parameters,
    ).fetchone()[0]
    connection.close()
    return {"source_file": source_file, "raw_cells": raw_count, "metrics": rows_to_dict(rows)}
