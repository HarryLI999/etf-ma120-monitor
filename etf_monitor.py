from __future__ import annotations

import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

TZ = ZoneInfo("Asia/Shanghai")
OUTPUT_DIR = Path("data")
JSON_PATH = OUTPUT_DIR / "latest.json"
CSV_PATH = OUTPUT_DIR / "latest.csv"

ETF_LIST = {
    "512890": "红利低波ETF华泰柏瑞",
    "515450": "红利低波50ETF南方",
    "159307": "红利低波100ETF博时",
    "515080": "中证红利ETF招商",
    "561580": "央企红利ETF华泰柏瑞",
    "159758": "红利质量ETF华夏",
    "159545": "恒生红利低波ETF易方达",
    "513910": "港股通央企红利ETF华夏",
}


@dataclass
class ETFResult:
    code: str
    name: str
    trade_date: str | None = None
    close: float | None = None
    ma120: float | None = None
    ratio: float | None = None
    status: str = "ok"
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ETF代码": self.code,
            "ETF名称": self.name,
            "交易日期": self.trade_date,
            "ETF现价": self.close,
            "MA120": self.ma120,
            "现价/MA120": self.ratio,
            "状态": self.status,
            "说明": self.note,
        }


def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    required = {"日期", "收盘"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"返回数据缺少字段: {sorted(missing)}")

    clean = df.copy()
    clean["日期"] = pd.to_datetime(clean["日期"], errors="coerce")
    clean["收盘"] = pd.to_numeric(clean["收盘"], errors="coerce")
    clean = clean.dropna(subset=["日期", "收盘"])
    clean = clean.sort_values("日期").drop_duplicates(subset=["日期"], keep="last")
    return clean.reset_index(drop=True)


def fetch_one(code: str, name: str, start_date: str, end_date: str) -> ETFResult:
    result = ETFResult(code=code, name=name)
    try:
        raw_df = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
        )
        qfq_df = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )

        raw_df = normalize_history(raw_df)
        qfq_df = normalize_history(qfq_df)

        if raw_df.empty or qfq_df.empty:
            raise ValueError("行情为空")
        if len(qfq_df) < 120:
            raise ValueError(f"前复权历史数据不足120条，当前仅{len(qfq_df)}条")

        raw_last = raw_df.iloc[-1]
        qfq_last = qfq_df.iloc[-1]
        raw_date = raw_last["日期"].date()
        qfq_date = qfq_last["日期"].date()
        if raw_date != qfq_date:
            raise ValueError(f"不复权与前复权最新日期不一致: {raw_date} / {qfq_date}")

        latest_120 = qfq_df.tail(120)
        close = float(raw_last["收盘"])
        ma120 = float(latest_120["收盘"].mean())
        ratio = close / ma120

        result.trade_date = raw_date.isoformat()
        result.close = round(close, 4)
        result.ma120 = round(ma120, 4)
        result.ratio = round(ratio, 4)
        result.note = "现价采用东方财富不复权收盘价；MA120采用最近120个交易日前复权收盘价算术平均值。"
    except Exception as exc:  # noqa: BLE001
        result.status = "error"
        result.note = f"{type(exc).__name__}: {exc}"
    return result


def run_once(today: date) -> tuple[list[ETFResult], bool]:
    start = (today - timedelta(days=500)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    results = [fetch_one(code, name, start, end) for code, name in ETF_LIST.items()]
    has_today = any(item.trade_date == today.isoformat() for item in results)
    return results, has_today


def write_outputs(results: list[ETFResult], now: datetime) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sorted_results = sorted(
        results,
        key=lambda item: (item.ratio is None, item.ratio if item.ratio is not None else float("inf")),
    )

    trade_dates = sorted({item.trade_date for item in results if item.trade_date})
    payload = {
        "generated_at_beijing": now.isoformat(timespec="seconds"),
        "trade_dates": trade_dates,
        "primary_source": "AKShare fund_etf_hist_em（东方财富历史行情）",
        "price_rule": "ETF现价使用当天不复权收盘价",
        "ma120_rule": "最近120个交易日前复权收盘价的简单算术平均值，包含当日",
        "records": [item.as_dict() for item in sorted_results],
    }

    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(sorted_results[0].as_dict().keys()))
        writer.writeheader()
        writer.writerows(item.as_dict() for item in sorted_results)


def main() -> int:
    now = datetime.now(TZ)
    today = now.date()

    if today.weekday() >= 5:
        print(f"{today}为周末，不更新数据。")
        return 0

    # 数据源盘后偶尔更新稍慢；最多尝试3次，每次间隔3分钟。
    results: list[ETFResult] = []
    has_today = False
    for attempt in range(1, 4):
        print(f"第{attempt}次获取，目标日期：{today}")
        results, has_today = run_once(today)
        for item in results:
            print(item.as_dict())
        if has_today:
            break
        if attempt < 3:
            print("尚未发现今日行情，180秒后重试。")
            time.sleep(180)

    # 工作日但所有ETF均没有今日数据：通常为A股休市日或数据源尚未更新。
    # 不覆盖上一交易日的latest文件，以免产生错误通知。
    if not has_today:
        print(f"所有ETF均无{today}行情，视为休市或数据尚未更新；不写入结果。")
        return 0

    # 已确认至少一个ETF出现今日行情。缺失项目会以error状态写入，便于明确标注。
    for item in results:
        if item.trade_date != today.isoformat() and item.status == "ok":
            item.status = "error"
            item.note = f"数据尚未更新至{today}，最新交易日期为{item.trade_date}。"
            item.close = None
            item.ma120 = None
            item.ratio = None

    write_outputs(results, datetime.now(TZ))
    print(f"已写入：{JSON_PATH} 和 {CSV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
