from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import connect, initialize_database

LOCATION = "พัทยา ชลบุรี"
ENCODINGS = ("utf-8-sig", "cp874", "tis-620")
# ไฟล์ที่ผิดทั้งไฟล์อาจมีแถวผิดเป็นหมื่น เก็บลง import_errors แค่ช่วงต้นพอให้รู้ว่าผิดแบบไหน
MAX_ERRORS_RECORDED = 50


class RowErrors(ValueError):
    """แถวที่แปลงค่าไม่ได้ของไฟล์หนึ่ง เก็บเป็น (เลขบรรทัดในไฟล์, ข้อความ)"""

    def __init__(self, errors: list[tuple[Optional[int], str]]) -> None:
        super().__init__(f"มี {len(errors)} แถวที่ข้อมูลไม่ถูกต้อง")
        self.errors = errors


def decode_csv(data: bytes, name: str) -> list[dict[str, str]]:
    last_error: UnicodeDecodeError | None = None
    for encoding in ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as error:
            last_error = error
            continue
        return list(csv.DictReader(io.StringIO(text, newline="")))
    raise ValueError(f"อ่านไฟล์ {name} ไม่ได้: {last_error}")


def read_csv(path: Path) -> list[dict[str, str]]:
    return decode_csv(path.read_bytes(), path.name)


def integer(value: str | None) -> int:
    try:
        return int(float((value or "0").replace(",", "").strip()))
    except ValueError:
        raise ValueError(f"ต้องเป็นตัวเลข แต่ได้ {value!r}") from None


def buddhist_year(value: str | None) -> int:
    # ไฟล์ของเมืองพัทยาใช้ปี พ.ศ. ถ้าหลุดเป็น ค.ศ. มาจะถูกลบ 543 ซ้ำจนกลายเป็นปี 14xx แล้วหายไปจากกราฟเงียบ ๆ
    year = integer(value)
    if not 2400 <= year <= 2700:
        raise ValueError(f"ปีต้องเป็น พ.ศ. แต่ได้ {value!r}")
    # ไฟล์รายปีบางไฟล์ของต้นทางมีปีไล่ต่อไปถึงอนาคต (เช่น 2569-2571 ในไฟล์ของปี 2567) จากการลากสูตรในตาราง
    if year > date.today().year + 543:
        raise ValueError(f"ปี {year} ยังมาไม่ถึง")
    return year


def month_number(value: str | None) -> int:
    month = integer(value)
    if not 1 <= month <= 12:
        raise ValueError(f"เดือนต้องเป็น 1-12 แต่ได้ {value!r}")
    return month


def decimal(value: str | None) -> float:
    try:
        return float((value or "").replace(",", "").strip())
    except ValueError:
        raise ValueError(f"ต้องเป็นตัวเลข แต่ได้ {value!r}") from None


def clean(value: str | None) -> str:
    return (value or "").strip()


MONTH_IDS = {name: number for number, name in enumerate((
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
), start=1)}


def month_of(row: dict[str, str]) -> int:
    """บางไฟล์ของเมืองพัทยามีแต่ชื่อเดือน ไม่มี month_id"""
    if clean(row.get("month_id")):
        return month_number(row.get("month_id"))
    name = clean(row.get("month_name"))
    if name not in MONTH_IDS:
        raise ValueError(f"ไม่รู้จักชื่อเดือน {name!r}")
    return MONTH_IDS[name]


# ---------------------------------------------------------------- แปลงแถวของแต่ละรูปแบบไฟล์
# แต่ละฟังก์ชันรับ (ชื่อไฟล์, แถว) คืนรายการแถวที่จะเขียนลงตาราง แถวเดียวของไฟล์อาจได้หลายแถวหรือไม่ได้เลย


def monthly_rows(source_file: str, row: dict[str, str]) -> list[tuple]:
    year_be = buddhist_year(row.get("calendar_year"))
    return [(
        source_file,
        LOCATION,
        year_be,
        year_be - 543,
        month_number(row.get("month_id")),
        clean(row.get("month_name")) or None,
        integer(row.get("number_of_tourists")),
        clean(row.get("unit")) or "คน",
    )]


DEMOGRAPHIC_DIMENSIONS = (
    ("gender", "เพศ"),
    ("age", "อายุ"),
    ("income", "รายได้"),
    ("education", "การศึกษา"),
    ("occupation", "อาชีพ"),
)


def demographic_rows(source_file: str, row: dict[str, str]) -> list[tuple]:
    year_be = buddhist_year(row.get("year"))
    population = integer(row.get("population"))
    for column, category in DEMOGRAPHIC_DIMENSIONS:
        segment = clean(row.get(column))
        if segment:
            return [(source_file, LOCATION, year_be, year_be - 543, category, segment, population, clean(row.get("unit")) or "คน")]
    return []


def demographic_wide_rows(source_file: str, row: dict[str, str]) -> list[tuple]:
    """รองรับไฟล์รูปแบบจริงของเทศบาลเมืองพัทยา: category_type,category_label,pop_2563,pop_2564,..."""
    category = clean(row.get("category_type"))
    segment = clean(row.get("category_label"))
    if not category or not segment:
        return []
    values = []
    for column, raw_value in row.items():
        if not column or not column.startswith("pop_") or not clean(raw_value):
            continue
        year_be = buddhist_year(column.removeprefix("pop_"))
        values.append((source_file, LOCATION, year_be, year_be - 543, category, segment, integer(raw_value), "คน"))
    return values


def zone_traffic_rows(source_file: str, row: dict[str, str]) -> list[tuple]:
    year_be = buddhist_year(row.get("calendar_year"))
    return [(
        source_file,
        clean(row.get("intersection_id")),
        clean(row.get("intersection_name")),
        year_be,
        year_be - 543,
        month_number(row.get("month_id")),
        clean(row.get("month_name")) or None,
        integer(row.get("vehicle_volume")),
        clean(row.get("unit")) or "คัน",
    )]


def boat_rows(source_file: str, row: dict[str, str]) -> list[tuple]:
    year_be = buddhist_year(row.get("calendar_year"))
    return [(
        source_file,
        year_be,
        year_be - 543,
        month_of(row),
        clean(row.get("month_name")) or None,
        clean(row.get("boat_type_id")),
        clean(row.get("boat_type_name")),
        clean(row.get("route_id")),
        clean(row.get("route_name")),
        integer(row.get("number_of_trips")),
        integer(row.get("number_of_passengers")),
    )]


def air_quality_rows(source_file: str, row: dict[str, str]) -> list[tuple]:
    year_be = buddhist_year(row.get("calendar_year"))
    return [(
        source_file,
        year_be,
        year_be - 543,
        month_of(row),
        clean(row.get("month_name")) or None,
        decimal(row.get("pm25_avg")),
        decimal(row.get("pm25_min")) if clean(row.get("pm25_min")) else None,
        decimal(row.get("pm25_max")) if clean(row.get("pm25_max")) else None,
        integer(row.get("exceed_days")) if clean(row.get("exceed_days")) else None,
    )]


# ชุดขยะทั้งเมือง (150) ใช้หัวคอลัมน์เดียวกับชุดขยะเกาะล้าน (hom8) แยกได้จากประเภทขยะที่มีเฉพาะในชุดทั้งเมือง
# ถ้าปล่อยเข้ามา ยอดขยะของเกาะจะกลายเป็นหลักหมื่นตันต่อเดือน
CITY_ONLY_WASTE_TYPES = {"ขยะติดเชื้อ", "ขยะอันตราย"}


def island_waste_rows(source_file: str, row: dict[str, str]) -> list[tuple]:
    year_be = buddhist_year(row.get("calendar_year"))
    waste_type = clean(row.get("waste_type"))
    if waste_type in CITY_ONLY_WASTE_TYPES:
        raise ValueError(f"พบประเภท {waste_type!r} ซึ่งเป็นของชุดขยะทั้งเมือง ไม่ใช่ชุดขยะเกาะล้าน")
    return [(
        source_file,
        year_be,
        year_be - 543,
        month_of(row),
        clean(row.get("month_name")) or None,
        waste_type,
        decimal(row.get("waste_amount_ton")),
    )]


@dataclass(frozen=True)
class Dataset:
    label: str
    table: str
    columns: tuple[str, ...]
    # คอลัมน์ที่บอกว่าสองแถวเป็นข้อมูลของช่วงเดียวกัน (ไม่รวม source_file)
    period_keys: tuple[str, ...]
    convert: Callable[[str, dict[str, str]], list[tuple]]


DEMOGRAPHIC_COLUMNS = ("source_file", "location", "calendar_year_be", "calendar_year", "category", "segment", "population", "unit")
DEMOGRAPHIC_KEYS = ("location", "calendar_year_be", "category", "segment")

MONTHLY = Dataset(
    "นักท่องเที่ยวรายเดือน", "tourism_monthly",
    ("source_file", "location", "calendar_year_be", "calendar_year", "month_id", "month_name", "number_of_tourists", "unit"),
    ("location", "calendar_year_be", "month_id"), monthly_rows,
)
DEMOGRAPHICS = Dataset("ประชากรจำแนกกลุ่ม", "demographic_profiles", DEMOGRAPHIC_COLUMNS, DEMOGRAPHIC_KEYS, demographic_rows)
DEMOGRAPHICS_WIDE = Dataset(
    "ประชากรจำแนกกลุ่ม (รูปแบบตาราง)", "demographic_profiles", DEMOGRAPHIC_COLUMNS, DEMOGRAPHIC_KEYS, demographic_wide_rows,
)
ZONE_TRAFFIC = Dataset(
    "ปริมาณรถรายแยก", "zone_traffic_volume",
    ("source_file", "intersection_id", "intersection_name", "calendar_year_be", "calendar_year", "month_id", "month_name", "vehicle_volume", "unit"),
    ("intersection_id", "calendar_year_be", "month_id"), zone_traffic_rows,
)


BOAT_PASSENGERS = Dataset(
    "ผู้โดยสารเรือเกาะล้าน", "boat_passengers_monthly",
    ("source_file", "calendar_year_be", "calendar_year", "month_id", "month_name", "boat_type_id", "boat_type_name",
     "route_id", "route_name", "number_of_trips", "number_of_passengers"),
    ("calendar_year_be", "month_id", "boat_type_id", "route_id"), boat_rows,
)
AIR_QUALITY = Dataset(
    "ฝุ่น PM2.5 รายเดือน", "air_quality_monthly",
    ("source_file", "calendar_year_be", "calendar_year", "month_id", "month_name", "pm25_avg", "pm25_min", "pm25_max", "exceed_days"),
    ("calendar_year_be", "month_id"), air_quality_rows,
)
ISLAND_WASTE = Dataset(
    "ขยะเกาะล้าน", "island_waste_monthly",
    ("source_file", "calendar_year_be", "calendar_year", "month_id", "month_name", "waste_type", "waste_amount_ton"),
    ("calendar_year_be", "month_id", "waste_type"), island_waste_rows,
)


def detect_dataset(headers: set[str]) -> Optional[Dataset]:
    """เลือกรูปแบบไฟล์จากหัวคอลัมน์ คืน None ถ้าไม่รู้จัก"""
    if {"calendar_year", "month_id", "number_of_tourists"}.issubset(headers):
        return MONTHLY
    if {"year", "population"}.issubset(headers):
        return DEMOGRAPHICS
    if {"intersection_id", "intersection_name", "vehicle_volume"}.issubset(headers):
        return ZONE_TRAFFIC
    if {"category_type", "category_label"}.issubset(headers) and any(h.startswith("pop_") for h in headers):
        return DEMOGRAPHICS_WIDE
    if {"calendar_year", "boat_type_id", "route_id", "number_of_passengers"}.issubset(headers):
        return BOAT_PASSENGERS
    if {"calendar_year", "pm25_avg"}.issubset(headers):
        return AIR_QUALITY
    if {"calendar_year", "month_name", "waste_type", "waste_amount_ton"}.issubset(headers):
        return ISLAND_WASTE
    return None


def convert_rows(dataset: Dataset, source_file: str, rows: list[dict[str, str]]) -> list[tuple]:
    """แปลงทุกแถวให้เสร็จก่อนแตะฐานข้อมูล ถ้ามีแถวที่แปลงไม่ได้จะรวบรวมเลขบรรทัดแล้วยกเลิกทั้งไฟล์"""
    values: list[tuple] = []
    errors: list[tuple[Optional[int], str]] = []
    # บรรทัด 1 ของไฟล์คือหัวตาราง แถวข้อมูลแรกจึงเป็นบรรทัด 2
    for line, row in enumerate(rows, start=2):
        try:
            values.extend(dataset.convert(source_file, row))
        except ValueError as exc:
            errors.append((line, str(exc)))
    if errors:
        raise RowErrors(errors)
    return values


def store_rows(connection: psycopg.Connection, dataset: Dataset, source_file: str, values: list[tuple]) -> int:
    """แทนที่ข้อมูลของไฟล์นี้ในตาราง คืนจำนวนแถวของไฟล์อื่นที่ถูกแทนที่เพราะเป็นช่วงเดียวกัน

    UNIQUE ของตารางรวม source_file อยู่ด้วย ไฟล์ชื่อใหม่ที่มีเดือนซ้ำกับไฟล์เก่าจึงแทรกได้โดยไม่ชน
    ถ้าปล่อยไว้ทั้งสองชุด ยอดรวมบนแดชบอร์ดจะนับเดือนเดียวกันสองรอบ จึงให้ไฟล์ที่นำเข้าทีหลังเป็นตัวจริง
    """
    table = dataset.table
    # พิกัดของแยกเก็บอยู่บนแถวปริมาณรถ ถ้าลบแถวเดิมทิ้งเฉย ๆ แยกจะหายจากแผนที่จนกว่าจะรัน geocode ใหม่
    coordinates = connection.execute(
        """SELECT DISTINCT ON (intersection_name) intersection_name, latitude, longitude, geocode_method, geocode_detail
           FROM zone_traffic_volume WHERE latitude IS NOT NULL ORDER BY intersection_name, id DESC"""
    ).fetchall() if dataset is ZONE_TRAFFIC else []

    connection.execute(f"DELETE FROM {table} WHERE source_file = %s", (source_file,))
    placeholders = ", ".join(["%s"] * len(dataset.columns))
    with connection.cursor() as cursor:
        cursor.executemany(f"INSERT INTO {table} ({', '.join(dataset.columns)}) VALUES ({placeholders})", values)
        same_period = " AND ".join(f"old.{key} = new.{key}" for key in dataset.period_keys)
        replaced = cursor.execute(
            f"""DELETE FROM {table} old USING {table} new
                WHERE new.source_file = %s AND old.source_file <> %s AND {same_period}""",
            (source_file, source_file),
        ).rowcount
        cursor.executemany(
            """UPDATE zone_traffic_volume
               SET latitude = %(latitude)s, longitude = %(longitude)s,
                   geocode_method = %(geocode_method)s, geocode_detail = %(geocode_detail)s
               WHERE intersection_name = %(intersection_name)s AND latitude IS NULL""",
            coordinates,
        )
    return replaced


def ensure_data_source(connection: psycopg.Connection, name: str, source_type: str, endpoint: Optional[str] = None, metadata: Optional[str] = None) -> int:
    row = connection.execute("SELECT id FROM data_sources WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    return connection.execute(
        "INSERT INTO data_sources (name, source_type, endpoint, metadata) VALUES (%s, %s, %s, %s) RETURNING id",
        (name, source_type, endpoint, metadata),
    ).fetchone()["id"]


def create_import_record(connection: psycopg.Connection, data_source_id: Optional[int], import_type: str, source_name: str, raw_filename: str, source_url: Optional[str], records_total: int) -> int:
    return connection.execute(
        "INSERT INTO imports (data_source_id, import_type, source_name, raw_filename, source_url, records_total, records_success, records_failed) VALUES (%s, %s, %s, %s, %s, %s, 0, 0) RETURNING id",
        (data_source_id, import_type, source_name, raw_filename, source_url, records_total),
    ).fetchone()["id"]


def attach_data_source(connection: psycopg.Connection, import_id: int, name: str, source_type: str) -> None:
    connection.execute(
        "UPDATE imports SET data_source_id = %s WHERE id = %s",
        (ensure_data_source(connection, name, source_type), import_id),
    )


def update_import_record(connection: psycopg.Connection, import_id: int, success: int, failed: int, notes: Optional[str] = None) -> None:
    connection.execute(
        "UPDATE imports SET records_success = %s, records_failed = %s, notes = %s WHERE id = %s",
        (success, failed, notes, import_id),
    )


def import_rows(connection: psycopg.Connection, filename: str, rows: list[dict[str, str]]) -> dict:
    """นำเข้า CSV หนึ่งไฟล์ที่อ่านเป็นแถวแล้ว พร้อมบันทึกประวัติลงตาราง imports ใช้ร่วมกันทั้งสคริปต์นี้และหน้าเว็บ

    ไฟล์หนึ่งเข้าทั้งไฟล์หรือไม่เข้าเลย ถ้ามีแถวผิดแม้แถวเดียว ข้อมูลเดิมของไฟล์นั้นจะยังอยู่ครบ
    """
    # ยังไม่ผูกกับแหล่งข้อมูลจนกว่าจะนำเข้าสำเร็จ ไฟล์ที่นำเข้าไม่ผ่านจะได้ไม่ไปโผล่เป็นแหล่งข้อมูลบนแดชบอร์ด
    # และไม่ทำให้ยอดระเบียนล่าสุดของแหล่งเดิมกลายเป็น 0 ทั้งที่ข้อมูลเดิมยังอยู่ครบ
    import_id = create_import_record(connection, None, "csv", filename, filename, None, len(rows))
    dataset = detect_dataset({header for header in rows[0] if header}) if rows else None
    success = replaced = 0
    errors: list[tuple[Optional[int], str]] = []
    if dataset is None:
        errors = [(None, f"รูปแบบไฟล์ไม่รองรับ: {filename}" if rows else f"ไฟล์ไม่มีแถวข้อมูล: {filename}")]
    else:
        try:
            values = convert_rows(dataset, filename, rows)
            # savepoint: ถ้านำเข้าพังกลางทาง ข้อมูลเดิมของไฟล์นี้ที่เพิ่งถูก DELETE จะถูกคืนกลับมาครบ
            # แทนที่จะหายไปหรือเหลือครึ่ง ๆ กลาง ๆ
            with connection.transaction():
                replaced = store_rows(connection, dataset, filename, values)
            success = len(values)
            attach_data_source(connection, import_id, Path(filename).stem, "csv")
        except RowErrors as exc:
            errors = exc.errors
        except psycopg.errors.UniqueViolation:
            errors = [(None, "ไฟล์มีแถวที่ซ้ำกัน (ช่วงเวลาและรายการเดียวกันปรากฏมากกว่าหนึ่งครั้ง)")]
        except Exception as exc:
            errors = [(None, str(exc))]

    notes = []
    if replaced:
        notes.append(f"แทนที่ข้อมูลช่วงเดียวกันจากไฟล์อื่น {replaced} แถว")
    if dataset is ZONE_TRAFFIC and success:
        missing = connection.execute(
            "SELECT COUNT(DISTINCT intersection_name) AS total FROM zone_traffic_volume WHERE source_file = %s AND latitude IS NULL",
            (filename,),
        ).fetchone()["total"]
        if missing:
            notes.append(f"มี {missing} แยกที่ยังไม่มีพิกัด รัน scripts/geocode_intersections.py เพื่อหาพิกัด")
    if len(errors) > MAX_ERRORS_RECORDED:
        notes.append(f"บันทึกข้อผิดพลาด {MAX_ERRORS_RECORDED} แถวแรก จากทั้งหมด {len(errors)} แถว")

    with connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO import_errors (import_id, row_number, error_text) VALUES (%s, %s, %s)",
            [(import_id, line, text) for line, text in errors[:MAX_ERRORS_RECORDED]],
        )
    # ไฟล์เข้าทั้งไฟล์หรือไม่เข้าเลย เมื่อมีข้อผิดพลาดจึงนับว่าไม่สำเร็จทุกแถว
    failed = len(rows) if errors else 0
    update_import_record(connection, import_id, success, failed, " · ".join(notes) or None)
    return {
        "import_id": import_id,
        "filename": filename,
        "label": dataset.label if dataset else None,
        "records_total": len(rows),
        "records_success": success,
        "records_failed": failed,
        "notes": notes,
        "errors": [{"row_number": line, "error_text": text} for line, text in errors[:MAX_ERRORS_RECORDED]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="นำเข้า CSV ข้อมูลท่องเที่ยวและประชากร")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.files:
        if not path.exists():
            raise SystemExit(f"ไม่พบไฟล์: {path}")
    initialize_database()
    result: list[str] = []
    with connect() as connection:
        for path in args.files:
            outcome = import_rows(connection, path.name, read_csv(path))
            connection.commit()
            if not outcome["errors"]:
                result.append(f"{path.name}: {outcome['label']} {outcome['records_success']} แถว")
            else:
                result.append(f"{path.name}: นำเข้าไม่สำเร็จ")
                for error in outcome["errors"]:
                    line = f"บรรทัด {error['row_number']}: " if error["row_number"] else ""
                    result.append(f"  - {line}{error['error_text']}")
            result.extend(f"  ({note})" for note in outcome["notes"])
    print("\n".join(result))


if __name__ == "__main__":
    main()
