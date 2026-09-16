"""แบบจำลองพยากรณ์และวิเคราะห์ผลกระทบ เขียนด้วย Python ล้วนเพื่อให้ตรวจสอบสูตรได้ทุกบรรทัด

ทุกฟังก์ชันในไฟล์นี้ไม่แตะฐานข้อมูล รับข้อมูลที่ query มาแล้วเป็นลิสต์ธรรมดา ทดสอบแยกได้และไม่ผูกกับ SQL

แนวคิดที่ใช้:
- ข้อมูลนักท่องเที่ยวมีเพียง 48 เดือน (4 ฤดูกาล) และรูปแบบฤดูกาลยังไม่คงที่เพราะเพิ่งฟื้นจากโควิด
  (ปี 2022 เดือนพีคสูงกว่าค่าเฉลี่ยถึง 69% แต่ปี 2025 เหลือ 18%) จึงไม่ยึดวิธีใดวิธีหนึ่งไว้ล่วงหน้า
- ระบบเทียบหลายวิธีตั้งแต่ง่ายสุดจนถึง Holt-Winters แล้ว "เลือกวิธีจากปีตรวจสอบ" และ
  "รายงานความแม่นจากปีทดสอบที่ไม่ถูกใช้เลือกวิธี" เพื่อไม่ให้ตัวเลขความแม่นสวยเกินจริง
- การวิเคราะห์ผลกระทบเป็นความสัมพันธ์ (association) ไม่ใช่เหตุและผล เพราะฝนกับโลว์ซีซันเกิดพร้อมกัน
  จึงตัดฤดูกาลและแนวโน้มออกก่อนเสมอ
"""
from __future__ import annotations

import math
from typing import Callable, Optional, Sequence

MONTHS_IN_YEAR = 12
Z_95 = 1.959964

# ความแรงของฤดูกาลที่ใช้ในวิธี damped_seasonal: 0 = ไม่ใส่ฤดูกาลเลย, 1 = ใส่เต็มตามที่วัดได้
SEASONAL_DAMPING = 0.5

# ค่าพารามิเตอร์ Holt-Winters ที่ค้นหา ระดับนี้ใช้เวลาไม่ถึงวินาทีบนอนุกรม 48 เดือน
ALPHA_GRID = [round(0.05 * step, 2) for step in range(1, 19)]  # 0.05-0.90 ระดับ
BETA_GRID = [0.0, 0.05, 0.1, 0.2, 0.3, 0.4]                    # แนวโน้ม
GAMMA_GRID = [0.0, 0.05, 0.1, 0.2, 0.3, 0.4]                   # ฤดูกาล
PHI_GRID = [0.80, 0.90, 0.95, 1.0]                             # ตัวหน่วงแนวโน้ม กันพยากรณ์ไกล ๆ พุ่งเกินจริง


# ---------------------------------------------------------------- สถิติพื้นฐาน


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def standard_deviation(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / (len(values) - 1))


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """คืน (ความชัน, จุดตัดแกน) ด้วยวิธีกำลังสองน้อยสุด"""
    x_mean, y_mean = mean(xs), mean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return 0.0, y_mean
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    return slope, y_mean - slope * x_mean


def correlation(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) < 3:
        return None
    x_mean, y_mean = mean(xs), mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return numerator / denominator if denominator else None


def correlation_interval(r: float, n: int) -> Optional[tuple[float, float]]:
    """ช่วงความเชื่อมั่น 95% ของสหสัมพันธ์ ด้วยการแปลง Fisher z (ไม่ต้องใช้ตาราง t)"""
    if n < 4 or abs(r) >= 1:
        return None
    z = 0.5 * math.log((1 + r) / (1 - r))
    margin = Z_95 / math.sqrt(n - 3)
    return (math.tanh(z - margin), math.tanh(z + margin))


def mape(actual: Sequence[float], predicted: Sequence[float]) -> Optional[float]:
    """ค่าคลาดเคลื่อนสัมบูรณ์เฉลี่ยเป็นเปอร์เซ็นต์ ยิ่งต่ำยิ่งแม่น"""
    pairs = [(a, p) for a, p in zip(actual, predicted) if a]
    if not pairs:
        return None
    return 100 * mean([abs(a - p) / a for a, p in pairs])


# ---------------------------------------------------------------- ฤดูกาล


def seasonal_indices(series: Sequence[float]) -> list[float]:
    """ดัชนีฤดูกาลรายเดือนด้วยวิธี ratio-to-moving-average (ค่าเฉลี่ยของดัชนีเท่ากับ 1)"""
    half = MONTHS_IN_YEAR // 2
    ratios: dict[int, list[float]] = {month: [] for month in range(MONTHS_IN_YEAR)}
    for index in range(half, len(series) - half):
        window = series[index - half:index + half + 1]
        # ค่าเฉลี่ยเคลื่อนที่แบบกึ่งกลาง 12 เดือน ให้น้ำหนักปลายสองข้างครึ่งเดียว
        centered = (sum(window[1:-1]) + (window[0] + window[-1]) / 2) / MONTHS_IN_YEAR
        if centered:
            ratios[index % MONTHS_IN_YEAR].append(series[index] / centered)
    indices = [mean(ratios[month]) if ratios[month] else 1.0 for month in range(MONTHS_IN_YEAR)]
    average = mean(indices)
    return [index / average for index in indices] if average else indices


# ---------------------------------------------------------------- วิธีพยากรณ์


def flat_mean_forecast(train: Sequence[float], horizon: int) -> list[float]:
    """ค่าเฉลี่ย 12 เดือนล่าสุด ไม่ใส่ฤดูกาลและแนวโน้มเลย"""
    return [mean(train[-MONTHS_IN_YEAR:])] * horizon


def _level_times_season(train: Sequence[float], horizon: int, damping: float) -> list[float]:
    level = mean(train[-MONTHS_IN_YEAR:])
    indices = seasonal_indices(train)
    return [
        level * (1 + damping * (indices[(len(train) + step - 1) % MONTHS_IN_YEAR] - 1))
        for step in range(1, horizon + 1)
    ]


def damped_seasonal_forecast(train: Sequence[float], horizon: int) -> list[float]:
    """ระดับล่าสุดคูณฤดูกาลแบบลดความแรงลงครึ่งหนึ่ง เผื่อว่ารูปฤดูกาลกำลังเปลี่ยน"""
    return _level_times_season(train, horizon, SEASONAL_DAMPING)


def seasonal_forecast(train: Sequence[float], horizon: int) -> list[float]:
    """ระดับล่าสุดคูณฤดูกาลเต็มแรงตามที่วัดได้จากข้อมูล"""
    return _level_times_season(train, horizon, 1.0)


def seasonal_naive_forecast(train: Sequence[float], horizon: int) -> list[float]:
    """ทายว่าเท่ากับเดือนเดียวกันของปีก่อน เป็นเกณฑ์มาตรฐานของการพยากรณ์ตามฤดูกาล"""
    return [train[len(train) - MONTHS_IN_YEAR + (step - 1) % MONTHS_IN_YEAR] for step in range(1, horizon + 1)]


def same_month_mean_forecast(train: Sequence[float], horizon: int) -> list[float]:
    """ค่าเฉลี่ยของเดือนเดียวกันจากทุกปีที่มี แล้วปรับด้วยระดับล่าสุด"""
    adjustment = mean(train[-MONTHS_IN_YEAR:]) / mean(train) if mean(train) else 1.0
    forecast = []
    for step in range(1, horizon + 1):
        month = (len(train) + step - 1) % MONTHS_IN_YEAR
        forecast.append(mean(train[month::MONTHS_IN_YEAR]) * adjustment)
    return forecast


# ---- Holt-Winters (ฤดูกาลแบบคูณ + แนวโน้มแบบหน่วง) ----


def _initial_state(series: Sequence[float]) -> tuple[float, float, list[float]]:
    """ตั้งค่าเริ่มต้นจากสองฤดูกาลแรก: ระดับ แนวโน้ม และดัชนีฤดูกาลรายเดือน"""
    seasons = len(series) // MONTHS_IN_YEAR
    first_season = series[:MONTHS_IN_YEAR]
    level = mean(first_season)
    trend = 0.0
    if seasons >= 2:
        trend = (mean(series[MONTHS_IN_YEAR:2 * MONTHS_IN_YEAR]) - mean(first_season)) / MONTHS_IN_YEAR

    seasonal: list[float] = []
    for month in range(MONTHS_IN_YEAR):
        ratios = []
        for season in range(min(seasons, 2)):
            window = series[season * MONTHS_IN_YEAR:(season + 1) * MONTHS_IN_YEAR]
            window_mean = mean(window)
            if window_mean:
                ratios.append(window[month] / window_mean)
        seasonal.append(mean(ratios) if ratios else 1.0)
    return level, trend, seasonal


def _damped_sum(phi: float, horizon: int) -> float:
    """ผลรวม phi + phi^2 + ... + phi^h ใช้หน่วงแนวโน้มไม่ให้พยากรณ์ไกล ๆ พุ่งเกินจริง"""
    if phi == 1.0:
        return float(horizon)
    return phi * (1 - phi ** horizon) / (1 - phi)


def holt_winters_fit(series: Sequence[float], alpha: float, beta: float, gamma: float, phi: float) -> dict:
    level, trend, seasonal = _initial_state(series)
    fitted: list[float] = []
    for index, value in enumerate(series):
        season_index = seasonal[index % MONTHS_IN_YEAR]
        fitted.append((level + phi * trend) * season_index)
        previous_level = level
        level = alpha * (value / season_index if season_index else value) + (1 - alpha) * (level + phi * trend)
        trend = beta * (level - previous_level) + (1 - beta) * phi * trend
        if level:
            seasonal[index % MONTHS_IN_YEAR] = gamma * (value / level) + (1 - gamma) * season_index
    return {"level": level, "trend": trend, "seasonal": seasonal, "fitted": fitted, "length": len(series)}


def holt_winters_parameters(train: Sequence[float]) -> dict:
    """เลือกพารามิเตอร์ที่ทำนายย้อนหลังในชุดที่ให้มาได้แม่นที่สุด (ผู้เรียกต้องไม่ส่งชุดทดสอบเข้ามา)"""
    best = None
    for alpha in ALPHA_GRID:
        for beta in BETA_GRID:
            for gamma in GAMMA_GRID:
                for phi in PHI_GRID:
                    state = holt_winters_fit(train, alpha, beta, gamma, phi)
                    # ข้ามฤดูกาลแรกเพราะแบบจำลองยังตั้งตัวไม่เข้าที่
                    error = mape(train[MONTHS_IN_YEAR:], state["fitted"][MONTHS_IN_YEAR:])
                    if error is not None and (best is None or error < best["in_sample_mape_percent"]):
                        best = {"alpha": alpha, "beta": beta, "gamma": gamma, "phi": phi,
                                "in_sample_mape_percent": round(error, 2)}
    return best or {"alpha": 0.3, "beta": 0.1, "gamma": 0.1, "phi": 0.95, "in_sample_mape_percent": None}


def holt_winters_forecast(train: Sequence[float], horizon: int) -> list[float]:
    parameters = holt_winters_parameters(train)
    state = holt_winters_fit(train, parameters["alpha"], parameters["beta"], parameters["gamma"], parameters["phi"])
    return [
        (state["level"] + _damped_sum(parameters["phi"], step) * state["trend"])
        * state["seasonal"][(state["length"] + step - 1) % MONTHS_IN_YEAR]
        for step in range(1, horizon + 1)
    ]


Method = Callable[[Sequence[float], int], list[float]]

METHODS: dict[str, tuple[str, Method]] = {
    "flat_mean": ("ค่าเฉลี่ย 12 เดือนล่าสุด", flat_mean_forecast),
    "damped_seasonal": ("ระดับล่าสุด x ฤดูกาลครึ่งแรง", damped_seasonal_forecast),
    "seasonal": ("ระดับล่าสุด x ฤดูกาลเต็ม", seasonal_forecast),
    "seasonal_naive": ("เดือนเดียวกันปีก่อน", seasonal_naive_forecast),
    "same_month_mean": ("เฉลี่ยเดือนเดียวกันทุกปี ปรับระดับ", same_month_mean_forecast),
    "holt_winters": ("Holt-Winters (ฤดูกาลคูณ แนวโน้มหน่วง)", holt_winters_forecast),
}

# ต้องมีอย่างน้อย 2 ฤดูกาลไว้ฝึก + 1 ปีตรวจสอบ + 1 ปีทดสอบ
MINIMUM_MONTHS = 4 * MONTHS_IN_YEAR


def compare_methods(series: Sequence[float], test_size: int = MONTHS_IN_YEAR) -> list[dict]:
    """ฝึกด้วยข้อมูลก่อนช่วงทดสอบ แล้ววัดว่าแต่ละวิธีทายช่วงทดสอบได้คลาดเคลื่อนกี่เปอร์เซ็นต์"""
    train, test = list(series[:-test_size]), list(series[-test_size:])
    results = []
    for key, (label, method) in METHODS.items():
        error = mape(test, method(train, test_size))
        results.append({"method": key, "label": label, "mape_percent": round(error, 2) if error is not None else None})
    return sorted(results, key=lambda item: item["mape_percent"] if item["mape_percent"] is not None else 999)


def _relative_errors(series: Sequence[float], method: Method, window: int) -> list[float]:
    """ความคลาดเคลื่อนของการทายล่วงหน้า 1 เดือน โดยเลื่อนจุดตัดไปทีละเดือน ใช้คำนวณช่วงประมาณ"""
    errors = []
    for cutoff in range(len(series) - window, len(series)):
        prediction = method(list(series[:cutoff]), 1)[0]
        if series[cutoff]:
            errors.append((series[cutoff] - prediction) / series[cutoff])
    return errors


def forecast_series(series: Sequence[float], horizon: int) -> dict:
    """เลือกวิธีพยากรณ์จากปีตรวจสอบ รายงานความแม่นจากปีทดสอบ แล้วพยากรณ์อนาคตด้วยข้อมูลทั้งหมด"""
    if len(series) < MINIMUM_MONTHS:
        return {"error": f"ต้องมีข้อมูลอย่างน้อย {MINIMUM_MONTHS} เดือน แต่มี {len(series)} เดือน"}

    # ปีตรวจสอบ = 12 เดือนก่อนปีทดสอบ ใช้เลือกวิธีเท่านั้น ไม่แตะปีทดสอบ
    validation = compare_methods(series[:-MONTHS_IN_YEAR])
    chosen_key = validation[0]["method"]
    label, method = METHODS[chosen_key]

    # ปีทดสอบ = 12 เดือนสุดท้าย ไม่ถูกใช้เลือกวิธี ตัวเลขนี้จึงเป็นความแม่นที่รายงานได้จริง
    test = compare_methods(series)
    chosen_test = next(item for item in test if item["method"] == chosen_key)
    baseline_test = next(item for item in test if item["method"] == "seasonal_naive")

    errors = _relative_errors(series, method, MONTHS_IN_YEAR)
    relative_sd = standard_deviation(errors) if len(errors) > 1 else None
    values = method(list(series), horizon)
    points = []
    for step, value in enumerate(values, start=1):
        # ยิ่งพยากรณ์ไกลยิ่งไม่แน่นอน จึงขยายช่วงตาม sqrt(h)
        margin = Z_95 * relative_sd * math.sqrt(step) * value if relative_sd else None
        points.append({
            "step": step,
            "visitors": round(value),
            "lower": max(0, round(value - margin)) if margin else None,
            "upper": round(value + margin) if margin else None,
        })

    return {
        "points": points,
        "model": {"method": chosen_key, "label": label, "selected_by": "ความแม่นในปีตรวจสอบ (ไม่ใช้ปีทดสอบ)"},
        "accuracy": {
            "validation_year_ranking": validation,
            "test_year_ranking": test,
            "test_mape_percent": chosen_test["mape_percent"],
            "seasonal_naive_mape_percent": baseline_test["mape_percent"],
            "beats_baseline": bool(
                chosen_test["mape_percent"] is not None
                and baseline_test["mape_percent"] is not None
                and chosen_test["mape_percent"] <= baseline_test["mape_percent"]
            ),
            "one_step_mape_percent": round(100 * mean([abs(error) for error in errors]), 2) if errors else None,
            "interval_note": "ช่วงประมาณ = พยากรณ์ ± 1.96 x ส่วนเบี่ยงเบนของความคลาดเคลื่อนสัมพัทธ์ x sqrt(เดือนข้างหน้า)",
        },
    }


# ---------------------------------------------------------------- วิเคราะห์ผลกระทบ


def deseasonalized_index(series: Sequence[float]) -> list[float]:
    """สัดส่วนของค่าจริงต่อค่าที่ควรเป็นตามฤดูกาลและแนวโน้ม (1.0 = ตรงตามที่คาด)

    ใช้เป็นตัวแปรตามในการวิเคราะห์ผลกระทบ เพื่อไม่ให้ "ฝนตกช่วงโลว์ซีซัน" ถูกนับซ้ำเป็นผลของฝน
    """
    indices = seasonal_indices(series)
    adjusted = [value / indices[position % MONTHS_IN_YEAR] for position, value in enumerate(series)]
    slope, intercept = linear_fit(list(range(len(adjusted))), adjusted)
    expected = [slope * position + intercept for position in range(len(adjusted))]
    return [value / trend if trend else 1.0 for value, trend in zip(adjusted, expected)]


def _impact(values: Sequence[float], driver: Sequence[float], unit_scale: float) -> dict:
    r = correlation(driver, values)
    slope, _ = linear_fit(driver, values)
    interval = correlation_interval(r, len(values)) if r is not None else None
    return {
        "months_used": len(values),
        "correlation": round(r, 3) if r is not None else None,
        "correlation_ci_95": [round(interval[0], 3), round(interval[1], 3)] if interval else None,
        # ช่วงความเชื่อมั่นไม่คร่อม 0 = ความสัมพันธ์ชัดพอที่จะไม่ใช่ความบังเอิญ
        "significant": bool(interval and (interval[0] > 0 or interval[1] < 0)),
        "effect_percent": round(slope * unit_scale * 100, 2),
    }


def rain_impact(visitor_index: Sequence[float], monthly_rain_mm: Sequence[float], months: Sequence[str]) -> dict:
    """ผลของฝนที่มากกว่าปกติของเดือนนั้น ต่อจำนวนคนหลังตัดฤดูกาลและแนวโน้มออกแล้ว"""
    # จับกลุ่มตามเดือนในปฏิทินจากป้าย YYYY-MM ไม่ใช่ตำแหน่งในลิสต์ เผื่อข้อมูลบางเดือนขาดหาย
    by_month: dict[str, list[float]] = {}
    for month, rain in zip(months, monthly_rain_mm):
        by_month.setdefault(month[5:7], []).append(rain)
    normal = {month: mean(values) for month, values in by_month.items()}
    anomaly = [rain - normal[month[5:7]] for month, rain in zip(months, monthly_rain_mm)]

    result = _impact(list(visitor_index), anomaly, unit_scale=100)
    result["effect_unit"] = "% ต่อฝนที่มากกว่าปกติของเดือนนั้น 100 มม."
    result["monthly"] = [
        {"month": month, "rain_mm": round(rain, 1), "rain_anomaly_mm": round(value, 1), "visitor_index": round(index, 3)}
        for month, rain, value, index in zip(months, monthly_rain_mm, anomaly, visitor_index)
    ]
    return result


def holiday_impact(visitor_index: Sequence[float], holiday_days: Sequence[int], months: Sequence[str]) -> dict:
    """ผลของจำนวนวันหยุดในเดือน ต่อจำนวนคนหลังตัดฤดูกาลและแนวโน้มออกแล้ว"""
    result = _impact(list(visitor_index), [float(days) for days in holiday_days], unit_scale=1)
    result["effect_unit"] = "% ต่อวันหยุดราชการที่เพิ่มขึ้น 1 วัน"

    many = [index for index, days in zip(visitor_index, holiday_days) if days >= 2]
    few = [index for index, days in zip(visitor_index, holiday_days) if days <= 1]
    if many and few:
        result["comparison"] = {
            "months_with_2plus_holidays": len(many),
            "months_with_0_or_1_holiday": len(few),
            "difference_percent": round((mean(many) / mean(few) - 1) * 100, 2),
        }
    result["monthly"] = [
        {"month": month, "holiday_days": days, "visitor_index": round(index, 3)}
        for month, days, index in zip(months, holiday_days, visitor_index)
    ]
    return result
