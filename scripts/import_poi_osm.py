from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import connect, initialize_database

SOURCE_NAME = "OpenStreetMap Overpass API"
# Pattaya bounding box: south, west, north, east
BBOX = (12.85, 100.83, 12.98, 100.95)
OVERPASS_ENDPOINTS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
QUERY = """
[out:json][timeout:60];
(
  node["tourism"]({south},{west},{north},{east});
  node["amenity"~"^(restaurant|cafe|bar|nightclub|marketplace)$"]({south},{west},{north},{east});
  node["shop"="mall"]({south},{west},{north},{east});
);
out body;
""".format(south=BBOX[0], west=BBOX[1], north=BBOX[2], east=BBOX[3])


def fetch_poi() -> dict:
    body = urlencode({"data": QUERY}).encode("utf-8")
    last_error: Optional[Exception] = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            request = Request(endpoint, data=body, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=60) as response:
                payload = json.load(response)
            if payload.get("elements"):
                return payload
        except Exception as exc:  # try next mirror
            last_error = exc
            continue
    raise URLError(f"ทุก Overpass endpoint ใช้งานไม่ได้: {last_error}")


def build_address(tags: dict) -> Optional[str]:
    parts = [
        tags.get("addr:housenumber"),
        tags.get("addr:street"),
        tags.get("addr:subdistrict"),
        tags.get("addr:city"),
    ]
    parts = [part for part in parts if part]
    return " ".join(parts) if parts else None


def ensure_data_source(connection: psycopg.Connection, name: str, source_type: str, endpoint: Optional[str] = None) -> int:
    row = connection.execute("SELECT id FROM data_sources WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    return connection.execute(
        "INSERT INTO data_sources (name, source_type, endpoint) VALUES (%s, %s, %s) RETURNING id",
        (name, source_type, endpoint),
    ).fetchone()["id"]


def import_poi(connection: psycopg.Connection, payload: dict) -> tuple[int, int]:
    data_source_id = ensure_data_source(connection, SOURCE_NAME, "open_api", OVERPASS_ENDPOINTS[0])
    elements = [el for el in payload.get("elements", []) if el.get("tags", {}).get("name")]

    import_id = connection.execute(
        """
        INSERT INTO imports (data_source_id, import_type, source_name, source_url, records_total, records_success, records_failed)
        VALUES (%s, 'api', %s, %s, %s, 0, 0)
        RETURNING id
        """,
        (data_source_id, SOURCE_NAME, OVERPASS_ENDPOINTS[0], len(elements)),
    ).fetchone()["id"]

    # เตรียมแถวใน Python ก่อนแล้วเขียนรวดเดียว ฐานข้อมูลอยู่บนคลาวด์ ถ้าเขียนทีละแถวจะช้ามาก
    values = []
    errors = []
    for element in elements:
        tags = element.get("tags", {})
        category = tags.get("tourism") or tags.get("amenity") or tags.get("shop") or "other"
        try:
            values.append((
                data_source_id,
                str(element["id"]),
                tags.get("name"),
                category,
                build_address(tags),
                element["lat"],
                element["lon"],
                tags.get("phone") or tags.get("contact:phone"),
                tags.get("website") or tags.get("contact:website"),
            ))
        except KeyError as exc:  # node ที่ไม่มี id หรือพิกัด ใช้บนแผนที่ไม่ได้
            errors.append(f"node {element.get('id')}: ไม่มีฟิลด์ {exc}")

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO poi_businesses
                (data_source_id, external_id, name, category, address, latitude, longitude, phone, website)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(data_source_id, external_id) DO UPDATE SET
                name = excluded.name,
                category = excluded.category,
                address = excluded.address,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                phone = excluded.phone,
                website = excluded.website
            """,
            values,
        )
        cursor.executemany(
            "INSERT INTO import_errors (import_id, error_text) VALUES (%s, %s)",
            [(import_id, error) for error in errors],
        )

    connection.execute(
        "UPDATE imports SET records_success = %s, records_failed = %s WHERE id = %s",
        (len(values), len(errors), import_id),
    )
    return len(values), len(errors)


def main() -> None:
    parser = argparse.ArgumentParser(description="นำเข้าสถานที่ท่องเที่ยว/ธุรกิจในพัทยาจาก OpenStreetMap (Overpass API)")
    parser.parse_args()

    try:
        payload = fetch_poi()
    except URLError as exc:
        raise SystemExit(f"ดึงข้อมูลสถานที่ไม่สำเร็จ: {exc}")

    initialize_database()
    with connect() as connection:
        success, failed = import_poi(connection, payload)
    print(f"นำเข้าสถานที่ท่องเที่ยว/ธุรกิจสำเร็จ {success} รายการ (ผิดพลาด {failed})")


if __name__ == "__main__":
    main()
