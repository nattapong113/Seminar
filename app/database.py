from pathlib import Path
import sqlite3
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "pattaya_tourism.db"


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    connection = get_connection()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS tourism_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_on TEXT NOT NULL,
            zone TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            visitors INTEGER NOT NULL,
            spending_thb REAL NOT NULL,
            avg_stay_hours REAL NOT NULL,
            weather TEXT NOT NULL,
            holiday INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_observations_date
            ON tourism_observations (observed_on);
        CREATE INDEX IF NOT EXISTS idx_observations_zone
            ON tourism_observations (zone);
        CREATE TABLE IF NOT EXISTS raw_excel_cells (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            sheet_name TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            column_number INTEGER NOT NULL,
            cell_value TEXT,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_raw_excel_sheet
            ON raw_excel_cells (source_file, sheet_name);
        CREATE TABLE IF NOT EXISTS pattaya_report_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            sheet_name TEXT NOT NULL,
            metric_label TEXT NOT NULL,
            metric_value REAL,
            unit TEXT,
            year INTEGER,
            quarter INTEGER,
            source_row INTEGER,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_report_metric_label
            ON pattaya_report_metrics (metric_label);
        CREATE TABLE IF NOT EXISTS tourism_monthly (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            location TEXT NOT NULL,
            calendar_year_be INTEGER NOT NULL,
            calendar_year INTEGER NOT NULL,
            month_id INTEGER NOT NULL,
            month_name TEXT,
            number_of_tourists INTEGER NOT NULL,
            unit TEXT,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_file, location, calendar_year_be, month_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tourism_monthly_date
            ON tourism_monthly (calendar_year, month_id);
        CREATE TABLE IF NOT EXISTS demographic_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            location TEXT NOT NULL,
            calendar_year_be INTEGER NOT NULL,
            calendar_year INTEGER NOT NULL,
            category TEXT NOT NULL,
            segment TEXT NOT NULL,
            population INTEGER NOT NULL,
            unit TEXT,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_file, location, calendar_year_be, category, segment)
        );
        CREATE INDEX IF NOT EXISTS idx_demographic_year
            ON demographic_profiles (calendar_year, category);
        """
    )
    count = connection.execute("SELECT COUNT(*) FROM tourism_observations").fetchone()[0]
    if count == 0:
        seed_database(connection)
    connection.commit()
    connection.close()


def seed_database(connection: sqlite3.Connection) -> None:
    zones = [
        ("Walking Street", 12.9236, 100.8694, 1.28, 1.18),
        ("Jomtien Beach", 12.9006, 100.8750, 1.05, 0.96),
        ("Central Pattaya", 12.9348, 100.8830, 1.18, 1.08),
        ("Na Kluea", 12.9672, 100.8875, 0.84, 0.91),
        ("Pratumnak Hill", 12.9138, 100.8590, 0.76, 0.87),
        ("Pattaya Floating Market", 12.8706, 100.9026, 0.91, 0.99),
    ]
    rows: list[tuple[Any, ...]] = []
    for day in range(1, 31):
        observed_on = f"2026-08-{day:02d}"
        holiday = 1 if day in {1, 2, 9, 10, 12, 15, 16, 23, 24, 30, 31} else 0
        weather = "ฝนตก" if day in {4, 5, 11, 18, 19, 25, 26} else "แจ่มใส"
        weekend_factor = 1.32 if day % 7 in {1, 2} else 1.0
        for zone, latitude, longitude, visitor_factor, spending_factor in zones:
            visitors = round(4100 * visitor_factor * weekend_factor * (1.12 if holiday else 1))
            spending = round(visitors * 820 * spending_factor * (0.94 if weather == "ฝนตก" else 1))
            avg_stay = round(3.3 * spending_factor * (1.08 if holiday else 1), 1)
            rows.append((observed_on, zone, latitude, longitude, visitors, spending, avg_stay, weather, holiday))
    connection.executemany(
        """
        INSERT INTO tourism_observations
            (observed_on, zone, latitude, longitude, visitors, spending_thb,
             avg_stay_hours, weather, holiday)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
