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

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DATA_DIR = Path("data")
JSON_PATH = DATA_DIR / "latest.json"
CSV_PATH = DATA_DIR / "latest.csv"

KLINE_URLS = [
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
]
QUOTE_URL = "https://qt.gtimg.cn/q="

# 固定汇报顺序及每只ETF对应的均线周期。
ETFS = [
    ("512890", "红利低波ETF华泰柏瑞", 130),
    ("515080", "中证红利ETF招商", 62),
    ("515450", "红利低波50ETF南方", 65),
    ("159758", "红利质量ETF华夏", 108),
    ("561580", "央企红利ETF华泰柏瑞", 119),
    ("513910", "港股通央企红利ETF华夏", 55),
    ("159545", "恒生红利低波ETF易方达", 52),
    ("159307", "红利低波100ETF博时", 101),
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


def round4(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def fetch_qfq_daily_kline(symbol: str, required_count: int) -> list[KlineRow]:
    """获取腾讯前复权日K线，并确保数据数量满足指定均线周期。"""
    request_count = max(required_count + 40, 180)
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
                if len(parsed) < required_count:
                    raise RuntimeError(
                        f"{symbol} 前复权日线仅有{len(parsed)}条，"
                        f"不足MA{required_count}所需的{required_count}条"
                    )
                return parsed

            except (
                requests.RequestException,
                ValueError,
                RuntimeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                print(
                    f"{symbol} 第{attempt}次请求失败，接口={url}，"
                    f"param={param_value}，原因={exc}"
                )

        if attempt < 3:
            time.sleep(attempt * 5)

    raise RuntimeError(f"{symbol} 连续3次请求失败: {last_error}")


def fetch_tencent_quote(symbol: str) -> tuple[Decimal | None, str | None, str]:
    """读取腾讯实时快照；快照失败时允许使用同日K线收盘价。"""
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

        price = to_decimal(fields[3]) if fields[3] else None
        raw_time = fields[30] if fields[30] else ""
        quote_date = raw_time[:8]
        if len(quote_date) == 8 and quote_date.isdigit():
            quote_date = f"{quote_date[:4]}-{quote_date[4:6]}-{quote_date[6:8]}"
        else:
            quote_date = None

        return price, quote_date, "腾讯财经实时快照"
    except Exception as exc:
        return None, None, f"腾讯快照获取失败: {type(exc).__name__}: {exc}"


def empty_result(
    code: str,
    name: str,
    ma_period: int,
    trade_date: str | None,
    status: str,
    note: str,
) -> dict[str, Any]:
    return {
        "ETF代码": code,
        "ETF名称": name,
        "交易日期": trade_date,
        "ETF现价": None,
        "MA周期": ma_period,
        "MA名称": f"MA{ma_period}",
        "MA": None,
        "现价/MA": None,
        "状态": status,
        "说明": note,
    }


def calculate_one(
    code: str,
    name: str,
    ma_period: int,
    target_date: str,
) -> dict[str, Any]:
    symbol = market_symbol(code)
    ma_name = f"MA{ma_period}"

    try:
        qfq_rows = fetch_qfq_daily_kline(symbol, ma_period)
        latest = qfq_rows[-1]

        if latest.date != target_date:
            return empty_result(
                code,
                name,
                ma_period,
                latest.date,
                "not_updated",
                (
                    f"最新K线日期为{latest.date}，目标日期为{target_date}；"
                    "可能为休市日或盘后数据尚未更新"
                ),
            )

        recent_rows = qfq_rows[-ma_period:]
        kline_close = latest.close
        ma_value = (
            sum((row.close for row in recent_rows), Decimal("0"))
            / Decimal(ma_period)
        )

        quote_price, quote_date, quote_note = fetch_tencent_quote(symbol)
        final_close = kline_close
        notes = [
            f"{ma_name}采用腾讯财经最近{ma_period}个交易日前复权收盘价算术平均值"
        ]

        if quote_price is not None and quote_date == target_date:
            difference = abs(quote_price - kline_close)
            final_close = quote_price
            if difference <= Decimal("0.001"):
                notes.append("腾讯实时快照与K线末日收盘价一致，采用实时快照价")
            else:
                notes.append(
                    f"腾讯实时快照价{round4(quote_price)}与日K收盘价"
                    f"{round4(kline_close)}相差{round4(difference)}，"
                    f"最终采用实时快照价{round4(quote_price)}"
                )
        elif quote_price is not None:
            notes.append(
                f"腾讯快照日期{quote_date or '未知'}与目标日期{target_date}不一致，"
                f"最终采用日K收盘价{round4(kline_close)}"
            )
        else:
            notes.append(
                f"{quote_note}；最终采用日K收盘价{round4(kline_close)}"
            )

        ratio = final_close / ma_value

        return {
            "ETF代码": code,
            "ETF名称": name,
            "交易日期": target_date,
            "ETF现价": float(round4(final_close)),
            "MA周期": ma_period,
            "MA名称": ma_name,
            "MA": float(round4(ma_value)),
            "现价/MA": float(round4(ratio)),
            "状态": "ok",
            "说明": "；".join(notes),
        }

    except Exception as exc:
        return empty_result(
            code,
            name,
            ma_period,
            None,
            "error",
            f"{type(exc).__name__}: {exc}",
        )


def write_outputs(results: list[dict[str, Any]], target_date: str) -> None:
    success = [item for item in results if item["状态"] == "ok"]
    failed = [item for item in results if item["状态"] != "ok"]

    # results已按ETFS固定顺序生成，不再按现价/MA排序。
    ordered = results
    generated_at = datetime.now(BEIJING_TZ).isoformat(timespec="seconds")
    payload = {
        "交易日期": target_date,
        "生成时间": generated_at,
        "数据来源": "腾讯财经前复权日K线；腾讯实时快照用于最终现价",
        "计算口径": (
            "ETF现价优先采用与交易日期一致的腾讯实时快照价；"
            "各ETF按预设周期采用截至当日最近N个交易日前复权收盘价的算术平均值"
        ),
        "均线配置": {
            code: f"MA{ma_period}" for code, _, ma_period in ETFS
        },
        "成功数量": len(success),
        "失败数量": len(failed),
        "data": ordered,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    fields = [
        "ETF代码",
        "ETF名称",
        "交易日期",
        "ETF现价",
        "MA周期",
        "MA名称",
        "MA",
        "现价/MA",
        "状态",
        "说明",
    ]
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(ordered)

    print(
        f"已写入 {JSON_PATH} 和 {CSV_PATH}，"
        f"成功{len(success)}只，失败{len(failed)}只。"
    )
    for item in ordered:
        print(item)


def main() -> int:
    now = datetime.now(BEIJING_TZ)
    target_date = now.date().isoformat()
    print(f"北京时间: {now.isoformat(timespec='seconds')}，目标日期: {target_date}")

    if now.weekday() >= 5:
        print("今天是周末，不执行行情写入。")
        return 0

    results = [
        calculate_one(code, name, ma_period, target_date)
        for code, name, ma_period in ETFS
    ]
    success_count = sum(item["状态"] == "ok" for item in results)
    error_count = sum(item["状态"] == "error" for item in results)

    if success_count == 0:
        for item in results:
            print(item)
        if error_count > 0:
            print("所有ETF均获取失败，工作流标记为失败。")
            return 1
        print(
            f"所有ETF均无{target_date}行情，视为休市或数据尚未更新，"
            "不覆盖旧结果。"
        )
        return 0

    write_outputs(results, target_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
