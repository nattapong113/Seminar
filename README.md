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
- `GET /api/pattaya/report`: ตรวจสอบ metrics ที่นำเข้าจากชีตพัทยา

## นำเข้าไฟล์ Excel ของจังหวัดชลบุรี

ไฟล์รายงานแบบหลายชีต เช่น `ข้อมูลชล (1).xlsx` จะถูกเก็บข้อมูลต้นฉบับทุกช่องใน `raw_excel_cells` และแยกตัวเลขจากชีต `พัทยา ชลบุรี` ลง `pattaya_report_metrics` โดยไม่ทับข้อมูลรายวันเดิม

```powershell
& .\.venv\Scripts\python.exe scripts\import_excel.py "C:\Users\PONG\Downloads\ข้อมูลชล (1).xlsx"
```

หลังนำเข้า ตรวจผลได้ที่ `http://127.0.0.1:8000/api/pattaya/report` หรือเปิด Swagger ที่ `http://127.0.0.1:8000/docs`

## นำเข้า CSV

ระบบรองรับไฟล์นักท่องเที่ยวรายเดือนและไฟล์ข้อมูลประชากร โดยตรวจ encoding ภาษาไทยให้อัตโนมัติ

```powershell
& .\.venv\Scripts\python.exe scripts\import_csv.py `
	"C:\Users\PONG\Downloads\172-.csv" `
	"C:\Users\PONG\Downloads\238-.csv"
```

- `172-.csv` เก็บใน `tourism_monthly` และใช้สร้างกราฟแนวโน้มจริง
- `238-.csv` เก็บใน `demographic_profiles` สำหรับวิเคราะห์กลุ่มประชากร
- API ข้อมูลรายเดือน: `GET /api/tourism/monthly`
- API ข้อมูลประชากร: `GET /api/demographics?year=2024&category=เพศ`

ข้อมูลในเวอร์ชันเริ่มต้นเป็นข้อมูลจำลองเพื่อทดสอบระบบ ควรเปลี่ยนเป็นข้อมูลจริงและเพิ่มระบบยืนยันตัวตนก่อนใช้งานจริง
