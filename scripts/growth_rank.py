"""3년 성장률 랭킹.

  python growth_rank.py --db fin.db --base 2022 --last 2025 --top 20 \
      --min-cap 100000000000 --out ./out

계산 규칙 (앞서 정리한 함정 대응):
  * 기준연도 값 > 0 이고 최근연도 값 > 0  -> CAGR = (last/base)^(1/n) - 1
  * 기준연도 <= 0 이고 최근연도  > 0      -> '흑자전환' 별도 목록 (CAGR 계산 불가)
  * 기준연도  > 0 이고 최근연도 <= 0      -> '적자전환' 제외
  * 둘 다 <= 0                            -> '계속적자' 제외
  분모가 음수면 CAGR 은 수학적으로 의미가 없다. 억지로 % 를 만들지 않는다.

연결(CFS) 우선, 없으면 별도(OFS) 사용. 혼용 여부를 fs_div 컬럼에 남긴다.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

METRICS = {
    "revenue": "매출액",
    "operating_profit": "영업이익",
    "total_assets": "자산총계",
    "total_equity": "자기자본",
}


def load_frame(db: str, base: int, last: int, reprt_code: str) -> pd.DataFrame:
    conn = sqlite3.connect(db)
    q = """
        SELECT f.corp_code, c.corp_name, c.stock_code,
               f.bsns_year, f.fs_div, f.metric, f.amount, f.rcept_dt
        FROM financials f
        JOIN corps c USING (corp_code)
        WHERE f.bsns_year IN (?, ?)
          AND f.reprt_code = ?
          AND f.amount IS NOT NULL
    """
    df = pd.read_sql_query(q, conn, params=(base, last, reprt_code))
    conn.close()
    if df.empty:
        return df
    # 연결 우선: 같은 (corp, year, metric) 에 CFS/OFS 가 다 있으면 CFS 채택
    df["fs_rank"] = df["fs_div"].map({"CFS": 0, "OFS": 1}).fillna(2)
    df = df.sort_values("fs_rank").drop_duplicates(
        subset=["corp_code", "bsns_year", "metric"], keep="first"
    )
    return df


def classify(base_v, last_v, years: int):
    """(cagr, 상태) 반환. cagr 은 계산 불가 시 None."""
    if base_v is None or last_v is None or pd.isna(base_v) or pd.isna(last_v):
        return None, "결측"
    if base_v > 0 and last_v > 0:
        return (last_v / base_v) ** (1 / years) - 1, "정상"
    if base_v <= 0 < last_v:
        return None, "흑자전환"
    if last_v <= 0 < base_v:
        return None, "적자전환"
    return None, "계속적자"


def build_metric_table(df: pd.DataFrame, metric: str, base: int, last: int) -> pd.DataFrame:
    sub = df[df["metric"] == metric]
    if sub.empty:
        return pd.DataFrame()
    wide = sub.pivot_table(
        index=["corp_code", "corp_name", "stock_code"],
        columns="bsns_year",
        values="amount",
        aggfunc="first",
    ).reset_index()
    fs = sub[sub["bsns_year"] == last][["corp_code", "fs_div", "rcept_dt"]]
    wide = wide.merge(fs, on="corp_code", how="left")

    for y in (base, last):
        if y not in wide.columns:
            wide[y] = pd.NA
    years = last - base
    calc = wide.apply(lambda r: classify(r[base], r[last], years), axis=1)
    wide["cagr"] = [c[0] for c in calc]
    wide["status"] = [c[1] for c in calc]
    wide = wide.rename(columns={base: "base_amt", last: "last_amt"})
    wide["multiple"] = wide["last_amt"] / wide["base_amt"].where(wide["base_amt"] > 0)
    return wide


def attach_market_filter(wide: pd.DataFrame, min_cap: float, min_value: float, date: str | None):
    """pykrx 로 시가총액/거래대금을 붙여 소형주 노이즈를 제거. 미설치면 건너뜀."""
    if min_cap <= 0 and min_value <= 0:
        return wide, "필터 없음"
    try:
        from pykrx import stock as krx
    except ImportError:
        return wide, "pykrx 미설치 -> 시총/거래대금 필터 생략"

    if date is None:
        date = krx.get_nearest_business_day_in_a_week()
    cap = krx.get_market_cap(date, market="ALL").reset_index()
    cap.columns = [str(c) for c in cap.columns]
    tick_col = cap.columns[0]
    cap = cap.rename(columns={tick_col: "stock_code", "시가총액": "market_cap", "거래대금": "trade_value"})
    keep = ["stock_code", "market_cap"] + (["trade_value"] if "trade_value" in cap else [])
    wide = wide.merge(cap[keep], on="stock_code", how="left")

    before = len(wide)
    if min_cap > 0:
        wide = wide[wide["market_cap"].fillna(0) >= min_cap]
    if min_value > 0 and "trade_value" in wide:
        wide = wide[wide["trade_value"].fillna(0) >= min_value]
    return wide, f"{date} 기준 시총/거래대금 필터: {before:,} -> {len(wide):,}종목"


def fmt_won(x) -> str:
    if pd.isna(x):
        return "-"
    return f"{x / 1e8:,.0f}억"


def report(wide: pd.DataFrame, label: str, top: int) -> pd.DataFrame:
    ok = wide[wide["status"] == "정상"].sort_values("cagr", ascending=False).head(top).copy()
    print(f"\n{'=' * 78}\n{label} 3년 CAGR TOP {top}\n{'=' * 78}")
    if ok.empty:
        print("  해당 없음")
        return ok
    print(f"{'#':>3} {'종목명':<18}{'코드':<8}{'CAGR':>9} {'배수':>7} {'기준':>12} {'최근':>12}  fs")
    for i, (_, r) in enumerate(ok.iterrows(), 1):
        print(
            f"{i:>3} {str(r['corp_name'])[:17]:<18}{r['stock_code']:<8}"
            f"{r['cagr'] * 100:>8.1f}% {r['multiple']:>6.2f}x "
            f"{fmt_won(r['base_amt']):>12} {fmt_won(r['last_amt']):>12}  {r.get('fs_div', '-')}"
        )
    return ok


def report_turnaround(wide: pd.DataFrame, label: str, top: int) -> pd.DataFrame:
    tn = wide[wide["status"] == "흑자전환"].sort_values("last_amt", ascending=False).head(top).copy()
    if tn.empty:
        return tn
    print(f"\n[{label}] 흑자전환 (CAGR 계산 불가, 최근값 순) TOP {min(top, len(tn))}")
    for i, (_, r) in enumerate(tn.iterrows(), 1):
        print(
            f"{i:>3} {str(r['corp_name'])[:17]:<18}{r['stock_code']:<8}"
            f"{fmt_won(r['base_amt']):>12} -> {fmt_won(r['last_amt']):>12}"
        )
    return tn


def main() -> int:
    ap = argparse.ArgumentParser(description="3년 성장률 TOP N")
    ap.add_argument("--db", default="fin.db")
    ap.add_argument("--base", type=int, required=True, help="기준 사업연도")
    ap.add_argument("--last", type=int, required=True, help="최근 사업연도")
    ap.add_argument("--reprt-code", default="11011")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-cap", type=float, default=1e11, help="최소 시가총액(원), 기본 1,000억")
    ap.add_argument("--min-value", type=float, default=0, help="최소 당일 거래대금(원)")
    ap.add_argument("--cap-date", default=None, help="시총 기준일 YYYYMMDD")
    ap.add_argument("--out", default=None, help="CSV 출력 디렉터리")
    args = ap.parse_args()

    df = load_frame(args.db, args.base, args.last, args.reprt_code)
    if df.empty:
        print("데이터가 없습니다. build_db.py 를 먼저 실행하세요.")
        return 1
    print(f"로드: {df['corp_code'].nunique():,}개사 / {len(df):,}행 ({args.base} vs {args.last})")

    outdir = Path(args.out) if args.out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)

    for metric, label in METRICS.items():
        wide = build_metric_table(df, metric, args.base, args.last)
        if wide.empty:
            print(f"\n[{label}] 데이터 없음")
            continue
        wide, note = attach_market_filter(wide, args.min_cap, args.min_value, args.cap_date)
        print(f"\n[{label}] {note}")
        counts = wide["status"].value_counts().to_dict()
        print(f"[{label}] 상태 분포: {counts}")

        ok = report(wide, label, args.top)
        report_turnaround(wide, label, args.top)

        if outdir is not None:
            wide.to_csv(outdir / f"growth_{metric}_full.csv", index=False, encoding="utf-8-sig")
            ok.to_csv(outdir / f"growth_{metric}_top{args.top}.csv", index=False, encoding="utf-8-sig")

    if outdir:
        print(f"\nCSV 저장: {outdir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
