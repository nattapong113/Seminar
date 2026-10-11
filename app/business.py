"""ข้อมูลและการวิเคราะห์ของธุรกิจแต่ละราย (หน้า "ธุรกิจของฉัน")

ต่างจากส่วนอื่นของระบบที่เป็น Open Data ของเมือง ข้อมูลในส่วนนี้เป็นของผู้ใช้แต่ละคน คนอื่นมองไม่เห็น
ระบบเอารายได้รายเดือนของธุรกิจมาวางคู่กับบริบทของเมือง (นักท่องเที่ยว ฝน วันหยุด) ด้วยวิธีเดียวกับ /api/impact
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from scripts import import_csv

from . import analytics

# ชื่อคอลัมน์ที่รับได้ของแต่ละช่อง ให้คนทำไฟล์เองไม่ต้องจำชื่อเดียว
YEAR_COLUMNS = ("year", "calendar_year")
MONTH_COLUMNS = ("month", "month_id")
REVENUE_COLUMNS = ("revenue", "revenue_baht")
CUSTOMER_COLUMNS = ("customers", "guests")
TEMPLATE_HEADER = "year,month,revenue,customers"

# ต่ำกว่านี้ถือว่าเปลี่ยนแปลงน้อยเกินกว่าจะสั่งปรับกำลังคน (ค่าเดียวกับคำแนะนำของเมือง)
MATERIAL_CHANGE_PERCENT = 5
THAI_MONTHS = (
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
)


def _pick(row: dict[str, str], columns: tuple[str, ...]) -> Optional[str]:
    return next((row[column] for column in columns if import_csv.clean(row.get(column))), None)


def parse_upload(data: bytes, filename: str) -> list[dict]:
    """อ่านไฟล์รายได้รายเดือน คืนแถวที่เรียงตามเวลาแล้ว โยน RowErrors ถ้ามีแถวผิด เดือนซ้ำ หรือเดือนขาด

    ต้องเป็นเดือนต่อเนื่องไม่มีช่องว่าง เพราะการหาฤดูกาลและการพยากรณ์นับตำแหน่งของเดือนในอนุกรม
    """
    rows = import_csv.decode_csv(data, filename)
    headers = {header for header in rows[0] if header} if rows else set()
    missing = [group[0] for group in (YEAR_COLUMNS, MONTH_COLUMNS, REVENUE_COLUMNS) if not headers & set(group)]
    if not rows or missing:
        raise import_csv.RowErrors([(None, (
            f"ไฟล์ต้องมีคอลัมน์ {TEMPLATE_HEADER} (customers ไม่ใส่ก็ได้)"
            + (f" ขาดคอลัมน์ {', '.join(missing)}" if rows else " และมีข้อมูลอย่างน้อยหนึ่งแถว")
        ))])

    parsed, errors = [], []
    for line, row in enumerate(rows, start=2):
        try:
            year = import_csv.integer(_pick(row, YEAR_COLUMNS))
            # รับได้ทั้ง พ.ศ. และ ค.ศ. เก็บเป็น ค.ศ. เหมือนตารางอื่น
            year = year - 543 if year >= 2400 else year
            if not 2000 <= year <= date.today().year:
                raise ValueError(f"ปี {_pick(row, YEAR_COLUMNS)!r} อยู่นอกช่วงที่รับได้")
            revenue = import_csv.decimal(_pick(row, REVENUE_COLUMNS))
            if revenue < 0:
                raise ValueError("รายได้ติดลบ")
            customers = _pick(row, CUSTOMER_COLUMNS)
            parsed.append({
                "line": line, "year": year, "month": import_csv.month_number(_pick(row, MONTH_COLUMNS)),
                "revenue": revenue, "customers": import_csv.integer(customers) if customers else None,
            })
        except ValueError as exc:
            errors.append((line, str(exc)))
    if errors:
        raise import_csv.RowErrors(errors)

    parsed.sort(key=lambda row: (row["year"], row["month"]))
    for before, after in zip(parsed, parsed[1:]):
        gap = (after["year"] * 12 + after["month"]) - (before["year"] * 12 + before["month"])
        if gap == 0:
            errors.append((after["line"], f"เดือน {after['year']}-{after['month']:02d} ซ้ำกับบรรทัด {before['line']}"))
        elif gap > 1:
            errors.append((after["line"], f"ข้อมูลขาดช่วง: ไม่มีเดือนระหว่าง {before['year']}-{before['month']:02d} กับ {after['year']}-{after['month']:02d}"))
    if errors:
        raise import_csv.RowErrors(errors)
    return parsed


def _change(current: float, previous: float) -> Optional[float]:
    return round((current / previous - 1) * 100, 1) if previous else None


def analyze(rows: list[dict], tourists: dict[str, float], visitor_index: dict[str, float],
            rain: dict[str, float], holidays: dict[str, int], today_month: str) -> dict:
    """วิเคราะห์รายได้ของธุรกิจหนึ่งราย rows เรียงตามเวลาและต่อเนื่องแล้ว

    tourists / visitor_index / rain / holidays เป็นบริบทของเมือง คีย์เป็นป้ายเดือน YYYY-MM
    """
    labels = [f"{row['year']:04d}-{row['month']:02d}" for row in rows]
    revenue = [float(row["revenue"]) for row in rows]
    has_customers = all(row["customers"] is not None for row in rows)
    months = [{
        "month": label, "revenue": round(value), "customers": row["customers"], "tourists": tourists.get(label),
        "revenue_per_customer": round(value / row["customers"]) if row["customers"] else None,
    } for label, value, row in zip(labels, revenue, rows)]

    last_year, previous_year = revenue[-12:], revenue[-24:-12]
    summary = {
        "first_month": labels[0], "last_month": labels[-1], "months": len(rows),
        "revenue_last_12": round(sum(last_year)),
        # เทียบได้ก็ต่อเมื่อมีครบสองปีเต็ม ไม่งั้นจะเทียบ 12 เดือนกับช่วงที่สั้นกว่า
        "revenue_change_percent": _change(sum(last_year), sum(previous_year)) if len(revenue) >= 24 else None,
        "customers_last_12": sum(row["customers"] for row in rows[-12:]) if has_customers else None,
        "revenue_per_customer": (
            round(sum(last_year) / sum(row["customers"] for row in rows[-12:]))
            if has_customers and sum(row["customers"] for row in rows[-12:]) else None
        ),
    }
    result = {"summary": summary, "months": months, "seasonality": [], "links": {}, "forecast": None, "recommendations": []}
    recommendations = result["recommendations"]
    lag = (int(today_month[:4]) * 12 + int(today_month[5:7])) - (int(labels[-1][:4]) * 12 + int(labels[-1][5:7]))
    if lag >= 2:
        recommendations.append({
            "key": "stale_data", "signal": "คุณภาพข้อมูล",
            "title": f"ข้อมูลล่าสุดคือเดือน {labels[-1]}",
            "detail": f"ช้ากว่าปัจจุบัน {lag} เดือน ควรอัปโหลดเดือนล่าสุดก่อนใช้คำแนะนำด้านล่างตัดสินใจ",
        })

    if len(revenue) < 2 * analytics.MONTHS_IN_YEAR:
        result["note"] = f"มีข้อมูล {len(revenue)} เดือน ต้องมีอย่างน้อย 24 เดือนจึงจะหาฤดูกาล ผลกระทบ และพยากรณ์ได้"
        return result

    first_month = rows[0]["month"]
    result["seasonality"] = sorted(
        ({"month_id": (first_month - 1 + position) % 12 + 1, "index": round(value, 3)}
         for position, value in enumerate(analytics.seasonal_indices(revenue))),
        key=lambda item: item["month_id"],
    )
    peak = max(result["seasonality"], key=lambda item: item["index"])
    low = min(result["seasonality"], key=lambda item: item["index"])
    recommendations.append({
        "key": "seasonality", "signal": "ฤดูกาลของธุรกิจ",
        "title": f"เดือน{THAI_MONTHS[peak['month_id'] - 1]}รายได้สูงสุด เดือน{THAI_MONTHS[low['month_id'] - 1]}ต่ำสุด",
        "detail": (
            f"เดือน{THAI_MONTHS[peak['month_id'] - 1]}สูงกว่าค่าเฉลี่ยทั้งปี {(peak['index'] - 1) * 100:.0f}% "
            f"ส่วนเดือน{THAI_MONTHS[low['month_id'] - 1]}ต่ำกว่า {(1 - low['index']) * 100:.0f}% "
            "ใช้ช่วงรายได้ต่ำสำหรับงานซ่อมบำรุงและการลาพักของพนักงาน"
        ),
    })

    # ตัดฤดูกาลและแนวโน้มของรายได้ออกก่อน เหลือดัชนี 1.0 = เท่าที่ควรเป็น แล้วจึงเทียบกับปัจจัยของเมือง
    index = dict(zip(labels, analytics.deseasonalized_index(revenue)))
    links = result["links"]

    shared = [label for label in labels if label in visitor_index]
    if len(shared) >= analytics.MONTHS_IN_YEAR:
        links["tourists"] = analytics.tourism_link([index[label] for label in shared], [visitor_index[label] for label in shared])
    rainy = [label for label in labels if label in rain]
    if len(rainy) >= analytics.MONTHS_IN_YEAR:
        links["rain"] = analytics.rain_impact([index[label] for label in rainy], [rain[label] for label in rainy], rainy)
    first_holiday = min(holidays) if holidays else None
    holiday_months = [label for label in labels if first_holiday and label >= first_holiday]
    if len(holiday_months) >= analytics.MONTHS_IN_YEAR:
        links["holidays"] = analytics.holiday_impact(
            [index[label] for label in holiday_months], [holidays.get(label, 0) for label in holiday_months], holiday_months
        )
    for link in links.values():
        link.pop("monthly", None)

    link = links.get("tourists")
    if link:
        recommendations.append({
            "key": "tourist_link", "signal": "ความสัมพันธ์กับนักท่องเที่ยว",
            "title": (
                "รายได้ขึ้นลงตามจำนวนนักท่องเที่ยวของเมือง" if link["significant"]
                else "ยังสรุปไม่ได้ว่ารายได้ขึ้นลงตามจำนวนนักท่องเที่ยวเกาะล้าน"
            ),
            "detail": (
                f"เดือนที่นักท่องเที่ยวมากกว่าที่คาด 10% รายได้ต่างจากที่คาด {link['effect_percent']:+}% "
                f"(r = {link['correlation']}, {link['months_used']} เดือน) ใช้พยากรณ์นักท่องเที่ยวของเมืองช่วยวางแผนได้"
                if link["significant"] else
                f"r = {link['correlation']} ช่วงความเชื่อมั่นคร่อมศูนย์ ({link['months_used']} เดือน) "
                "ลูกค้าของธุรกิจนี้อาจไม่ใช่กลุ่มเดียวกับนักท่องเที่ยวเกาะล้าน จึงไม่ควรใช้ตัวเลขของเมืองแทนข้อมูลของตัวเอง"
            ),
            "assumption": "เป็นความสัมพันธ์หลังตัดฤดูกาลออกแล้ว ไม่ใช่การพิสูจน์เหตุและผล",
        })
    link = links.get("rain")
    if link and link["significant"]:
        recommendations.append({
            "key": "rain_link", "signal": "ผลจากฝน",
            "title": "เดือนที่ฝนมากกว่าปกติ รายได้ต่างจากที่คาด",
            "detail": f"ฝนมากกว่าปกติ 100 มม. รายได้ต่างจากที่คาด {link['effect_percent']:+}% (r = {link['correlation']}, {link['months_used']} เดือน)",
            "assumption": "เป็นความสัมพันธ์หลังตัดฤดูกาลออกแล้ว ไม่ใช่การพิสูจน์เหตุและผล",
        })

    forecast = analytics.forecast_series(revenue, 3)
    if "error" not in forecast:
        points = []
        for point in forecast["points"]:
            position = int(labels[-1][:4]) * 12 + int(labels[-1][5:7]) - 1 + point["step"]
            points.append({
                "month": f"{position // 12:04d}-{position % 12 + 1:02d}",
                "revenue": point["visitors"], "lower": point["lower"], "upper": point["upper"],
            })
        result["forecast"] = {
            "points": points, "model": forecast["model"]["label"],
            "test_mape_percent": forecast["accuracy"]["test_mape_percent"],
        }
        nearest = points[0]
        same_month_last_year = revenue[-12]
        change = (nearest["revenue"] / same_month_last_year - 1) * 100 if same_month_last_year else 0
        recommendations.insert(1 if lag >= 2 else 0, {
            "key": "forecast", "signal": "พยากรณ์รายได้",
            "title": f"เดือน {nearest['month']} คาดรายได้ {nearest['revenue']:,} บาท",
            "detail": (
                f"เทียบเดือนเดียวกันปีก่อนที่ {round(same_month_last_year):,} บาท ({change:+.1f}%) "
                + (f"ควร{'เพิ่ม' if change > 0 else 'ลด'}กำลังคนและสต็อกราว {abs(change):.0f}%"
                   if abs(change) >= MATERIAL_CHANGE_PERCENT
                   else "ยังไม่ต้องปรับกำลังคน เพราะเปลี่ยนแปลงน้อยกว่าความคลาดเคลื่อนของแบบจำลอง")
                + (f" ช่วงประมาณ 95% คือ {nearest['lower']:,} ถึง {nearest['upper']:,} บาท" if nearest["lower"] is not None else "")
            ),
            "assumption": f"แบบจำลอง{forecast['model']['label']} คลาดเคลื่อน {forecast['accuracy']['test_mape_percent']}% ในปีทดสอบ และสมมติว่ากำลังคนแปรผันตรงกับรายได้",
        })
    return result


def facts_for_ai(name: str, analysis: dict) -> str:
    """ข้อเท็จจริงของธุรกิจที่ส่งให้ AI เรียบเรียง ตัวเลขจัดรูปแบบไว้แล้ว AI ต้องคัดลอกไปตรง ๆ"""
    summary = analysis["summary"]
    facts = [
        f"ธุรกิจชื่อ {name} มีข้อมูลรายได้ {summary['months']} เดือน ตั้งแต่ {summary['first_month']} ถึง {summary['last_month']}",
        f"รายได้ 12 เดือนล่าสุดรวม {summary['revenue_last_12']:,} บาท"
        + (f" เปลี่ยนแปลง {summary['revenue_change_percent']:+}% เทียบ 12 เดือนก่อนหน้า" if summary["revenue_change_percent"] is not None else ""),
    ]
    if summary["revenue_per_customer"] is not None:
        facts.append(f"ลูกค้า 12 เดือนล่าสุด {summary['customers_last_12']:,} ราย รายได้เฉลี่ยต่อลูกค้า {summary['revenue_per_customer']:,} บาท")
    if analysis["seasonality"]:
        peak = max(analysis["seasonality"], key=lambda item: item["index"])
        low = min(analysis["seasonality"], key=lambda item: item["index"])
        facts.append(
            f"รายได้สูงสุดในเดือน{THAI_MONTHS[peak['month_id'] - 1]} สูงกว่าค่าเฉลี่ยทั้งปี {(peak['index'] - 1) * 100:.0f}% "
            f"ต่ำสุดในเดือน{THAI_MONTHS[low['month_id'] - 1]} ต่ำกว่าค่าเฉลี่ย {(1 - low['index']) * 100:.0f}%"
        )
    for point in (analysis["forecast"] or {}).get("points", []):
        facts.append(
            f"พยากรณ์รายได้เดือน {point['month']}: {point['revenue']:,} บาท"
            + (f" (ช่วงประมาณ 95% คือ {point['lower']:,} ถึง {point['upper']:,} บาท)" if point["lower"] is not None else "")
        )
    if analysis["forecast"]:
        facts.append(f"แบบจำลองพยากรณ์คลาดเคลื่อน {analysis['forecast']['test_mape_percent']}% ในปีทดสอบ")
    labels = {
        "tourists": "เดือนที่นักท่องเที่ยวเกาะล้านมากกว่าที่คาด 10%",
        "rain": "เดือนที่ฝนมากกว่าปกติ 100 มม.",
        "holidays": "เดือนที่วันหยุดราชการเพิ่มขึ้น 1 วัน",
    }
    for key, link in analysis["links"].items():
        facts.append(
            f"{labels[key]} รายได้ต่างจากที่คาด {link['effect_percent']:+}% "
            f"ความสัมพันธ์นี้{'ชัดเจนทางสถิติ' if link['significant'] else 'ยังสรุปไม่ได้ทางสถิติ'} (ข้อมูล {link['months_used']} เดือน)"
        )
    return "\n".join(f"- {fact}" for fact in facts)
