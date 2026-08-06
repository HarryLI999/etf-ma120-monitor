from __future__ import annotations

import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

SCRIPT_VERSION = "intraday-dynamic-ma-v2"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

DATA_DIR = Path("data")
JSON_PATH = DATA_DIR / "latest.json"
CSV_PATH = DATA_DIR / "latest.csv"

KLINE_URLS = [
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
]
QUOTE_URL = "https://qt.gtimg.cn/q="

# 固定顺序：代码、名称、均线周期、区间下限、区间上限、备注。
ETFS = [
    ("512890", "红利低波ETF华泰柏瑞", 130, Decimal("0.98"), Decimal("1.07"), "(0.98~1.07)"),
    ("515080", "中证红利ETF招商", 62, Decimal("0.96"), Decimal("1.09"), "(0.96~1.09) 长"),
    ("515450", "红利低波50ETF南方", 65, Decimal("0.99"), Decimal("1.11"), "(0.99~1.11) 长"),
    ("159758", "红利质量ETF华夏", 108, Decimal("0.93"), Decimal("1.09"), "(0.93~1.09)"),
    ("561580", "央企红利ETF华泰柏瑞", 119, Decimal("0.94"), Decimal("1.11"), "(0.94~1.11)"),
    ("513910", "港股通央企红利ETF华夏", 55, Decimal("0.92"), Decimal("1.09"), "(0.92~1.09)"),
    ("159545", "恒生红利低波ETF易方达", 52, Decimal("0.94"), Decimal("1.09"), "(0.94~1.09) 新"),
    ("159307", "红利低波100ETF博时", 101, Decimal("0.96"), Decimal("1.07"), "(0.96~1.07) 新"),
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


@dataclass(frozen=True)
class Quote:
    price: Decimal
    date: str
    time: str
    raw_time: str


def market_symbol(code: str) -> str:
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"无法转换价格: {value!r}") from exc


def round4(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def round1(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def parse_quote_datetime(raw_time: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", raw_time or "")
    if len(digits) < 8:
        raise RuntimeError(f"腾讯快照时间无法识别: {raw_time!r}")

    quote_date = f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    if len(digits) >= 14:
        quote_time = f"{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
    elif len(digits) >= 12:
        quote_time = f"{digits[8:10]}:{digits[10:12]}"
    else:
        quote_time = ""
    return quote_date, quote_time


def fetch_tencent_quote(symbol: str) -> Quote:
    """获取腾讯最新行情快照；失败时不得回退到日K价格。"""
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            response = requests.get(
                QUOTE_URL + symbol,
                headers=HEADERS,
                timeout=20,
            )
            response.raise_for_status()
            response.encoding = "gbk"
            text = response.text.strip()

            match = re.search(r'="(.*)";', text)
            if not match:
                raise RuntimeError("腾讯快照返回格式无法识别")

            fields = match.group(1).split("~")
            if len(fields) < 31:
                raise RuntimeError(f"腾讯快照字段不足: {len(fields)}")
            if not fields[3]:
                raise RuntimeError("腾讯快照未返回最新价格")

            price = to_decimal(fields[3])
            if price <= 0:
                raise RuntimeError(f"腾讯快照价格无效: {price}")

            raw_time = fields[30] or ""
            quote_date, quote_time = parse_quote_datetime(raw_time)
            return Quote(
                price=price,
                date=quote_date,
                time=quote_time,
                raw_time=raw_time,
            )

        except Exception as exc:
            last_error = exc
            print(
                f"{symbol} 第{attempt}次快照请求失败: "
                f"{type(exc).__name__}: {exc}"
            )
            if attempt < 3:
                time.sleep(attempt * 3)

    raise RuntimeError(f"{symbol} 快照连续3次请求失败: {last_error}")


def fetch_qfq_daily_kline(symbol: str, required_completed_count: int) -> list[KlineRow]:
    """获取腾讯前复权日K线；调用方负责剔除执行日当天未完成K线。"""
    request_count = max(required_completed_count + 60, 220)
    param_value = f"{symbol},day,,,{request_count},qfq"
    last_error: Exception | None = None

    for attempt in range(1, 4):
        for url in KLINE_URLS:
            try:
                response = requests.get(
                    url,
                    params={"param": param_value},
                    headers=HEADERS,
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()

                if payload.get("code") != 0:
                    raise RuntimeError(
                        f"腾讯K线接口返回异常: {payload.get('msg') or payload}"
                    )

                security_data = (payload.get("data") or {}).get(symbol)
                if not security_data:
                    raise RuntimeError(f"腾讯K线接口未返回 {symbol} 的行情")

                rows = security_data.get("qfqday") or security_data.get("day")
                if not rows:
                    raise RuntimeError(f"腾讯K线接口未返回 {symbol} 的日K线")

                parsed: list[KlineRow] = []
                for row in rows:
                    if not isinstance(row, list) or len(row) < 3:
                        continue
                    parsed.append(
                        KlineRow(date=str(row[0]), close=to_decimal(row[2]))
                    )

                parsed.sort(key=lambda item: item.date)
                if not parsed:
                    raise RuntimeError(f"腾讯K线接口返回的 {symbol} 日线无法解析")
                return parsed

            except (
                requests.RequestException,
                ValueError,
                RuntimeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                print(
                    f"{symbol} 第{attempt}次K线请求失败，接口={url}，"
                    f"param={param_value}，原因={exc}"
                )

        if attempt < 3:
            time.sleep(attempt * 5)

    raise RuntimeError(f"{symbol} K线连续3次请求失败: {last_error}")


def empty_result(
    code: str,
    name: str,
    ma_period: int,
    note_text: str,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "ETF代码": code,
        "ETF名称": name,
        "交易日期": None,
        "行情时间": None,
        "历史数据截止日期": None,
        "ETF现价": None,
        "MA周期": ma_period,
        "MA名称": f"MA{ma_period}",
        "MA": None,
        "现价/MA": None,
        "备注": note_text,
        "占比": None,
        "状态": status,
        "说明": reason,
    }


def calculate_one(
    code: str,
    name: str,
    ma_period: int,
    lower: Decimal,
    upper: Decimal,
    note_text: str,
    target_date: str,
) -> dict[str, Any]:
    symbol = market_symbol(code)
    ma_name = f"MA{ma_period}"

    try:
        # 先取一次快照；表格现价与动态MA中加入的当日价格必须是同一数值。
        quote = fetch_tencent_quote(symbol)
        if quote.date != target_date:
            raise RuntimeError(
                f"腾讯快照日期为{quote.date}，北京时间当天为{target_date}；"
                "不得使用上一交易日价格冒充当天盘中价格"
            )

        qfq_rows = fetch_qfq_daily_kline(symbol, ma_period - 1)

        # 无论手动运行发生在盘中还是收盘后，均排除执行日当天日K，
        # 只保留执行日前的完整交易日收盘价。
        completed_rows = [row for row in qfq_rows if row.date < target_date]
        if len(completed_rows) < ma_period - 1:
            raise RuntimeError(
                f"执行日前完整前复权日线仅有{len(completed_rows)}条，"
                f"不足{ma_name}所需的{ma_period - 1}条历史数据"
            )

        history_rows = completed_rows[-(ma_period - 1):]
        history_cutoff = history_rows[-1].date
        history_sum = sum(
            (row.close for row in history_rows),
            Decimal("0"),
        )

        # 动态MA的第N个价格与ETF现价完全一致。
        ma_value = (history_sum + quote.price) / Decimal(ma_period)
        ratio = quote.price / ma_value

        proportion: Decimal | None
        if upper == lower:
            proportion = None
        else:
            proportion = (
                (ratio - lower) / (upper - lower) * Decimal("100")
            )

        quote_time_text = quote.time or quote.raw_time
        return {
            "ETF代码": code,
            "ETF名称": name,
            "交易日期": quote.date,
            "行情时间": quote_time_text,
            "历史数据截止日期": history_cutoff,
            "ETF现价": float(round4(quote.price)),
            "MA周期": ma_period,
            "MA名称": ma_name,
            "MA": float(round4(ma_value)),
            "现价/MA": float(round4(ratio)),
            "备注": note_text,
            "占比": (
                float(round1(proportion))
                if proportion is not None
                else None
            ),
            "状态": "ok",
            "说明": (
                f"{ma_name}=执行日前最近{ma_period - 1}个完整交易日前复权"
                f"收盘价之和，加当天{quote_time_text}腾讯实时快照价"
                f"{round4(quote.price)}后除以{ma_period}；"
                f"历史数据截止{history_cutoff}；盘中数据，非收盘价"
            ),
        }

    except Exception as exc:
        return empty_result(
            code=code,
            name=name,
            ma_period=ma_period,
            note_text=note_text,
            status="error",
            reason=f"{type(exc).__name__}: {exc}",
        )


def write_outputs(results: list[dict[str, Any]], target_date: str) -> None:
    success = [item for item in results if item["状态"] == "ok"]
    failed = [item for item in results if item["状态"] != "ok"]
    generated_at = datetime.now(BEIJING_TZ).isoformat(timespec="seconds")

    quote_times = [
        str(item["行情时间"])
        for item in success
        if item.get("行情时间")
    ]
    history_dates = sorted(
        {
            str(item["历史数据截止日期"])
            for item in success
            if item.get("历史数据截止日期")
        }
    )

    if not history_dates:
        root_history_cutoff: str | None = None
    elif len(history_dates) == 1:
        root_history_cutoff = history_dates[0]
    else:
        root_history_cutoff = ",".join(history_dates)

    payload = {
        "脚本版本": SCRIPT_VERSION,
        "交易日期": target_date,
        "生成时间": generated_at,
        "行情时间范围": (
            f"{min(quote_times)}~{max(quote_times)}"
            if quote_times
            else None
        ),
        "价格性质": "盘中最新行情快照价，非收盘价",
        "历史数据截止日期": root_history_cutoff,
        "数据来源": "腾讯财经实时行情快照；腾讯财经前复权日K线",
        "计算口径": (
            "盘中MAN=(执行日前最近N-1个完整交易日前复权收盘价之和+"
            "执行时同一ETF的当天最新实时快照价)/N；"
            "现价/MA=同一实时快照价/盘中MAN"
        ),
        "占比计算口径": (
            "占比=(现价/MA-区间下限)/(区间上限-区间下限)*100%；"
            "保留1位小数，不限制在0%~100%"
        ),
        "均线配置": {
            code: f"MA{ma_period}"
            for code, _, ma_period, *_ in ETFS
        },
        "成功数量": len(success),
        "失败数量": len(failed),
        "data": results,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fields = [
        "ETF代码",
        "ETF名称",
        "交易日期",
        "行情时间",
        "历史数据截止日期",
        "ETF现价",
        "MA周期",
        "MA名称",
        "MA",
        "现价/MA",
        "备注",
        "占比",
        "状态",
        "说明",
    ]
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    print(
        f"脚本版本={SCRIPT_VERSION}；已写入 {JSON_PATH} 和 {CSV_PATH}，"
        f"成功{len(success)}只，失败{len(failed)}只。"
    )
    for item in results:
        print(item)


def main() -> int:
    now = datetime.now(BEIJING_TZ)
    target_date = now.date().isoformat()
    print(
        f"脚本版本={SCRIPT_VERSION}；北京时间: "
        f"{now.isoformat(timespec='seconds')}，目标日期: {target_date}"
    )

    if now.weekday() >= 5:
        print("今天是周末，不执行盘中行情写入。")
        return 0

    results = [
        calculate_one(
            code,
            name,
            ma_period,
            lower,
            upper,
            note_text,
            target_date,
        )
        for code, name, ma_period, lower, upper, note_text in ETFS
    ]

    write_outputs(results, target_date)
    failed_count = sum(item["状态"] != "ok" for item in results)
    if failed_count:
        print(f"本次有{failed_count}只ETF失败，工作流标记为失败。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
