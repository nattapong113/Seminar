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
            holiday INTEGER NOT NULL DEFAULT 0,
            data_source TEXT NOT NULL DEFAULT 'seed',
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        CREATE TABLE IF NOT EXISTS zone_traffic_volume (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            intersection_id TEXT NOT NULL,
            intersection_name TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            calendar_year_be INTEGER NOT NULL,
            calendar_year INTEGER NOT NULL,
            month_id INTEGER NOT NULL,
            month_name TEXT,
            vehicle_volume INTEGER NOT NULL,
            unit TEXT,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_file, intersection_id, calendar_year_be, month_id)
        );
        CREATE INDEX IF NOT EXISTS idx_zone_traffic_date
            ON zone_traffic_volume (calendar_year, month_id);
        CREATE INDEX IF NOT EXISTS idx_zone_traffic_intersection
            ON zone_traffic_volume (intersection_name);
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
        -- Data provenance and import tracking
        CREATE TABLE IF NOT EXISTS data_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_type TEXT NOT NULL, -- e.g. open_api, csv, weather, holidays
            endpoint TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_source_id INTEGER,
            import_type TEXT NOT NULL, -- csv | api
            source_name TEXT,
            raw_filename TEXT,
            source_url TEXT,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            records_total INTEGER,
            records_success INTEGER,
            records_failed INTEGER,
            notes TEXT,
            FOREIGN KEY(data_source_id) REFERENCES data_sources(id)
        );
        CREATE TABLE IF NOT EXISTS import_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            row_number INTEGER,
            error_text TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(import_id) REFERENCES imports(id)
        );
        -- POI / Businesses
        CREATE TABLE IF NOT EXISTS poi_businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_source_id INTEGER,
            external_id TEXT,
            name TEXT NOT NULL,
            category TEXT,
            address TEXT,
            latitude REAL,
            longitude REAL,
            phone TEXT,
            website TEXT,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(data_source_id, external_id),
            FOREIGN KEY(data_source_id) REFERENCES data_sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_poi_location ON poi_businesses (latitude, longitude);
        -- User and RBAC
        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            full_name TEXT,
            email TEXT UNIQUE,
            password_hash TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_roles (
            user_id INTEGER NOT NULL,
            role_id INTEGER NOT NULL,
            PRIMARY KEY (user_id, role_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(role_id) REFERENCES roles(id)
        );
        CREATE TABLE IF NOT EXISTS role_permissions (
            role_id INTEGER NOT NULL,
            permission TEXT NOT NULL,
            PRIMARY KEY (role_id, permission),
            FOREIGN KEY(role_id) REFERENCES roles(id)
        );
        -- Public holidays and festivals
        CREATE TABLE IF NOT EXISTS public_holidays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_source_id INTEGER,
            date TEXT NOT NULL,
            name TEXT NOT NULL,
            local_name TEXT,
            holiday_type TEXT,
            country_code TEXT NOT NULL DEFAULT 'TH',
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date, name, country_code),
            FOREIGN KEY(data_source_id) REFERENCES data_sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_public_holidays_date ON public_holidays (date);
        -- Audit logs for imports, user actions
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            actor TEXT,
            target TEXT,
            details TEXT,
            timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
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
