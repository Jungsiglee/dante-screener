"""단테 기법 조건 스캔 -> docs/result.json

  python scripts/scan.py --data data --out docs/result.json --fin dart_fin/fin.db

조건 (검색으로 파악한 256기법 / 이평때리기 기준. 수치 정의는 전부 튜너블):
  256      5일선이 20일선을 상향돌파 (당일 발생)  -> 손절 20일선, 목표 60일선 이상
  역배열   MA112 < MA224 < MA448 (장기 역배열 상태)
  112안착  종가가 MA112 위로 올라와 SETTLE_DAYS 연속 유지
  224돌파  종가가 MA224 상향돌파
  바닥     최근 LOOKBACK 고점 대비 DRAWDOWN 이상 하락한 이력
  거래량   당일 거래량 >= 20일 평균 * VOL_MULT

'안착', '바닥' 같은 말은 원 기법에 수치 정의가 없다. 아래 상수는 임의로 정한
출발점이며, 백테스트로 확정하기 전까지는 신뢰하지 말 것.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

MA_WINDOWS = (5, 20, 60, 112, 224, 448)

SETTLE_DAYS = 3       # 112일선 안착 판정 연속일수
LOOKBACK = 250        # 바닥 판정 고점 조회 기간
DRAWDOWN = -0.35      # 고점 대비 하락률 기준
VOL_MULT = 1.5        # 거래량 배수
CROSS_RECENT = 3      # 최근 N거래일 내 크로스면 유효

WEIGHTS = {
    "cross_256": 30,
    "settled_112": 20,
    "break_224": 15,
    "reverse_align": 10,
    "bottomed": 10,
    "volume": 15,
}
KST = timezone(timedelta(hours=9))


MIN_ROW_COVERAGE = 0.30   # 이 비율 미만의 종목만 값이 있는 날짜는 유령 거래일로 본다
FFILL_LIMIT = 5           # 연속 결측을 직전 종가로 메우는 최대 일수


def clean(close: pd.DataFrame, volume: pd.DataFrame):
    """전종목 합집합 인덱스의 구멍을 메운다.

    종목마다 상장일이 다르고 데이터원이 개별 종목의 거래일을 빠뜨리기도 해서,
    전종목을 한 행렬에 모으면 종목별로 결측이 흩어져 생긴다. pandas rolling 은
    창 안에 NaN 이 하나만 있어도 결과를 NaN 으로 만들기 때문에, 그대로 두면
    448일선이 거의 전 종목에서 NaN 이 되어 통째로 걸러진다(2026-09-05, 2,697종목
    중 1종목만 생존한 사고).

    두 단계로 처리한다.
      1) 유령 거래일 제거: 극소수 종목만 값이 있는 날짜 행을 버린다.
      2) 종가는 직전 값으로 최대 FFILL_LIMIT 일까지 메운다(거래 없으면 가격 유지).
         거래량은 0 으로 메운다(거래가 없었다는 뜻이므로).
    상장 전 구간의 선행 결측은 메우지 않는다. 신규 상장주가 이평선 조건을
    통과해서는 안 되기 때문이다.
    """
    n = close.shape[1]
    if n:
        keep = close.notna().sum(axis=1) >= max(1, int(n * MIN_ROW_COVERAGE))
        dropped = int((~keep).sum())
        if dropped:
            print(f"  유령 거래일 {dropped}일 제거 ({len(close)} -> {int(keep.sum())}거래일)")
        close, volume = close[keep], volume.reindex(close.index)

    close = close.ffill(limit=FFILL_LIMIT)
    volume = volume.reindex(columns=close.columns).fillna(0.0)
    volume = volume.where(close.notna())
    return close, volume


def compute(close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    ma = {w: close.rolling(w).mean() for w in MA_WINDOWS}
    last = close.index[-1]

    def cur(df):
        return df.loc[last]

    c = cur(close)
    out = pd.DataFrame({"close": c})
    for w in MA_WINDOWS:
        out[f"ma{w}"] = cur(ma[w])

    # 256: 최근 CROSS_RECENT 거래일 안에 MA5 가 MA20 을 상향돌파
    above = ma[5] > ma[20]
    cross_up = above & ~above.shift(1).fillna(False)
    recent_cross = cross_up.tail(CROSS_RECENT)
    out["cross_256"] = recent_cross.any()
    days_since = recent_cross[::-1].values.argmax(axis=0)
    out["cross_age"] = np.where(out["cross_256"], days_since, -1)

    # 장기 역배열
    out["reverse_align"] = (cur(ma[112]) < cur(ma[224])) & (cur(ma[224]) < cur(ma[448]))

    # 112 안착: SETTLE_DAYS 연속 종가 > MA112, 그리고 그 직전엔 아래에 있었음
    over112 = close > ma[112]
    out["settled_112"] = over112.tail(SETTLE_DAYS).all() & (
        ~over112.shift(SETTLE_DAYS).tail(1).fillna(False).iloc[0]
    )

    # 224 상향돌파
    over224 = close > ma[224]
    out["break_224"] = over224.iloc[-1] & ~over224.shift(1).tail(1).fillna(False).iloc[0]

    # 바닥: LOOKBACK 고점 대비 낙폭 이력
    peak = close.tail(LOOKBACK).max()
    trough = close.tail(LOOKBACK).min()
    out["drawdown"] = (trough / peak) - 1
    out["from_peak"] = (c / peak) - 1
    out["bottomed"] = out["drawdown"] <= DRAWDOWN

    # 거래량
    v20 = volume.rolling(20).mean()
    out["vol_ratio"] = (cur(volume) / cur(v20)).replace([np.inf, -np.inf], np.nan)
    out["volume"] = out["vol_ratio"] >= VOL_MULT

    prev = close.iloc[-2] if len(close) > 1 else c
    out["change"] = (c / prev) - 1

    out["chart_score"] = sum(out[k].fillna(False).astype(int) * w for k, w in WEIGHTS.items())
    # 단테 규칙: 손절은 20일선, 목표는 60일선 위 구간
    out["stop"] = out["ma20"]
    out["target1"] = out["ma60"]
    out["target2"] = out[["ma112", "ma224"]].max(axis=1)
    return out


def add_fundamentals(out: pd.DataFrame, fin_db: str | None) -> pd.DataFrame:
    out["fin_score"] = 0.0
    out["rev_cagr"] = np.nan
    out["op_cagr"] = np.nan
    out["op_status"] = ""
    if not fin_db or not Path(fin_db).exists():
        return out

    conn = sqlite3.connect(fin_db)
    years = [r[0] for r in conn.execute("SELECT DISTINCT bsns_year FROM financials ORDER BY bsns_year")]
    if len(years) < 2:
        conn.close()
        return out
    base, last = years[0], years[-1]
    span = max(last - base, 1)

    q = """SELECT c.stock_code, f.metric, f.bsns_year, f.amount, f.fs_div
           FROM financials f JOIN corps c USING (corp_code)
           WHERE f.bsns_year IN (?,?) AND f.amount IS NOT NULL"""
    df = pd.read_sql_query(q, conn, params=(base, last))
    conn.close()
    if df.empty:
        return out

    df["fs_rank"] = df["fs_div"].map({"CFS": 0, "OFS": 1}).fillna(2)
    df = df.sort_values("fs_rank").drop_duplicates(["stock_code", "metric", "bsns_year"])

    for metric, col in (("revenue", "rev_cagr"), ("operating_profit", "op_cagr")):
        w = df[df["metric"] == metric].pivot_table(
            index="stock_code", columns="bsns_year", values="amount", aggfunc="first"
        )
        if base not in w or last not in w:
            continue
        b, l = w[base], w[last]
        cagr = pd.Series(np.nan, index=w.index)
        ok = (b > 0) & (l > 0)
        cagr[ok] = (l[ok] / b[ok]) ** (1 / span) - 1
        out[col] = out.index.map(cagr)
        if metric == "operating_profit":
            status = pd.Series("", index=w.index)
            status[(b <= 0) & (l > 0)] = "흑자전환"
            status[(b > 0) & (l <= 0)] = "적자전환"
            out["op_status"] = out.index.map(status).fillna("")

    # 백분위 -> 최대 10점씩. 자산증가율은 부채/증자로도 오르므로 점수화하지 않는다.
    for col in ("rev_cagr", "op_cagr"):
        pct = out[col].rank(pct=True)
        out["fin_score"] += (pct * 10).fillna(0)
    out.loc[out["op_status"] == "흑자전환", "fin_score"] += 5
    out.loc[out["op_status"] == "적자전환", "fin_score"] -= 10
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="단테 조건 스캔")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="docs/result.json")
    ap.add_argument("--fin", default=None, help="dart_fin 의 fin.db 경로 (선택)")
    ap.add_argument("--min-value", type=float, default=1e9,
                    help="20일 평균 거래대금 하한 (기본 10억). 시가총액은 데이터원 교체로 못 구한다")
    ap.add_argument("--top", type=int, default=120)
    args = ap.parse_args()

    data = Path(args.data)
    close = pd.read_parquet(data / "close.parquet")
    volume = pd.read_parquet(data / "volume.parquet")
    close.index = pd.to_datetime(close.index)
    volume.index = pd.to_datetime(volume.index)
    close, volume = close.sort_index(), volume.sort_index()

    have = len(close)
    if have < max(MA_WINDOWS):
        print(f"경고: {have}거래일뿐입니다. 448일선은 NaN 이 됩니다.")

    close, volume = clean(close, volume)

    out = compute(close, volume)
    out = add_fundamentals(out, args.fin)

    meta_path = data / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {"tickers": {}}
    tick = meta.get("tickers", {})
    out["name"] = [tick.get(t, {}).get("name", t) for t in out.index]
    out["cap"] = [tick.get(t, {}).get("cap", 0) for t in out.index]
    out["value"] = [tick.get(t, {}).get("value", 0) for t in out.index]

    before = len(out)
    out = out[out["value"] >= args.min_value] if args.min_value > 0 else out
    after_value = len(out)
    out = out[~out[[f"ma{w}" for w in MA_WINDOWS]].isna().any(axis=1)]
    print(f"  거래대금 {args.min_value/1e8:.0f}억 이상: {before:,} -> {after_value:,}")
    print(f"  이평선 산출 가능(상장 {max(MA_WINDOWS)}일 이상): {after_value:,} -> {len(out):,}")
    out["score"] = (out["chart_score"] + out["fin_score"]).clip(0, 100)
    hits = out[out["chart_score"] > 0].sort_values("score", ascending=False).head(args.top)
    print(f"필터: {before:,} -> {len(out):,}종목, 조건 충족 {len(hits):,}종목")

    def row(t, r):
        return {
            "ticker": t,
            "name": r["name"],
            "score": round(float(r["score"]), 1),
            "chart": int(r["chart_score"]),
            "fin": round(float(r["fin_score"]), 1),
            "close": int(r["close"]),
            "change": round(float(r["change"]) * 100, 2),
            "cap": int(r["cap"]),
            "value": int(r["value"]),
            "stop": int(r["stop"]) if pd.notna(r["stop"]) else None,
            "t1": int(r["target1"]) if pd.notna(r["target1"]) else None,
            "t2": int(r["target2"]) if pd.notna(r["target2"]) else None,
            "fromPeak": round(float(r["from_peak"]) * 100, 1),
            "volRatio": round(float(r["vol_ratio"]), 2) if pd.notna(r["vol_ratio"]) else None,
            "revCagr": round(float(r["rev_cagr"]) * 100, 1) if pd.notna(r["rev_cagr"]) else None,
            "opCagr": round(float(r["op_cagr"]) * 100, 1) if pd.notna(r["op_cagr"]) else None,
            "opStatus": r["op_status"] or None,
            "ma": {f"ma{w}": int(r[f"ma{w}"]) for w in MA_WINDOWS},
            "flags": [k for k in WEIGHTS if bool(r[k])],
            "crossAge": int(r["cross_age"]) if r["cross_256"] else None,
        }

    payload = {
        "generatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "tradeDate": close.index[-1].strftime("%Y-%m-%d"),
        "universe": int(len(out)),
        "sessions": have,
        "params": {
            "settleDays": SETTLE_DAYS, "lookback": LOOKBACK, "drawdown": DRAWDOWN,
            "volMult": VOL_MULT, "crossRecent": CROSS_RECENT, "minValue": args.min_value,
        },
        "weights": WEIGHTS,
        "items": [row(t, r) for t, r in hits.iterrows()],
    }
    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"저장: {outp} ({outp.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
