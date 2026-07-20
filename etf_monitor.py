from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DATA_DIR = Path("data")
JSON_PATH = DATA_DIR / "latest.json"
CSV_PATH = DATA_DIR / "latest.csv"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

ETFS = [
    ("512890", "红利低波ETF华泰柏瑞"),
    ("515450", "红利低波50ETF南方"),
    ("159307", "红利低波100ETF博时"),
    ("515080", "中证红利ETF招商"),
    ("561580", "央企红利ETF华泰柏瑞"),
    ("159758", "红利质量ETF华夏"),
    ("159545", "恒生红利低波ETF易方达"),
    ("513910", "港股通央企红利ETF华夏"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://gu.qq.com/",
    "Accept": "application/json,text/plain,*/*",
}


@dataclass(frozen=True)
class KlineRow:
    date: str
    close: Decimal


def market_symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"无法转换价格: {value!r}") from exc


def fetch_daily_kline(symbol: str, *, qfq: bool, count: int = 160) -> list[KlineRow]:
    suffix = ",qfq" if qfq else ""
    params = {"param": f"{symbol},day,,,{count}{suffix}"}
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            response = requests.get(
                KLINE_URL,
                params=params,
                headers=HEADERS,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()

            if payload.get("code") != 0:
                raise RuntimeError(f"腾讯接口返回异常: {payload.get('msg') or payload}")

            security_data = (payload.get("data") or {}).get(symbol)
            if not security_data:
                raise RuntimeError(f"腾讯接口未返回 {symbol} 的行情")

            rows = (
                security_data.get("qfqday")
                or security_data.get("day")
                or security_data.get("hfqday")
            )
            if not rows:
                raise RuntimeError(f"腾讯接口未返回 {symbol} 的日K线")

            parsed: list[KlineRow] = []
            for row in rows:
                if not isinstance(row, list) or len(row) < 3:
                    continue
                parsed.append(KlineRow(date=str(row[0]), close=to_decimal(row[2])))

            parsed.sort(key=lambda item: item.date)
            if not parsed:
                raise RuntimeError(f"{symbol} 日K线为空")
            return parsed

        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            print(f"{symbol} 第{attempt}次请求失败: {exc}")
            if attempt < 3:
                time.sleep(attempt * 5)

    raise RuntimeError(f"{symbol} 连续3次请求失败: {last_error}")


def round4(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_one(code: str, name: str, target_date: str) -> dict[str, Any]:
    symbol = market_symbol(code)
    try:
        raw_rows = fetch_daily_kline(symbol, qfq=False)
        qfq_rows = fetch_daily_kline(symbol, qfq=True)

        raw_latest = raw_rows[-1]
        qfq_latest = qfq_rows[-1]

        if raw_latest.date != target_date or qfq_latest.date != target_date:
            return {
                "ETF代码": code,
                "ETF名称": name,
                "交易日期": raw_latest.date,
                "ETF现价": None,
                "MA120": None,
                "现价/MA120": None,
                "状态": "not_updated",
                "说明": (
                    f"最新行情日期为{raw_latest.date}，目标日期为{target_date}；"
                    "可能为休市日或盘后数据尚未更新"
                ),
            }

        if len(qfq_rows) < 120:
            raise RuntimeError(f"前复权日线仅有{len(qfq_rows)}条，不足120条")

        current_close = raw_latest.close
        recent_120 = qfq_rows[-120:]
        ma120 = sum((row.close for row in recent_120), Decimal("0")) / Decimal("120")
        ratio = current_close / ma120
        qfq_last_close = qfq_latest.close
        difference = abs(current_close - qfq_last_close)

        note = "腾讯不复权收盘价与前复权序列末日收盘价一致"
        if difference > Decimal("0.001"):
            note = f"两种口径末日价格差{round4(difference)}，现价采用不复权收盘价"

        return {
            "ETF代码": code,
            "ETF名称": name,
            "交易日期": target_date,
            "ETF现价": float(round4(current_close)),
            "MA120": float(round4(ma120)),
            "现价/MA120": float(round4(ratio)),
            "状态": "ok",
            "说明": note,
        }

    except Exception as exc:  # 保留单只ETF错误，不影响其他标的
        return {
            "ETF代码": code,
            "ETF名称": name,
            "交易日期": None,
            "ETF现价": None,
            "MA120": None,
            "现价/MA120": None,
            "状态": "error",
            "说明": f"{type(exc).__name__}: {exc}",
        }


def write_outputs(results: list[dict[str, Any]], target_date: str) -> None:
    success = [item for item in results if item["状态"] == "ok"]
    failed = [item for item in results if item["状态"] != "ok"]
    success.sort(key=lambda item: item["现价/MA120"])
    ordered = success + failed

    generated_at = datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    payload = {
        "交易日期": target_date,
        "生成时间": generated_at,
        "数据来源": "腾讯财经日K线接口",
        "计算口径": "ETF现价采用当日不复权收盘价；MA120采用截至当日最近120个交易日前复权收盘价的算术平均值",
        "成功数量": len(success),
        "失败数量": len(failed),
        "data": ordered,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = ["ETF代码", "ETF名称", "交易日期", "ETF现价", "MA120", "现价/MA120", "状态", "说明"]
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(ordered)

    print(f"已写入 {JSON_PATH} 和 {CSV_PATH}，成功{len(success)}只，失败{len(failed)}只。")
    for item in ordered:
        print(item)


def main() -> int:
    now = datetime.now(BEIJING_TZ)
    target_date = now.date().isoformat()
    print(f"北京时间: {now.isoformat(timespec='seconds')}，目标日期: {target_date}")

    if now.weekday() >= 5:
        print("今天是周末，不执行行情写入。")
        return 0

    results = [calculate_one(code, name, target_date) for code, name in ETFS]
    success_count = sum(item["状态"] == "ok" for item in results)
    error_count = sum(item["状态"] == "error" for item in results)

    if success_count == 0:
        for item in results:
            print(item)
        if error_count > 0:
            print("所有ETF均获取失败，工作流标记为失败，避免出现‘成功但无数据’。")
            return 1
        print(f"所有ETF均无{target_date}行情，视为休市或数据尚未更新，不覆盖旧结果。")
        return 0

    write_outputs(results, target_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
