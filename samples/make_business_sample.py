"""สร้างไฟล์ข้อมูลธุรกิจ "สมมติ" สำหรับทดสอบหน้า ธุรกิจของฉัน

    python samples/make_business_sample.py   # เขียน samples/business-sample-hotel.csv

ตัวเลขทั้งหมดแต่งขึ้น ไม่ใช่ข้อมูลของธุรกิจจริง ตอนอัปโหลดต้องติ๊ก "เป็นข้อมูลสมมติเพื่อสาธิต" ทุกครั้ง
รูปแบบ: โรงแรมเล็กในพัทยา 48 เดือน (ม.ค. 2565 – ธ.ค. 2568) ไฮซีซัน พ.ย.–มี.ค. โลว์ซีซัน ก.ย.
ปี 2565 ยังฟื้นจากโควิด แล้วโตขึ้นทีละปี และมีสัญญาณรบกวนสุ่ม (seed คงที่ รันซ้ำได้ผลเดิม)
"""
import csv
import random
from pathlib import Path

SEASON = [1.25, 1.20, 1.15, 1.05, 0.90, 0.85, 0.95, 0.95, 0.70, 0.85, 1.10, 1.30]  # ม.ค.–ธ.ค.
YEAR_LEVEL = {2565: 0.70, 2566: 0.95, 2567: 1.05, 2568: 1.00}
BASE_REVENUE = 1_200_000  # บาท/เดือน
REVENUE_PER_GUEST = 2_150

random.seed(2568)
out = Path(__file__).with_name("business-sample-hotel.csv")
with out.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["year", "month", "revenue", "customers"])
    for year, level in YEAR_LEVEL.items():
        for month in range(1, 13):
            revenue = BASE_REVENUE * level * SEASON[month - 1] * random.uniform(0.92, 1.08)
            customers = revenue / (REVENUE_PER_GUEST * random.uniform(0.95, 1.05))
            writer.writerow([year, month, f"{revenue:.2f}", round(customers)])
print(f"เขียน {out.name} แล้ว 48 เดือน")
