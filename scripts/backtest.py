"""단테 조건의 실전 성적 측정.

  python scripts/backtest.py --data data --out docs/backtest.json

무엇을 재는가:
  scan.py 와 똑같은 조건을 과거 모든 날짜에 대해 계산하고, 신호가 뜬 다음
  거래일 종가에 매수했다고 가정한 뒤 아래 규칙으로 청산해 성적을 낸다.
    손절  종가가 20일선 아래로 마감  (단테 규칙)
    시간  --max-hold 거래일 경과
  같은 종목에 포지션이 열려 있는 동안의 재신호는 무시한다.

왜 다음날 종가에 사는가:
  스캔이 장 마감 후(16:10)에 돌기 때문에 신호 당일 종가로는 살 수 없다.
  당일 종가 매수로 잡으면 미래를 미리 아는 셈이 되어 성적이 부풀려진다.

반드시 같이 읽을 것 — 이 숫자가 과대평가인 이유:
  1) 생존편향. 종목 리스트가 '지금 상장돼 있는 회사'라서 그사이 상장폐지된
     회사가 통째로 빠져 있다. 실제로는 여기 안 잡힌 실패 사례가 더 있다.
  2) 체결 가정. 종가에 원하는 수량이 그대로 체결된다고 본다. 상한가 근처나
     거래대금이 얇은 종목에서는 성립하지 않는다.
  3) 비용 미반영. 수수료·세금·슬리피지를 넣지 않았다. 왕복 1%만 잡아도
     평균수익률에서 그만큼 빠진다.
  4) 같은 조건을 만든 사람이 같은 데이터로 검증하고 있다. 조건 상수를 이
     결과에 맞춰 다시 손보면 그때부터는 과최적화다.

비교 기준(baseline)이 핵심이다. 같은 기간 전 종목을 아무 날에나 사서 같은
규칙으로 팔았을 때의 성적을 함께 낸다. 신호의 성적이 이보다 나아야 의미가 있다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan import (CROSS_RECENT, DRAWDOWN, LOOKBACK, MA_WINDOWS, SETTLE_DAYS,
                  VOL_MULT, WEIGHTS, clean)

KST = timezone(timedelta(hours=9))
FLAGS = ("cross_256", "settled_112", "break_224", "reverse_align", "bottomed", "volume")


def conditions(close: pd.DataFrame, volume: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """scan.py 의 조건을 '마지막 날' 대신 '모든 날'에 대해 계산한다."""
    ma = {w: close.rolling(w).mean() for w in MA_WINDOWS}

    above = ma[5] > ma[20]
    cross_up = above & ~above.shift(1).fillna(False)
    # 최근 CROSS_RECENT 거래일 내 크로스 발생
    cross = cross_up.rolling(CROSS_RECENT, min_periods=1).max().fillna(0).astype(bool)

    over112 = close > ma[112]
    settled = (over112.rolling(SETTLE_DAYS).min().fillna(0).astype(bool)
               & ~over112.shift(SETTLE_DAYS).fillna(False))

    over224 = close > ma[224]
    brk = over224 & ~over224.shift(1).fillna(False)

    peak = close.rolling(LOOKBACK).max()
    trough = close.rolling(LOOKBACK).min()
    bottomed = (trough / peak - 1) <= DRAWDOWN

    v20 = volume.rolling(20).mean()
    volflag = (volume / v20) >= VOL_MULT

    return {
        "cross_256": cross,
        "settled_112": settled,
        "break_224": brk,
        "reverse_align": (ma[112] < ma[224]) & (ma[224] < ma[448]),
        "bottomed": bottomed.fillna(False),
        "volume": volflag.fillna(False),
        "_ma20": ma[20],
        "_ma448": ma[448],
        "_turnover": (close * volume).rolling(20).mean(),
    }


def simulate(px, ma20, entries, max_hold):
    """한 종목의 신호 배열 -> 체결 목록. 신호 다음날 종가 매수, 20일선 이탈 시 청산."""
    n = len(px)
    trades = []
    i = 0
    while i < n - 1:
        if not entries[i]:
            i += 1
            continue
        buy = i + 1                     # 신호 다음 거래일 종가
        if buy >= n or not np.isfinite(px[buy]):
            break
        sell, reason = None, "hold"
        for j in range(buy + 1, min(buy + max_hold + 1, n)):
            if not np.isfinite(px[j]):
                continue
            if np.isfinite(ma20[j]) and px[j] < ma20[j]:
                sell, reason = j, "stop"
                break
        if sell is None:
            sell = min(buy + max_hold, n - 1)
            reason = "time"
            if not np.isfinite(px[sell]):
                i = buy + 1
                continue
        trades.append((buy, sell, px[sell] / px[buy] - 1, sell - buy, reason))
        i = sell + 1                    # 청산 전까지 재진입 금지
    return trades


def stats(rets, holds=None, reasons=None) -> dict:
    r = np.asarray(rets, dtype=float)
    if r.size == 0:
        return {"trades": 0}
    win = r > 0
    gains, losses = r[win].sum(), -r[~win].sum()
    out = {
        "trades": int(r.size),
        "winRate": round(float(win.mean()) * 100, 1),
        "avgReturn": round(float(r.mean()) * 100, 2),
        "medianReturn": round(float(np.median(r)) * 100, 2),
        "avgWin": round(float(r[win].mean()) * 100, 2) if win.any() else None,
        "avgLoss": round(float(r[~win].mean()) * 100, 2) if (~win).any() else None,
        "profitFactor": round(float(gains / losses), 2) if losses > 0 else None,
        "best": round(float(r.max()) * 100, 1),
        "worst": round(float(r.min()) * 100, 1),
    }
    if holds is not None and len(holds):
        out["avgHoldDays"] = round(float(np.mean(holds)), 1)
    if reasons:
        out["exitStop"] = int(sum(1 for x in reasons if x == "stop"))
        out["exitTime"] = int(sum(1 for x in reasons if x == "time"))
    return out


def run(close, volume, cond, mask, max_hold):
    """mask(날짜 x 종목 불리언)를 진입 신호로 보고 전 종목을 돌린다."""
    px_all = close.to_numpy(dtype=float)
    ma20_all = cond["_ma20"].to_numpy(dtype=float)
    sig_all = mask.to_numpy(dtype=bool)
    rets, holds, reasons, entry_idx = [], [], [], []
    for k in range(close.shape[1]):
        if not sig_all[:, k].any():
            continue
        for buy, sell, ret, hold, why in simulate(
                px_all[:, k], ma20_all[:, k], sig_all[:, k], max_hold):
            rets.append(ret); holds.append(hold); reasons.append(why); entry_idx.append(buy)
    return rets, holds, reasons, entry_idx


def main() -> int:
    ap = argparse.ArgumentParser(description="단테 조건 백테스트")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="docs/backtest.json")
    ap.add_argument("--min-value", type=float, default=1e9, help="20일 평균 거래대금 하한")
    ap.add_argument("--max-hold", type=int, default=60, help="최대 보유 거래일")
    ap.add_argument("--min-score", type=int, default=70, help="주 시나리오의 차트점수 하한")
    args = ap.parse_args()

    data = Path(args.data)
    close = pd.read_parquet(data / "close.parquet")
    volume = pd.read_parquet(data / "volume.parquet")
    close.index = pd.to_datetime(close.index); volume.index = pd.to_datetime(volume.index)
    close, volume = clean(close.sort_index(), volume.sort_index())

    cond = conditions(close, volume)
    ready = cond["_ma448"].notna() & (cond["_turnover"] >= args.min_value)
    usable = int(ready.any(axis=1).sum())
    print(f"데이터: {close.shape[0]}거래일 × {close.shape[1]:,}종목")
    print(f"이평선 산출 가능 구간: {usable}거래일 "
          f"({close.index[-usable].date() if usable else '-'} ~ {close.index[-1].date()})")
    if usable < 250:
        print(f"경고: 검증 구간이 {usable}거래일뿐입니다. 시장 국면이 하나뿐이라 "
              f"결과를 일반화할 수 없습니다. prices.py --rebuild --days 1500 으로 "
              f"과거를 늘리세요.")

    score = sum(cond[f].astype(int) * w for f, w in WEIGHTS.items())

    report = {
        "generatedAt": datetime.now(KST).isoformat(timespec="seconds"),
        "period": [str(close.index[0].date()), str(close.index[-1].date())],
        "usableSessions": usable,
        "universe": int(close.shape[1]),
        "params": {"maxHold": args.max_hold, "minValue": args.min_value,
                   "minScore": args.min_score, "settleDays": SETTLE_DAYS,
                   "lookback": LOOKBACK, "drawdown": DRAWDOWN, "volMult": VOL_MULT},
        "scenarios": {},
    }

    def add(name, mask, note=""):
        rets, holds, reasons, _ = run(close, volume, cond, mask & ready, args.max_hold)
        s = stats(rets, holds, reasons)
        s["note"] = note
        report["scenarios"][name] = s
        if s["trades"]:
            print(f"  {name:22s} 체결 {s['trades']:6,}  승률 {s['winRate']:5.1f}%  "
                  f"평균 {s['avgReturn']:+6.2f}%  PF {s['profitFactor']}")
        else:
            print(f"  {name:22s} 체결 없음")

    print("\n[기준선] 신호와 무관하게 아무 날 매수")
    rng = np.random.default_rng(42)
    rand = pd.DataFrame(rng.random(close.shape) < 0.02, index=close.index, columns=close.columns)
    add("baseline_random", rand, "임의 진입. 이 성적을 못 넘으면 조건은 무의미하다")

    print(f"\n[주 시나리오] 차트점수 {args.min_score}점 이상")
    add(f"score>={args.min_score}", score >= args.min_score)

    print("\n[점수 구간별]")
    for lo, hi in ((30, 50), (50, 70), (70, 90), (90, 101)):
        add(f"score {lo}-{hi-1}", (score >= lo) & (score < hi))

    print("\n[조건 단독]")
    for f in FLAGS:
        add(f, cond[f])

    print("\n[핵심 조합]")
    add("256+역배열", cond["cross_256"] & cond["reverse_align"])
    add("256+바닥", cond["cross_256"] & cond["bottomed"])
    add("256+거래량", cond["cross_256"] & cond["volume"])
    add("256+역배열+바닥", cond["cross_256"] & cond["reverse_align"] & cond["bottomed"])

    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n저장: {out} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
