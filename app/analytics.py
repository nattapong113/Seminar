"""แบบจำลองพยากรณ์และวิเคราะห์ผลกระทบ เขียนด้วย Python ล้วนเพื่อให้ตรวจสอบสูตรได้ทุกบรรทัด

ทุกฟังก์ชันในไฟล์นี้ไม่แตะฐานข้อมูล รับข้อมูลที่ query มาแล้วเป็นลิสต์ธรรมดา ทดสอบแยกได้และไม่ผูกกับ SQL

แนวคิดที่ใช้:
- ข้อมูลนักท่องเที่ยวมีเพียง 48 เดือน (4 ฤดูกาล) และรูปแบบฤดูกาลยังไม่คงที่เพราะเพิ่งฟื้นจากโควิด
  (ปี 2022 เดือนพีคสูงกว่าค่าเฉลี่ยถึง 69% แต่ปี 2025 เหลือ 18%) จึงไม่ยึดวิธีใดวิธีหนึ่งไว้ล่วงหน้า
- ระบบเทียบหลายวิธีตั้งแต่ง่ายสุด Holt-Winters จนถึงโมเดล Machine Learning แล้ว "เลือกวิธีจากปีตรวจสอบ" และ
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

# ---- Machine Learning: ridge regression ที่เรียนตัวคูณของแต่ละเดือนจากฤดูกาลและวันหยุด ----

# ความแรงของ regularization ที่ลอง เลือกค่าที่ทายแถวที่ถูกกันออกได้ดีที่สุด (leave-one-out)
RIDGE_PENALTIES = (0.01, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """แก้ระบบสมการเชิงเส้นด้วย Gauss-Jordan (เมทริกซ์ที่ใช้มีแค่ 5x5)"""
    size = len(vector)
    rows = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        if not rows[column][column]:
            continue
        for row in range(size):
            if row != column:
                factor = rows[row][column] / rows[column][column]
                rows[row] = [value - factor * pivot_value for value, pivot_value in zip(rows[row], rows[column])]
    return [rows[index][size] / rows[index][index] if rows[index][index] else 0.0 for index in range(size)]


# น้ำหนักของข้อมูลลดลงครึ่งหนึ่งทุก 12 เดือนที่ย้อนไป รูปฤดูกาลของปีฟื้นโควิด (2022-2023) แรงผิดปกติและอ่อนลงทุกปี
# ถ้าให้ทุกปีหนักเท่ากัน โมเดลจะทายฤดูกาลแรงเกินจริง ตั้งค่านี้จากเหตุผล ไม่ได้ปรับหาค่าที่ทำให้ปีทดสอบสวย
RECENCY_HALF_LIFE_MONTHS = 12


def ridge_fit(features: list[list[float]], targets: list[float], weights: list[float], penalty: float) -> Callable[[list[float]], float]:
    """ฝึก ridge regression แบบถ่วงน้ำหนัก คืนฟังก์ชันทำนาย ปรับสเกล feature ก่อน เพื่อให้ penalty กดทุกตัวแปรเท่ากัน"""
    width, total = len(features[0]), sum(weights)
    centers = [sum(weight * row[column] for weight, row in zip(weights, features)) / total for column in range(width)]
    scales = [
        math.sqrt(sum(weight * (row[column] - centers[column]) ** 2 for weight, row in zip(weights, features)) / total) or 1.0
        for column in range(width)
    ]
    scaled = [[(row[column] - centers[column]) / scales[column] for column in range(width)] for row in features]
    target_mean = sum(weight * target for weight, target in zip(weights, targets)) / total
    gram = [
        [sum(weight * row[i] * row[j] for weight, row in zip(weights, scaled)) + (penalty if i == j else 0.0) for j in range(width)]
        for i in range(width)
    ]
    solution = _solve(gram, [
        sum(weight * row[i] * (target - target_mean) for weight, row, target in zip(weights, scaled, targets)) for i in range(width)
    ])
    return lambda row: target_mean + sum(
        coefficient * (value - center) / scale for coefficient, value, center, scale in zip(solution, row, centers, scales)
    )


def ridge_fit_cv(features: list[list[float]], targets: list[float], weights: list[float]) -> Callable[[list[float]], float]:
    """เลือก penalty ด้วย leave-one-out แล้วฝึกใหม่ด้วยข้อมูลทั้งหมด ไม่แตะข้อมูลนอกชุดฝึกที่ส่งเข้ามา"""
    best_penalty, best_error = RIDGE_PENALTIES[0], None
    for penalty in RIDGE_PENALTIES:
        error = 0.0
        for held_out in range(len(targets)):
            keep = lambda values: values[:held_out] + values[held_out + 1:]
            model = ridge_fit(keep(features), keep(targets), keep(weights), penalty)
            # แถวที่ใหม่กว่านับความผิดพลาดหนักกว่า ตามน้ำหนักเดียวกับตอนฝึก
            error += weights[held_out] * (model(features[held_out]) - targets[held_out]) ** 2
        if best_error is None or error < best_error:
            best_penalty, best_error = penalty, error
    return ridge_fit(features, targets, weights, best_penalty)


def ml_forecasters(first_month: str, holidays_by_month: dict[str, int]) -> dict[str, tuple[str, Method]]:
    """สร้างวิธีพยากรณ์แบบ ML ที่ผูกกับปฏิทินของอนุกรมนี้ คืนในรูปเดียวกับ METHODS เพื่อเข้าแข่งด้วยกติกาเดียวกัน

    first_month คือป้าย YYYY-MM ของจุดแรกในอนุกรม ต้องรู้เพราะวันหยุดผูกกับเดือนจริงในปฏิทิน ไม่ใช่ตำแหน่งในลิสต์
    โมเดลไม่ได้ทำนายจำนวนคนตรง ๆ แต่ทำนายว่าเดือนนั้นจะต่างจากค่าเฉลี่ย 12 เดือนก่อนหน้ากี่เท่า (ใน log)
    จาก feature ที่รู้ล่วงหน้าได้จริง คือตำแหน่งของเดือนในรอบปี (ฮาร์มอนิก 2 คู่) และจำนวนวันหยุดราชการของเดือนนั้น
    ข้อมูลเดือนที่ใหม่กว่ามีน้ำหนักมากกว่า (ดู RECENCY_HALF_LIFE_MONTHS)
    แล้วคูณกลับด้วยระดับ 12 เดือนล่าสุด จึงไม่วิ่งตามแนวโน้มขาขึ้นของช่วงฟื้นจากโควิดแบบที่ Holt-Winters พลาด
    """
    start = int(first_month[:4]) * MONTHS_IN_YEAR + int(first_month[5:7]) - 1

    def features(position: int) -> list[float]:
        absolute = start + position
        angle = 2 * math.pi * (absolute % MONTHS_IN_YEAR) / MONTHS_IN_YEAR
        label = f"{absolute // MONTHS_IN_YEAR:04d}-{absolute % MONTHS_IN_YEAR + 1:02d}"
        return [math.sin(angle), math.cos(angle), math.sin(2 * angle), math.cos(2 * angle), float(holidays_by_month.get(label, 0))]

    def ridge_forecast(train: Sequence[float], horizon: int) -> list[float]:
        rows = [
            position for position in range(MONTHS_IN_YEAR, len(train))
            if train[position] > 0 and mean(train[position - MONTHS_IN_YEAR:position]) > 0
        ]
        # ต้องมีอย่างน้อยหนึ่งปีไว้เป็นระดับตั้งต้น และอีกหนึ่งปีไว้เรียน ไม่งั้นใช้ระดับล่าสุดตรง ๆ
        if len(rows) < MONTHS_IN_YEAR:
            return flat_mean_forecast(train, horizon)
        # ปรับให้น้ำหนักรวมเท่ากับจำนวนแถว จะได้ไม่เปลี่ยนความหมายของ penalty
        weights = [0.5 ** ((len(train) - 1 - position) / RECENCY_HALF_LIFE_MONTHS) for position in rows]
        weights = [weight * len(weights) / sum(weights) for weight in weights]
        model = ridge_fit_cv(
            [features(position) for position in rows],
            [math.log(train[position] / mean(train[position - MONTHS_IN_YEAR:position])) for position in rows],
            weights,
        )
        level = mean(train[-MONTHS_IN_YEAR:])
        return [level * math.exp(model(features(len(train) + step))) for step in range(horizon)]

    def blend_forecast(train: Sequence[float], horizon: int) -> list[float]:
        return [(learned + flat) / 2 for learned, flat in zip(ridge_forecast(train, horizon), flat_mean_forecast(train, horizon))]

    return {
        "ml_ridge": ("Ridge regression จากฤดูกาลและวันหยุด", ridge_forecast),
        "ml_blend": ("ผสม ML กับค่าเฉลี่ย 12 เดือนล่าสุด", blend_forecast),
    }


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


def compare_methods(series: Sequence[float], test_size: int = MONTHS_IN_YEAR,
                    methods: Optional[dict[str, tuple[str, Method]]] = None) -> list[dict]:
    """ฝึกด้วยข้อมูลก่อนช่วงทดสอบ แล้ววัดว่าแต่ละวิธีทายช่วงทดสอบได้คลาดเคลื่อนกี่เปอร์เซ็นต์"""
    train, test = list(series[:-test_size]), list(series[-test_size:])
    results = []
    for key, (label, method) in (methods or METHODS).items():
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


def rolling_ranking(series: Sequence[float], methods: dict[str, tuple[str, Method]],
                    window: int = MONTHS_IN_YEAR) -> list[dict]:
    """วัดแต่ละวิธีโดยเลื่อนจุดตัดไปทีละเดือนตลอด window เดือนสุดท้าย แต่ละจุดตัดทายทุกเดือนที่เหลือจนสิ้นช่วง

    ได้ 78 การทายต่อวิธี (12 + 11 + ... + 1) แทน 12 การทายของการตัดครั้งเดียวใน compare_methods
    ใกล้การใช้งานจริงที่มีข้อมูลเข้าทุกเดือนมากกว่า จึงรายงานคู่กับการตัดครั้งเดียว

    ไม่ได้ใช้เลือกวิธี: เคยทดลองใช้แล้ววิธีที่ชนะปีตรวจสอบแบบนี้ ("เดือนเดียวกันปีก่อน") กลับแพ้เกือบทุกวิธีในปีทดสอบ
    และให้ช่วงประมาณกว้างจนใช้ไม่ได้ การทายทั้ง 78 ครั้งมาจากปีเดียวกัน ความผิดพลาดจึงไปทางเดียวกัน
    ไม่ได้เป็นหลักฐานอิสระ 78 ชิ้น ปัญหาจริงคือมีปีตรวจสอบปีเดียว ซึ่งการวัดถี่ขึ้นแก้ไม่ได้
    """
    results = []
    for key, (label, method) in methods.items():
        errors = []
        for origin in range(len(series) - window, len(series)):
            prediction = method(list(series[:origin]), len(series) - origin)
            errors.extend(abs(actual - value) / actual for actual, value in zip(series[origin:], prediction) if actual)
        results.append({"method": key, "label": label, "mape_percent": round(100 * mean(errors), 2) if errors else None})
    return sorted(results, key=lambda item: item["mape_percent"] if item["mape_percent"] is not None else 999)


def forecast_series(series: Sequence[float], horizon: int,
                    extra_methods: Optional[dict[str, tuple[str, Method]]] = None) -> dict:
    """เลือกวิธีพยากรณ์จากปีตรวจสอบ รายงานความแม่นจากปีทดสอบ แล้วพยากรณ์อนาคตด้วยข้อมูลทั้งหมด

    extra_methods คือวิธีที่ต้องรู้ปฏิทินของอนุกรม (ดู ml_forecasters) เข้าแข่งด้วยกติกาเดียวกับวิธีใน METHODS ทุกประการ
    "alternative" คือวิธีที่ดีที่สุดของอีกกลุ่มหนึ่ง (ถ้าวิธีหลักเป็น ML จะเป็นวิธีสถิติที่ดีที่สุด และกลับกัน)
    พร้อมผลพยากรณ์ของมัน ใช้แสดงเทียบกับวิธีหลักให้เห็นว่าสองแนวทางทายต่างกันแค่ไหน
    """
    if len(series) < MINIMUM_MONTHS:
        return {"error": f"ต้องมีข้อมูลอย่างน้อย {MINIMUM_MONTHS} เดือน แต่มี {len(series)} เดือน"}
    methods = {**METHODS, **(extra_methods or {})}
    without_test = series[:-MONTHS_IN_YEAR]

    # ปีตรวจสอบ = 12 เดือนก่อนปีทดสอบ ใช้เลือกวิธีเท่านั้น ไม่แตะปีทดสอบ
    validation = compare_methods(without_test, methods=methods)
    chosen_key = validation[0]["method"]
    label, method = methods[chosen_key]

    # ปีทดสอบ = 12 เดือนสุดท้าย ไม่ถูกใช้เลือกวิธี ตัวเลขนี้จึงเป็นความแม่นที่รายงานได้จริง
    # รายงานสองแบบ: ทายทั้งปีล่วงหน้าจากสิ้นปีก่อน (ยากสุด) และเลื่อนจุดตัดทีละเดือน (ใกล้การใช้งานจริงที่มีข้อมูลเข้าทุกเดือน)
    test = compare_methods(series, methods=methods)
    test_rolling = rolling_ranking(series, methods)
    score = lambda ranking, key: next(item["mape_percent"] for item in ranking if item["method"] == key)
    validation_rolling = rolling_ranking(without_test, methods)
    comparison = [
        {
            "method": item["method"], "label": item["label"],
            "is_chosen": item["method"] == chosen_key, "is_ml": item["method"].startswith("ml_"),
            "validation_mape_percent": item["mape_percent"],
            "validation_rolling_mape_percent": score(validation_rolling, item["method"]),
            "test_mape_percent": score(test, item["method"]),
            "test_rolling_mape_percent": score(test_rolling, item["method"]),
        }
        for item in validation
    ]

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

    uses_ml = chosen_key.startswith("ml_")
    alternative = next((item for item in comparison if item["is_ml"] != uses_ml), None)
    return {
        "points": points,
        "model": {
            "method": chosen_key, "label": label,
            "selected_by": "ความแม่นในปีตรวจสอบ (ไม่ใช้ปีทดสอบ)",
            "uses_ml": uses_ml,
        },
        "alternative": {
            **alternative,
            "points": [{"step": step, "visitors": round(value)}
                       for step, value in enumerate(methods[alternative["method"]][1](list(series), horizon), start=1)],
        } if alternative else None,
        "comparison": comparison,
        "accuracy": {
            "validation_year_ranking": validation,
            "test_year_ranking": test,
            "test_mape_percent": score(test, chosen_key),
            "test_rolling_mape_percent": score(test_rolling, chosen_key),
            "seasonal_naive_mape_percent": score(test, "seasonal_naive"),
            "beats_baseline": bool(
                score(test, chosen_key) is not None and score(test, "seasonal_naive") is not None
                and score(test, chosen_key) <= score(test, "seasonal_naive")
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


def _impact(values: Sequence[float], driver: Sequence[float], unit_scale: float, sample_size: Optional[int] = None) -> dict:
    r = correlation(driver, values)
    slope, _ = linear_fit(driver, values)
    interval = correlation_interval(r, sample_size or len(values)) if r is not None else None
    return {
        "months_used": len(values),
        "correlation": round(r, 3) if r is not None else None,
        "correlation_ci_95": [round(interval[0], 3), round(interval[1], 3)] if interval else None,
        # ช่วงความเชื่อมั่นไม่คร่อม 0 = ความสัมพันธ์ชัดพอที่จะไม่ใช่ความบังเอิญ
        "significant": bool(interval and (interval[0] > 0 or interval[1] < 0)),
        "effect_percent": round(slope * unit_scale * 100, 2),
    }


def monthly_anomaly(values: Sequence[float], months: Sequence[str]) -> list[float]:
    """ส่วนต่างของแต่ละเดือนจากค่าปกติของเดือนเดียวกันในปฏิทิน (ค่าเฉลี่ยของเดือนนั้นทุกปีที่มี)"""
    # จับกลุ่มตามเดือนในปฏิทินจากป้าย YYYY-MM ไม่ใช่ตำแหน่งในลิสต์ เผื่อข้อมูลบางเดือนขาดหาย
    by_month: dict[str, list[float]] = {}
    for month, value in zip(months, values):
        by_month.setdefault(month[5:7], []).append(value)
    normal = {month: mean(group) for month, group in by_month.items()}
    return [value - normal[month[5:7]] for month, value in zip(months, values)]


def rain_impact(visitor_index: Sequence[float], monthly_rain_mm: Sequence[float], months: Sequence[str]) -> dict:
    """ผลของฝนที่มากกว่าปกติของเดือนนั้น ต่อจำนวนคนหลังตัดฤดูกาลและแนวโน้มออกแล้ว"""
    anomaly = monthly_anomaly(monthly_rain_mm, months)

    result = _impact(list(visitor_index), anomaly, unit_scale=100)
    result["effect_unit"] = "% ต่อฝนที่มากกว่าปกติของเดือนนั้น 100 มม."
    result["monthly"] = [
        {"month": month, "rain_mm": round(rain, 1), "rain_anomaly_mm": round(value, 1), "visitor_index": round(index, 3)}
        for month, rain, value, index in zip(months, monthly_rain_mm, anomaly, visitor_index)
    ]
    return result


def without_year_level(values: Sequence[float], months: Sequence[str]) -> list[float]:
    """หักค่าเฉลี่ยของแต่ละปีออก เหลือเฉพาะการขึ้นลงภายในปีเดียวกัน"""
    by_year: dict[str, list[float]] = {}
    for month, value in zip(months, values):
        by_year.setdefault(month[:4], []).append(value)
    level = {year: mean(group) for year, group in by_year.items()}
    return [value - level[month[:4]] for month, value in zip(months, values)]


def pm25_impact(visitor_index: Sequence[float], monthly_pm25: Sequence[float], months: Sequence[str]) -> dict:
    """ผลของฝุ่น PM2.5 ที่สูงกว่าปกติของเดือนนั้น ต่อจำนวนคนหลังตัดฤดูกาลและแนวโน้มออกแล้ว

    ต่างจากฝนตรงที่ต้องหักระดับของทั้งปีออกด้วย PM2.5 มีข้อมูลแค่สองปี และปีหนึ่งฝุ่นสูงกว่าอีกปีทุกเดือน
    ถ้าไม่หัก ความต่างระหว่างปี (ซึ่งมีได้ร้อยสาเหตุ) จะถูกนับเป็นผลของฝุ่น ทดลองกับข้อมูลจริงแล้วได้ r = 0.69
    ที่ดูชัดเจน แต่พอหักระดับรายปีออกเหลือ 0.50 และช่วงความเชื่อมั่นคร่อมศูนย์
    """
    pm25 = without_year_level(monthly_anomaly(monthly_pm25, months), months)
    visitors = without_year_level(monthly_anomaly(visitor_index, months), months)
    # ค่าปกติรายเดือนและระดับรายปีประมาณจากข้อมูลชุดเดียวกัน จุดที่เป็นอิสระต่อกันจริงจึงน้อยกว่าจำนวนเดือน
    # ใช้จำนวนนี้คำนวณช่วงความเชื่อมั่น ไม่งั้นช่วงจะแคบเกินจริง
    independent = len(months) - len({month[5:7] for month in months}) - len({month[:4] for month in months}) + 1
    result = _impact(visitors, pm25, unit_scale=10, sample_size=independent)
    result["independent_points"] = independent
    result["effect_unit"] = "% ต่อ PM2.5 ที่สูงกว่าปกติของเดือนนั้น 10 มคก./ลบ.ม."
    result["monthly"] = [
        {"month": month, "pm25": round(value, 2), "pm25_anomaly": round(delta, 2), "visitor_index": round(index, 3)}
        for month, value, delta, index in zip(months, monthly_pm25, pm25, visitor_index)
    ]
    return result


def tourism_link(business_index: Sequence[float], visitor_index: Sequence[float]) -> dict:
    """ความสัมพันธ์ระหว่างรายได้ของธุรกิจกับจำนวนนักท่องเที่ยวของเมือง ทั้งสองฝั่งตัดฤดูกาลและแนวโน้มออกแล้ว

    ถ้าไม่ตัดออกก่อน สองอนุกรมที่มีไฮซีซันตรงกันจะดูสัมพันธ์กันเสมอ แม้ลูกค้าจะเป็นคนละกลุ่ม
    """
    result = _impact(list(business_index), list(visitor_index), unit_scale=0.1)
    result["effect_unit"] = "% ต่อนักท่องเที่ยวที่มากกว่าที่คาด 10%"
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
