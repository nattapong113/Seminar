from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import connect, initialize_database

# ชุดข้อมูลปริมาณรถ (112) ของเมืองพัทยาไม่มีพิกัดของแยกมาให้ สคริปต์นี้หาพิกัดจาก OpenStreetMap
# สองวิธี เรียงตามความน่าเชื่อถือ และบันทึกวิธีที่ใช้ลงคอลัมน์ geocode_method เพื่อให้ตรวจสอบย้อนได้
#
#   1) osm_junction_name - โหนดแยก (highway=traffic_signals / junction) ที่มีแท็ก name ตรงกับชื่อแยก
#      เป็นพิกัดที่ OSM ระบุว่าเป็นแยกนั้นโดยตรง แม่นที่สุด แต่พัทยามีแยกที่ตั้งชื่อไว้เพียงราว 10 แยก
#   2) osm_road_pair - จุดที่ถนนสองสายตัดกันจริงใน OSM (โหนดที่ใช้ร่วมกันระหว่าง way สองเส้น)
#      ใช้กับแยกที่ตั้งชื่อตามถนนที่มาบรรจบ เช่น "แยกพัทยากลาง" คือจุดที่ถนนพัทยากลางตัดถนนสุขุมวิท
#      พิกัดที่ได้เป็นข้อเท็จจริงจาก OSM ส่วนการจับคู่ชื่อแยกกับคู่ถนนเป็นการตีความตามชื่อแยก
#
# แยกที่ไม่เข้าเงื่อนไขทั้งสองข้อจะไม่ใส่พิกัดมั่ว ปล่อยเป็น NULL และยังใช้ดูอันดับ/กราฟปริมาณรถได้
BBOX = (12.83, 100.80, 13.00, 100.98)
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
USER_AGENT = "PattayaSmartTourism/0.1 (academic prototype)"

SUKHUMVIT = "ถนนสุขุมวิท"
NAKLUA = "ถนนนาเกลือ"

# แยกที่ตั้งชื่อตามถนนสายรองที่มาบรรจบถนนสายหลัก -> (ถนน ก, ถนน ข)
# ชื่อถนนใช้ตามที่ปรากฏใน OSM โดยรองรับการสะกดหลายแบบผ่าน ROAD_ALIASES
ROAD_PAIRS: dict[str, tuple[str, str]] = {
    "แยกพัทยาเหนือ": ("ถนนพัทยาเหนือ", SUKHUMVIT),
    "แยกพัทยากลาง": ("ถนนพัทยากลาง", SUKHUMVIT),
    "แยกพัทยาใต้": ("ถนนพัทยาใต้", SUKHUMVIT),
    "แยกเทพประสิทธิ์": ("ถนนเทพประสิทธิ์", SUKHUMVIT),
    "แยกทัพพระยา": ("ถนนทัพพระยา", SUKHUMVIT),
    "แยกชัยพฤกษ์": ("ถนนชัยพฤกษ์", SUKHUMVIT),
    "แยกบุณย์กัญจนา": ("ถนนบุณย์กัญจนา", SUKHUMVIT),
    "แยกสว่างฟ้า": ("สว่างฟ้า", SUKHUMVIT),
    "แยกท็อปสายสองพัทยากลาง": ("ถนนพัทยาสายสอง", "ถนนพัทยากลาง"),
    "แยกโพธิสารนาเกลือ": ("ถนนโพธิสาร", NAKLUA),
    "แยกจอมเทียนสายสอง": ("จอมเทียนสายสอง", "ถนนทัพพระยา"),
}

# ชื่อถนนใน OSM สะกดไม่เหมือนกันทุก way (เว้นวรรค/ไม่เว้นวรรค/มีคำว่า "ถนน" บ้างไม่มีบ้าง)
ROAD_ALIASES: dict[str, tuple[str, ...]] = {
    NAKLUA: (NAKLUA, "ถนนพัทยา นาเกลือ", "ถนนพัทยา-นาเกลือ", "ถนนพัทยานาเกลือ"),
    "สว่างฟ้า": ("สว่างฟ้า", "ถนนสว่างฟ้า"),
    "จอมเทียนสายสอง": ("จอมเทียนสายสอง", "ถนนจอมเทียนสายสอง"),
}

NAKLUA_SOI_PATTERN = re.compile(r"^แ[ยผ]กนาเกลือซอย\s*(\d+)$")


def overpass(query: str) -> list[dict]:
    body = urlencode({"data": query}).encode("utf-8")
    last_error: Optional[Exception] = None
    for endpoint in OVERPASS_ENDPOINTS:
        for _attempt in range(2):
            try:
                request = Request(endpoint, data=body, headers={"User-Agent": USER_AGENT})
                with urlopen(request, timeout=180) as response:
                    payload = json.load(response)
                elements = payload.get("elements", [])
                if elements:
                    return elements
            except Exception as exc:  # endpoint สาธารณะล่มบ่อย ต้องลองตัวถัดไป
                last_error = exc
                time.sleep(3)
    raise URLError(f"ทุก Overpass endpoint ใช้งานไม่ได้: {last_error}")


def fetch_named_junctions() -> dict[str, tuple[float, float]]:
    elements = overpass(
        """[out:json][timeout:180];
        (
          node["highway"="traffic_signals"]["name"]({south},{west},{north},{east});
          node["junction"]["name"]({south},{west},{north},{east});
          node["highway"="motorway_junction"]["name"]({south},{west},{north},{east});
        );
        out body;""".format(south=BBOX[0], west=BBOX[1], north=BBOX[2], east=BBOX[3])
    )
    points_by_name: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for element in elements:
        name = element.get("tags", {}).get("name")
        if name and "lat" in element and "lon" in element:
            points_by_name[name].append((element["lat"], element["lon"]))
    return {
        name: (mean(lat for lat, _ in points), mean(lon for _, lon in points))
        for name, points in points_by_name.items()
    }


def fetch_road_network() -> tuple[dict[str, list[list[int]]], dict[int, tuple[float, float]]]:
    """คืนรายชื่อโหนดของถนนแต่ละสาย และพิกัดของทุกโหนด เพื่อหาโหนดที่ถนนสองสายใช้ร่วมกัน"""
    elements = overpass(
        """[out:json][timeout:180];
        way["highway"]["name"]({south},{west},{north},{east});
        (._;>;);
        out body;""".format(south=BBOX[0], west=BBOX[1], north=BBOX[2], east=BBOX[3])
    )
    node_coordinates: dict[int, tuple[float, float]] = {}
    ways_by_name: dict[str, list[list[int]]] = defaultdict(list)
    for element in elements:
        if element["type"] == "node":
            node_coordinates[element["id"]] = (element["lat"], element["lon"])
        elif element["type"] == "way":
            name = element.get("tags", {}).get("name")
            if name:
                ways_by_name[name].append(element.get("nodes", []))
    return ways_by_name, node_coordinates


def nodes_for_road(ways_by_name: dict[str, list[list[int]]], road_name: str) -> set[int]:
    nodes: set[int] = set()
    for alias in ROAD_ALIASES.get(road_name, (road_name,)):
        for way_nodes in ways_by_name.get(alias, []):
            nodes.update(way_nodes)
    return nodes


def road_pair_coordinates(
    ways_by_name: dict[str, list[list[int]]],
    node_coordinates: dict[int, tuple[float, float]],
    road_a: str,
    road_b: str,
) -> Optional[tuple[float, float]]:
    shared = nodes_for_road(ways_by_name, road_a) & nodes_for_road(ways_by_name, road_b)
    points = [node_coordinates[node_id] for node_id in shared if node_id in node_coordinates]
    if not points:
        return None
    # ถนนสองสายอาจใช้โหนดร่วมกันหลายจุด (เกาะกลาง/ทางคู่ขนาน) ใช้ค่ากลางเป็นตัวแทนของแยก
    return (mean(lat for lat, _ in points), mean(lon for _, lon in points))


def resolve_road_pair(intersection_name: str) -> Optional[tuple[str, str]]:
    if intersection_name in ROAD_PAIRS:
        return ROAD_PAIRS[intersection_name]
    soi = NAKLUA_SOI_PATTERN.match(intersection_name)
    if soi:
        # OSM สะกดซอยนาเกลือทั้งแบบ "ซอยนาเกลือ 22" และ "นาเกลือ 22"
        number = soi.group(1)
        soi_name = f"ซอยนาเกลือ {number}"
        ROAD_ALIASES.setdefault(soi_name, (soi_name, f"นาเกลือ {number}"))
        return (soi_name, NAKLUA)
    return None


def normalize(intersection_name: str) -> str:
    # ต้นฉบับสะกด "แยก" ผิดเป็น "แผก" อยู่บางรายการ (เช่น "แผกเพนียดช้าง")
    for prefix in ("แยก", "แผก"):
        if intersection_name.startswith(prefix):
            return intersection_name[len(prefix):].strip()
    return intersection_name.strip()


def save(
    connection: psycopg.Connection,
    name: str,
    latitude: float,
    longitude: float,
    method: str,
    detail: str,
) -> None:
    connection.execute(
        """UPDATE zone_traffic_volume
           SET latitude = %s, longitude = %s, geocode_method = %s, geocode_detail = %s
           WHERE intersection_name = %s""",
        (latitude, longitude, method, detail, name),
    )


def main() -> None:
    initialize_database()
    # อ่านรายชื่อแยกแล้วปิด connection ก่อนเรียก Overpass ซึ่งอาจใช้เวลาหลายนาที
    # จะได้ไม่มี transaction ค้างอยู่บน Supabase ระหว่างรอ
    with connect() as connection:
        names = [
            row["intersection_name"]
            for row in connection.execute(
                "SELECT DISTINCT intersection_name FROM zone_traffic_volume ORDER BY intersection_name"
            )
        ]
    print(f"แยกทั้งหมดในชุดข้อมูล {len(names)} แยก")

    named_junctions = fetch_named_junctions()
    print(f"โหนดแยกที่มีชื่อใน OpenStreetMap {len(named_junctions)} ชื่อ")
    ways_by_name, node_coordinates = fetch_road_network()
    print(f"ถนนที่มีชื่อใน OpenStreetMap {len(ways_by_name)} สาย")

    by_method: dict[str, list[str]] = defaultdict(list)
    unmatched: list[str] = []
    with connect() as connection:
        for name in names:
            coordinates = named_junctions.get(normalize(name))
            if coordinates:
                save(connection, name, coordinates[0], coordinates[1], "osm_junction_name", normalize(name))
                by_method["osm_junction_name"].append(name)
                continue
            pair = resolve_road_pair(name)
            if pair:
                coordinates = road_pair_coordinates(ways_by_name, node_coordinates, *pair)
                if coordinates:
                    detail = f"{pair[0]} x {pair[1]}"
                    save(connection, name, coordinates[0], coordinates[1], "osm_road_pair", detail)
                    by_method["osm_road_pair"].append(f"{name} ({detail})")
                    continue
            unmatched.append(name)

    for method, matched in by_method.items():
        print(f"\n{method}: {len(matched)} แยก")
        for item in matched:
            print(f"  - {item}")
    print(f"\nสรุป: มีพิกัด {len(names) - len(unmatched)}/{len(names)} แยก")
    if unmatched:
        print("ยังไม่มีพิกัด (ไม่มีชื่อแยกใน OSM และระบุคู่ถนนไม่ได้):")
        for name in unmatched:
            print(f"  - {name}")


if __name__ == "__main__":
    main()
