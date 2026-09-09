from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import get_connection, initialize_database

# ชื่อแยกในชุดข้อมูล 112 ไม่มีอยู่ใน Nominatim (ค้นด้วยข้อความอิสระได้ผลลัพธ์ผิดเป้าบ่อย เช่น
# จับเอาโรงแรม/สถานีรถไฟที่ชื่อพ้องกันแทนตัวแยกจริง) จึงใช้โหนดสัญญาณไฟจราจร (highway=traffic_signals)
# ที่มีแท็ก name ตรงกับชื่อแยก (ตัดคำว่า "แยก" ออก) จาก OpenStreetMap ผ่าน Overpass API แทน
# ซึ่งให้พิกัดที่ตรวจสอบแหล่งที่มาได้ แม้จะครอบคลุมไม่ครบทุกแยกก็ตาม
BBOX = (12.85, 100.83, 12.98, 100.95)
OVERPASS_ENDPOINTS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
QUERY = """
[out:json][timeout:60];
(
  node["highway"="traffic_signals"]["name"]({south},{west},{north},{east});
  node["junction"]["name"]({south},{west},{north},{east});
);
out body;
""".format(south=BBOX[0], west=BBOX[1], north=BBOX[2], east=BBOX[3])


def fetch_named_junctions() -> dict[str, tuple[float, float]]:
    body = urlencode({"data": QUERY}).encode("utf-8")
    last_error: Optional[Exception] = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            request = Request(endpoint, data=body, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=60) as response:
                payload = json.load(response)
            elements = payload.get("elements", [])
            if elements:
                break
        except Exception as exc:
            last_error = exc
            continue
    else:
        raise URLError(f"ทุก Overpass endpoint ใช้งานไม่ได้: {last_error}")

    coordinates_by_name: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for element in elements:
        name = element.get("tags", {}).get("name")
        if name and "lat" in element and "lon" in element:
            coordinates_by_name[name].append((element["lat"], element["lon"]))
    return {
        name: (mean(lat for lat, _ in points), mean(lon for _, lon in points))
        for name, points in coordinates_by_name.items()
    }


def normalize(intersection_name: str) -> str:
    # ต้นฉบับสะกด "แยก" ผิดเป็น "แผก" อยู่บางรายการ (เช่น "แผกเพนียดช้าง")
    for prefix in ("แยก", "แผก"):
        if intersection_name.startswith(prefix):
            return intersection_name[len(prefix):].strip()
    return intersection_name.strip()


def main() -> None:
    initialize_database()
    connection = get_connection()
    try:
        named_junctions = fetch_named_junctions()
        rows = connection.execute(
            "SELECT DISTINCT intersection_name FROM zone_traffic_volume WHERE latitude IS NULL"
        ).fetchall()
        matched = 0
        unmatched: list[str] = []
        for (intersection_name,) in rows:
            coordinates = named_junctions.get(normalize(intersection_name))
            if coordinates:
                latitude, longitude = coordinates
                connection.execute(
                    "UPDATE zone_traffic_volume SET latitude = ?, longitude = ? WHERE intersection_name = ?",
                    (latitude, longitude, intersection_name),
                )
                matched += 1
                print(f"พบพิกัด: {intersection_name} -> {latitude:.5f},{longitude:.5f}")
            else:
                unmatched.append(intersection_name)
        connection.commit()
        print(f"\nสรุป: พบพิกัดจาก OpenStreetMap {matched} แยก จากทั้งหมด {len(rows)} แยก")
        if unmatched:
            print("แยกที่ยังไม่มีพิกัด (ไม่มีชื่อตรงกันใน OpenStreetMap):")
            for name in unmatched:
                print(f"  - {name}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
