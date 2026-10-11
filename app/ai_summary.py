"""สรุปผลเป็นภาษาคนด้วย Gemini API (โควตาฟรีของ Google AI Studio)

AI ทำหน้าที่เรียบเรียงเท่านั้น ตัวเลขทุกตัวคำนวณโดยระบบแล้วส่งไปเป็นข้อความ ไม่ให้ AI คิดเลขหรือสรุปเกินสถิติ
เรียกผ่าน urllib ของไลบรารีมาตรฐาน ไม่เพิ่มไลบรารีภายนอก เหตุผลเดียวกับ auth.py
"""

from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# เปลี่ยนรุ่นได้ด้วย GEMINI_MODEL ใน .env เมื่อ Google ออกรุ่นใหม่หรือเลิกรุ่นนี้
DEFAULT_MODEL = "gemini-3.5-flash"

CITY_AUDIENCE = "ผู้บริหารเมืองพัทยาและผู้ประกอบการท่องเที่ยว บทสรุปเริ่มจากแนวโน้มนักท่องเที่ยวและพยากรณ์"
BUSINESS_AUDIENCE = "เจ้าของธุรกิจรายนี้ บทสรุปเริ่มจากแนวโน้มรายได้และพยากรณ์รายได้ของธุรกิจ และพูดถึงเฉพาะธุรกิจนี้"

INSTRUCTION = """คุณเป็นผู้ช่วยของแดชบอร์ดการท่องเที่ยวเมืองพัทยา ทำสองอย่างแล้วตอบเป็น JSON

1. summary: บทสรุปสำหรับผู้อ่านที่ระบุไว้ ภาษาไทย 4-6 ประโยค เป็นย่อหน้าเดียว ไม่ใช้หัวข้อ ไม่ใช้สัญลักษณ์ markdown
2. priorities: เลือกข้อเสนอแนะที่ควรทำก่อนไม่เกิน 6 ข้อจากรายการที่ได้รับ เรียงจากเร่งด่วนที่สุด
   - key ต้องคัดลอกจากในวงเล็บเหลี่ยมของรายการเท่านั้น ห้ามแต่งข้อเสนอแนะใหม่
   - เรื่องที่ต้องลงมือภายใน 7 วันมาก่อน ตามด้วยเรื่องของเดือนถัดไป แล้วจึงเรื่องเชิงโครงสร้างระยะยาว
   - ถ้าข้อมูลที่ใช้ตัดสินใจเก่า ให้เรื่องคุณภาพข้อมูลอยู่ลำดับต้น เพราะกระทบข้ออื่นทั้งหมด
   - reason: เหตุผลที่อยู่ลำดับนี้ ภาษาไทยไม่เกิน 25 คำ

กติกาที่ใช้กับทั้ง summary และ reason
- ใช้เฉพาะข้อเท็จจริงที่ได้รับ ห้ามเพิ่มตัวเลข สาเหตุ หรือข้อมูลภายนอก
- คัดลอกตัวเลขตามที่ให้มาทุกหลัก ห้ามปัดเศษ ห้ามคำนวณตัวเลขใหม่
- ข้อใดระบุว่า "ยังสรุปไม่ได้ทางสถิติ" ต้องบอกว่ายังสรุปไม่ได้ ห้ามเขียนให้ดูเหมือนมีผล
- เป็นความสัมพันธ์ ไม่ใช่เหตุและผล ห้ามใช้คำว่า "ทำให้" หรือ "ส่งผลให้" กับผลทางสถิติ
- ถ้าข้อมูลล่าสุดเก่ากว่าปัจจุบัน ให้บอกไว้ในบทสรุปด้วย
- เริ่มจากเรื่องที่ผู้อ่านต้องรู้ก่อน
- กระชับ ประโยคสั้น รวมไม่เกิน 120 คำ ไม่ต้องใส่ทุกตัวเลขที่ได้รับ เลือกเฉพาะที่ผู้บริหารใช้ตัดสินใจ
- ช่วงประมาณของพยากรณ์ใส่เฉพาะเดือนแรก ไม่ต้องใส่ค่า r และจำนวนเดือนของข้อมูล"""

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "priorities": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"key": {"type": "STRING"}, "reason": {"type": "STRING"}},
                "required": ["key", "reason"],
            },
        },
    },
    "required": ["summary", "priorities"],
}
MAX_PRIORITIES = 6


class SummaryError(Exception):
    """ข้อความพร้อมแสดงให้ผู้ดูแลระบบเห็นบนหน้าเว็บ"""


def api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or None


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


def numbers_in(text: str) -> set[float]:
    return {float(match.replace(",", "")) for match in NUMBER.findall(text)}


def unverified_numbers(summary: str, facts: str) -> list[str]:
    """ตัวเลขในบทสรุปที่ไม่มีในข้อเท็จจริงที่ส่งไป ใช้เตือนผู้ดูแลว่า AI อาจแต่งหรือปัดตัวเลขเอง

    ข้ามจำนวนเต็มเล็ก ๆ (เช่น "3 เดือน" "2 ปี") ที่เป็นคำพูดทั่วไปมากกว่าตัวเลขจากข้อมูล
    """
    allowed = numbers_in(facts)
    found = []
    for match in NUMBER.findall(summary):
        value = float(match.replace(",", ""))
        if value not in allowed and not (value < 100 and value.is_integer()) and match not in found:
            found.append(match)
    return found


# รุ่นสำรองเมื่อรุ่นหลักคนใช้เยอะจนเซิร์ฟเวอร์ของ Google ตอบ 503 (เกิดเป็นช่วง ๆ กับโควตาฟรี) รุ่น lite มักว่างกว่า
FALLBACK_MODEL = "gemini-3.5-flash-lite"


def generate(facts: str, options: dict[str, str], audience: str = CITY_AUDIENCE) -> dict:
    """ส่งข้อเท็จจริงและรายการข้อเสนอแนะ (key -> ข้อความ) ให้ Gemini คืน {"content", "ranking", "model"}

    ranking คือ [{"key", "reason"}] ตามลำดับที่ AI จัด กรองแล้วให้เหลือเฉพาะ key ที่มีอยู่จริง
    โยน SummaryError ถ้าเรียกไม่สำเร็จ
    """
    if not api_key():
        raise SummaryError("ยังไม่ได้ตั้ง GEMINI_API_KEY ใน .env")
    models = list(dict.fromkeys([model_name(), FALLBACK_MODEL]))
    for model in models:
        try:
            return {**_request(model, facts, options, audience), "model": model}
        except HTTPError as exc:
            if exc.code == 503 and model != models[-1]:
                continue
            if exc.code == 503:
                raise SummaryError("เซิร์ฟเวอร์ของ Gemini มีผู้ใช้หนาแน่นชั่วคราว (ไม่เกี่ยวกับโควตาของเรา) รอสักครู่แล้วกดสรุปใหม่อีกครั้ง") from None
            if exc.code == 429:
                raise SummaryError("โควตาฟรีของ Gemini หมดชั่วคราว ลองใหม่ภายหลัง") from None
            try:
                detail = json.load(exc)["error"]["message"]
            except Exception:
                detail = exc.reason
            raise SummaryError(f"Gemini ตอบ {exc.code}: {detail}") from None
        except (URLError, TimeoutError) as exc:
            raise SummaryError(f"เชื่อมต่อ Gemini ไม่ได้: {exc}") from None
    raise SummaryError("เรียก Gemini ไม่สำเร็จ")


def _request(model: str, facts: str, options: dict[str, str], audience: str) -> dict:
    listed = "\n".join(f"[{key}] {text}" for key, text in options.items())
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": f"ผู้อ่าน: {audience}\n\nข้อเท็จจริงจากระบบ:\n{facts}\n\nรายการข้อเสนอแนะ:\n{listed}"}]}],
        # อุณหภูมิต่ำให้ยึดข้อเท็จจริง
        # โทเคนที่รุ่นนี้ใช้ "คิด" ก่อนตอบนับรวมใน maxOutputTokens ด้วย ทดลองจริงแล้วงานนี้คิดไปกว่า 3,000 โทเคน
        # จนไม่เหลือให้ตอบและได้ข้อความว่างกลับมา จึงสั่งให้คิดน้อย (งานเรียบเรียงไม่ต้องคิดลึก) และเผื่อเพดานไว้สูง
        "generationConfig": {
            "temperature": 0.2, "maxOutputTokens": 8192, "thinkingConfig": {"thinkingLevel": "low"},
            "responseMimeType": "application/json", "responseSchema": RESPONSE_SCHEMA,
        },
    }).encode("utf-8")
    request = Request(
        ENDPOINT.format(model=model), data=body,
        # ส่ง key ทาง header ไม่ใส่ใน URL จะได้ไม่ไปโผล่ใน log
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key()},
    )
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    candidates = payload.get("candidates") or [{}]
    parts = (candidates[0].get("content") or {}).get("parts") or []
    content = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
    if not content:
        reason = candidates[0].get("finishReason") or (payload.get("promptFeedback") or {}).get("blockReason") or "ไม่ทราบสาเหตุ"
        raise SummaryError(f"Gemini ไม่ได้ส่งข้อความกลับมา ({reason})")
    try:
        answer = json.loads(content)
        summary = answer["summary"].strip()
        priorities = answer["priorities"]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SummaryError("Gemini ตอบมาไม่ตรงรูปแบบที่กำหนด ลองกดสรุปใหม่อีกครั้ง") from None
    if not summary:
        raise SummaryError("Gemini ส่งบทสรุปว่างกลับมา ลองกดสรุปใหม่อีกครั้ง")
    # AI อาจตอบ key ที่ไม่มีในรายการหรือซ้ำกัน ทิ้งไปเฉย ๆ บทสรุปยังใช้ได้ และหน้าเว็บจะเรียงข้อที่เหลือตามลำดับเดิม
    ranking = []
    for item in priorities if isinstance(priorities, list) else []:
        key = item.get("key") if isinstance(item, dict) else None
        if key in options and key not in {ranked["key"] for ranked in ranking}:
            ranking.append({"key": key, "reason": str(item.get("reason") or "").strip()})
    return {"content": summary, "ranking": ranking[:MAX_PRIORITIES]}
