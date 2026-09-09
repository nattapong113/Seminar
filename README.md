# Pattaya Smart Tourism Dashboard

ระบบต้นแบบคลังข้อมูลและแดชบอร์ดวิเคราะห์พฤติกรรมนักท่องเที่ยวเมืองพัทยา

## เริ่มต้นใช้งาน

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

เปิด `http://127.0.0.1:8000` ในเบราว์เซอร์ ระบบจะสร้าง `pattaya_tourism.db` และข้อมูลตัวอย่างให้อัตโนมัติ

## โครงสร้างข้อมูล

- `tourism_observations`: ข้อมูลรายวันระดับโซน ประกอบด้วยพิกัด จำนวนคน รายได้ ระยะเวลาพัก สภาพอากาศ และวันหยุด
- `GET /api/summary`: KPI ช่วง 7 วันล่าสุด
- `GET /api/zones`: ข้อมูล heatmap และการจัดอันดับโซน
- `GET /api/trends`: แนวโน้มรายวัน
- `GET /api/recommendations`: คำแนะนำเชิงปฏิบัติจากสัญญาณข้อมูล
- `GET /api/weather/live`: สภาพอากาศปัจจุบันจาก Open-Meteo
- `GET /api/holidays?year=2026`: วันหยุดและเทศกาลของไทย (นำเข้าจาก Open Data ล่วงหน้าด้วยสคริปต์ด้านล่าง)
- `GET /api/poi?category=hotel&limit=200`: สถานที่ท่องเที่ยว/ธุรกิจในพัทยา (นำเข้าจาก OpenStreetMap ด้วยสคริปต์ด้านล่าง)
- `GET /api/traffic/zones?year=2025`: ปริมาณรถต่อแยกทั่วเมือง ใช้แทนความคึกคักรายโซนจริง (นำเข้าจาก Open Data ด้วยสคริปต์ด้านล่าง)
- `GET /api/traffic/trends?intersection_name=แยกพัทยากลาง`: แนวโน้มปริมาณรถรายเดือนของแยกที่ระบุ (ไม่ระบุ = รวมทุกแยก)
- `GET /api/pattaya/report`: ตรวจสอบ metrics ที่นำเข้าจากชีตพัทยา

## นำเข้าไฟล์ Excel ของจังหวัดชลบุรี

ไฟล์รายงานแบบหลายชีต เช่น `ข้อมูลชล (1).xlsx` จะถูกเก็บข้อมูลต้นฉบับทุกช่องใน `raw_excel_cells` และแยกตัวเลขจากชีต `พัทยา ชลบุรี` ลง `pattaya_report_metrics` โดยไม่ทับข้อมูลรายวันเดิม

```powershell
& .\.venv\Scripts\python.exe scripts\import_excel.py "C:\Users\PONG\Downloads\ข้อมูลชล (1).xlsx"
```

หลังนำเข้า ตรวจผลได้ที่ `http://127.0.0.1:8000/api/pattaya/report` หรือเปิด Swagger ที่ `http://127.0.0.1:8000/docs`

## นำเข้า CSV

ระบบรองรับไฟล์นักท่องเที่ยวรายเดือนและไฟล์ข้อมูลประชากร โดยตรวจ encoding ภาษาไทยให้อัตโนมัติ ไฟล์ต้นฉบับดาวน์โหลดได้จาก Open Data ของเทศบาลเมืองพัทยา ([data.pattaya.go.th](https://data.pattaya.go.th)) ชุดข้อมูล "นักท่องเที่ยวเดินทางลงเกาะล้าน" (172) และ "ประชากรแฝงของเมืองพัทยา" (238)

```powershell
& .\.venv\Scripts\python.exe scripts\import_csv.py `
	"C:\Users\PONG\Downloads\172-.csv" `
	"C:\Users\PONG\Downloads\238-.csv"
```

- `172-.csv` เก็บใน `tourism_monthly` และใช้สร้างกราฟแนวโน้มจริง
- `238-.csv` (รูปแบบตาราง `category_type,category_label,pop_YYYY,...`) เก็บใน `demographic_profiles` สำหรับวิเคราะห์กลุ่มประชากร
- API ข้อมูลรายเดือน: `GET /api/tourism/monthly`
- API ข้อมูลประชากร: `GET /api/demographics?year=2024&category=เพศ`

## นำเข้าปริมาณรถรายแยก (ตัวแทนความคึกคักรายโซน)

ยังไม่มี Open Data ที่ระบุ "จำนวนนักท่องเที่ยว/รายได้รายโซน" ตรง ๆ จึงใช้ชุดข้อมูล "ปริมาณรถ" (112) จาก data.pattaya.go.th ซึ่งมีปริมาณรถรายเดือนแยกตามสี่แยกจริงทั่วเมือง (จอมเทียน, นาเกลือ, พัทยากลาง ฯลฯ) แทนความคึกคักของแต่ละโซน เก็บใน `zone_traffic_volume`

```powershell
& .\.venv\Scripts\python.exe scripts\import_csv.py "C:\Users\PONG\Downloads\112-.csv"
& .\.venv\Scripts\python.exe scripts\geocode_intersections.py
```

ไฟล์ต้นฉบับไม่มีพิกัด สคริปต์ `geocode_intersections.py` จะจับคู่ชื่อแยกกับโหนดสัญญาณไฟจราจร (`highway=traffic_signals`) ที่มีชื่อตรงกันใน OpenStreetMap ให้อัตโนมัติ ปัจจุบันจับคู่ได้ 8 จาก 42 แยก (แยกที่เหลือยังไม่มีชื่อตรงกันใน OpenStreetMap จึงไม่มีพิกัด แต่ยังมีข้อมูลปริมาณรถอยู่ ใช้แสดงเป็นอันดับ/กราฟได้)

## นำเข้าวันหยุดและเทศกาล

ดึงวันหยุดราชการและเทศกาลของไทยจาก Open Data (World Holidays API) มาเก็บในตาราง `public_holidays`

```powershell
& .\.venv\Scripts\python.exe scripts\import_holidays.py
& .\.venv\Scripts\python.exe scripts\import_holidays.py --years 2026 2027
```

ตรวจผลได้ที่ `GET /api/holidays?year=2026`

## นำเข้าสถานที่ท่องเที่ยว/ธุรกิจในพัทยา

ดึงข้อมูลสถานที่ท่องเที่ยว โรงแรม ร้านอาหาร คาเฟ่ บาร์ ฯลฯ ในเขตพัทยาจาก OpenStreetMap (Overpass API) มาเก็บในตาราง `poi_businesses`

```powershell
& .\.venv\Scripts\python.exe scripts\import_poi_osm.py
```

ตรวจผลได้ที่ `GET /api/poi?category=hotel`

`tourism_observations` (heatmap/summary รายโซน) ยังเป็นข้อมูลจำลองสำหรับสาธิต ส่วนนักท่องเที่ยวรายเดือน ประชากร วันหยุด/เทศกาล และสถานที่ท่องเที่ยว/ธุรกิจ เชื่อมกับ Open Data จริงแล้วตามด้านบน ควรเพิ่มระบบยืนยันตัวตนก่อนใช้งานจริง
