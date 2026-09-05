"""전종목 일봉 수집 (증분).

  python scripts/prices.py --data data --days 520

close.parquet / volume.parquet : index=거래일, columns=티커 (float32)
meta.json                      : 종목명, 시가총액, 마지막 갱신일

증분 동작: 기존 parquet 의 마지막 날짜 다음 거래일부터만 받는다.
파일이 없으면 --days 만큼 전체 백필 (약 500 거래일 = 500 API 호출, 5~10분).
448일선을 쓰므로 최소 460 거래일 이상 보유해야 한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

KOSPI_INDEX = "1001"


def _krx():
    try:
        from pykrx import stock
    except ImportError:
        sys.exit("pykrx 가 필요합니다: pip install pykrx")
    return stock


def trading_days(start: date, end: date) -> list[str]:
    """실제 거래일 목록. KOSPI 지수 일봉이 존재하는 날 = 거래일."""
    stock = _krx()
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    try:
        idx = stock.get_index_ohlcv(s, e, KOSPI_INDEX)
        if len(idx):
            return [d.strftime("%Y%m%d") for d in idx.index]
    except Exception as exc:  # pykrx 버전차 대비
        print(f"  지수 조회 실패({exc}) -> 달력 순회로 대체", file=sys.stderr)
    # 폴백: 평일 전부 시도하고 빈 응답은 건너뛴다
    days, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return days


def fetch_day(stock, day: str) -> pd.DataFrame | None:
    df = stock.get_market_ohlcv(day, market="ALL")
    if df is None or df.empty:
        return None
    # 휴장일에도 전일 데이터가 0으로 채워져 오는 경우가 있어 방어
    if "거래량" in df.columns and df["거래량"].sum() == 0:
        return None
    return df


def load(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    return df


def save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.sort_index().astype("float32").to_parquet(path, compression="zstd")


def update_meta(stock, data_dir: Path, last_day: str) -> None:
    """종목명 + 시가총액 스냅샷. 각각 1회 호출로 끝낸다."""
    meta: dict[str, dict] = {}
    try:
        chg = stock.get_market_price_change(last_day, last_day, market="ALL")
        name_col = next((c for c in chg.columns if "종목명" in str(c)), None)
        if name_col:
            for ticker, row in chg.iterrows():
                meta.setdefault(str(ticker), {})["name"] = str(row[name_col])
    except Exception as exc:
        print(f"  종목명 조회 실패: {exc}", file=sys.stderr)

    try:
        cap = stock.get_market_cap(last_day, market="ALL")
        cap_col = next((c for c in cap.columns if "시가총액" in str(c)), None)
        val_col = next((c for c in cap.columns if "거래대금" in str(c)), None)
        for ticker, row in cap.iterrows():
            m = meta.setdefault(str(ticker), {})
            if cap_col:
                m["cap"] = int(row[cap_col])
            if val_col:
                m["value"] = int(row[val_col])
    except Exception as exc:
        print(f"  시가총액 조회 실패: {exc}", file=sys.stderr)

    (data_dir / "meta.json").write_text(
        json.dumps({"as_of": last_day, "tickers": meta}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  meta.json: {len(meta):,}종목")


def main() -> int:
    ap = argparse.ArgumentParser(description="전종목 일봉 증분 수집")
    ap.add_argument("--data", default="data")
    ap.add_argument("--days", type=int, default=520, help="최초 백필 시 확보할 거래일 수")
    ap.add_argument("--end", default=None, help="YYYYMMDD (기본: 오늘)")
    args = ap.parse_args()

    stock = _krx()
    data_dir = Path(args.data)
    close_p, vol_p = data_dir / "close.parquet", data_dir / "volume.parquet"

    end = datetime.strptime(args.end, "%Y%m%d").date() if args.end else date.today()
    close, volume = load(close_p), load(vol_p)

    if close is None:
        start = end - timedelta(days=int(args.days * 1.55))  # 거래일 -> 달력일 여유
        print(f"전체 백필: {start} ~ {end}")
    else:
        start = (close.index.max() + pd.Timedelta(days=1)).date()
        print(f"증분 수집: {start} ~ {end} (보유 {len(close)}거래일)")
        if start > end:
            print("갱신할 날짜가 없습니다.")
            update_meta(stock, data_dir, close.index.max().strftime("%Y%m%d"))
            return 0

    days = trading_days(start, end)
    print(f"대상 거래일 {len(days)}일")

    c_rows: dict[pd.Timestamp, pd.Series] = {}
    v_rows: dict[pd.Timestamp, pd.Series] = {}
    for i, day in enumerate(days, 1):
        df = fetch_day(stock, day)
        if df is None:
            continue
        ts = pd.Timestamp(day)
        c_rows[ts] = df["종가"].astype("float32")
        v_rows[ts] = df["거래량"].astype("float32")
        if i % 20 == 0 or i == len(days):
            print(f"\r  {i}/{len(days)} ({day}) 수집 {len(c_rows)}일", end="", flush=True)
    print()

    if not c_rows:
        print("수집된 거래일이 없습니다. 휴장 기간이거나 KRX 접근이 차단됐을 수 있습니다.")
        return 1

    new_c = pd.DataFrame(c_rows).T.sort_index()
    new_v = pd.DataFrame(v_rows).T.sort_index()
    close = new_c if close is None else pd.concat([close, new_c]).groupby(level=0).last()
    volume = new_v if volume is None else pd.concat([volume, new_v]).groupby(level=0).last()

    # 448일선 + 여유만 남기고 잘라 파일 크기를 일정하게 유지
    keep = max(args.days, 470)
    close, volume = close.tail(keep), volume.tail(keep)

    save(close, close_p)
    save(volume, vol_p)
    last = close.index.max().strftime("%Y%m%d")
    print(f"저장: {close.shape[0]}거래일 × {close.shape[1]:,}종목 (최종 {last})")
    update_meta(stock, data_dir, last)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
