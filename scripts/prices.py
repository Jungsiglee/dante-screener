"""전종목 일봉 수집 (증분) — yfinance 기반.

  python scripts/prices.py --data data --days 520

왜 pykrx 를 버렸나:
  GitHub Actions 러너에서 KRX 데이터 API 가 세션 없음(응답에 LOGOUT)으로 요청을
  거부한다. 2026-09-05 확인. 홈페이지는 HTTP 200 이 오므로 IP 전체 차단은 아니고,
  요청이 정상 세션으로 인정받지 못하는 문제다. 러너에서 우회할 방법이 마땅치 않아
  데이터원을 야후 파이낸스로 교체했다. DART 는 러너에서 정상 동작한다(status 000).

구성:
  종목 리스트  DART corpCode.xml 의 stock_code (상장사만)
  일봉        yfinance, 6자리 티커 + .KS(코스피) / .KQ(코스닥)
  거래대금     종가 x 거래량 을 직접 계산 (pykrx 없이는 시가총액을 못 구해 대체)

산출물:
  close.parquet / volume.parquet  index=거래일, columns=6자리 티커 (float32)
  meta.json                       종목명, 20일 평균 거래대금, 마지막 갱신일
  symbols.json                    티커 -> 야후 심볼 해석 캐시

증분 동작: 기존 parquet 의 마지막 날짜 다음날부터만 받는다. 파일이 없으면
--days 만큼 전체 백필. 448일선을 쓰므로 최소 460 거래일 이상 보유해야 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

BATCH = 150        # yfinance 한 번에 요청할 심볼 수
PAUSE = 1.0        # 배치 간 대기 (야후 요청 제한 회피)
SUFFIXES = (".KS", ".KQ")   # 코스피, 코스닥


# ----------------------------------------------------------------------
# 종목 리스트
# ----------------------------------------------------------------------
def dart_listed() -> dict[str, str]:
    """DART 고유번호 파일에서 상장사 {6자리 티커: 종목명}."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dart_client import DartClient

    if not os.environ.get("DART_API_KEY", "").strip():
        raise SystemExit(
            "DART_API_KEY 가 필요합니다. 종목 리스트를 DART 에서 받기 때문입니다."
        )
    rows = DartClient().corp_codes()
    out: dict[str, str] = {}
    for r in rows:
        code = (r.get("stock_code") or "").strip()
        if len(code) == 6 and code.isdigit():
            out[code] = (r.get("corp_name") or "").strip() or code
    if not out:
        raise SystemExit("DART corpCode 에서 상장사를 찾지 못했습니다.")
    return out


# ----------------------------------------------------------------------
# yfinance
# ----------------------------------------------------------------------
def _yf():
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance 가 필요합니다: pip install yfinance")
    return yf


def _download(symbols, start, end):
    """심볼 리스트 -> (종가, 거래량). 컬럼은 야후 심볼 그대로."""
    yf = _yf()
    raw = yf.download(
        symbols,
        start=start,
        end=end,
        auto_adjust=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    if raw is None or len(raw) == 0:
        return pd.DataFrame(), pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        lv0 = raw.columns.get_level_values(0)
        close = raw["Close"] if "Close" in lv0 else pd.DataFrame()
        vol = raw["Volume"] if "Volume" in lv0 else pd.DataFrame()
    else:
        # 심볼이 하나면 컬럼이 평평하게 온다
        close = raw[["Close"]].rename(columns={"Close": symbols[0]})
        vol = raw[["Volume"]].rename(columns={"Volume": symbols[0]})
    return close, vol


def fetch(symbols, start, end):
    """배치로 나눠 받아 이어 붙인다."""
    cs, vs = [], []
    total = (len(symbols) + BATCH - 1) // BATCH
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i : i + BATCH]
        n = i // BATCH + 1
        try:
            c, v = _download(chunk, start, end)
        except Exception as exc:
            print(f"  배치 {n}/{total} 실패: {exc}", file=sys.stderr)
            continue
        if len(c.columns):
            cs.append(c)
            vs.append(v)
        print(f"  배치 {n}/{total} ({len(c.columns)}종목)", flush=True)
        time.sleep(PAUSE)
    if not cs:
        return pd.DataFrame(), pd.DataFrame()
    return pd.concat(cs, axis=1), pd.concat(vs, axis=1)


# ----------------------------------------------------------------------
# 심볼 해석 (.KS / .KQ)
# ----------------------------------------------------------------------
def resolve_symbols(tickers, cache_path: Path):
    """6자리 티커 -> 야후 심볼. 한 번 푼 것은 캐시에서 재사용한다.

    DART 는 코스피/코스닥 구분을 주지 않으므로 .KS 를 먼저 시도하고,
    데이터가 없는 것만 .KQ 로 재시도한다.
    """
    cache = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))

    unknown = [t for t in tickers if t not in cache]
    if unknown:
        print(f"심볼 해석 필요: {len(unknown):,}종목")
        end = date.today()
        s = (end - timedelta(days=12)).isoformat()
        e = (end + timedelta(days=1)).isoformat()

        remaining = list(unknown)
        for suf in SUFFIXES:
            if not remaining:
                break
            print(f"  {suf} 시도: {len(remaining):,}종목")
            close, _ = fetch([t + suf for t in remaining], s, e)
            hit = set()
            for col in close.columns:
                if close[col].notna().any():
                    hit.add(str(col).split(".")[0])
            for t in hit:
                cache[t] = t + suf
            remaining = [t for t in remaining if t not in hit]
            print(f"    확인 {len(hit):,}종목, 남음 {len(remaining):,}종목")

        for t in remaining:
            cache[t] = ""  # 야후에 없는 종목. 빈 값으로 기록해 재시도 방지

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    return {t: cache[t] for t in tickers if cache.get(t)}


# ----------------------------------------------------------------------
# 저장
# ----------------------------------------------------------------------
def load(path: Path):
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    return df


def save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.sort_index().astype("float32").to_parquet(path, compression="zstd")


def write_meta(data_dir: Path, names, close, volume, last_day: str) -> None:
    """종목명 + 20일 평균 거래대금. 시가총액은 구할 수 없어 0 으로 둔다."""
    turnover = (close * volume).tail(20).mean()
    meta = {}
    for t in close.columns:
        v = turnover.get(t)
        meta[str(t)] = {
            "name": names.get(str(t), str(t)),
            "cap": 0,
            "value": int(v) if pd.notna(v) else 0,
        }
    (data_dir / "meta.json").write_text(
        json.dumps({"as_of": last_day, "tickers": meta}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  meta.json: {len(meta):,}종목")


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="전종목 일봉 증분 수집 (yfinance)")
    ap.add_argument("--data", default="data")
    ap.add_argument("--days", type=int, default=520, help="최초 백필 시 확보할 거래일 수")
    ap.add_argument("--end", default=None, help="YYYYMMDD (기본: 오늘)")
    args = ap.parse_args()

    data_dir = Path(args.data)
    close_p, vol_p = data_dir / "close.parquet", data_dir / "volume.parquet"

    end = datetime.strptime(args.end, "%Y%m%d").date() if args.end else date.today()
    close, volume = load(close_p), load(vol_p)

    names = dart_listed()
    print(f"DART 상장사: {len(names):,}종목")
    symbols = resolve_symbols(sorted(names), data_dir / "symbols.json")
    print(f"야후 심볼 확보: {len(symbols):,}종목")
    if not symbols:
        print("받을 심볼이 없습니다.")
        return 1

    if close is None:
        start = end - timedelta(days=int(args.days * 1.55))  # 거래일 -> 달력일 여유
        print(f"전체 백필: {start} ~ {end}")
    else:
        start = (close.index.max() + pd.Timedelta(days=1)).date()
        print(f"증분 수집: {start} ~ {end} (보유 {len(close)}거래일)")
        if start > end:
            print("갱신할 날짜가 없습니다.")
            write_meta(data_dir, names, close, volume,
                       close.index.max().strftime("%Y%m%d"))
            return 0

    rev = {v: k for k, v in symbols.items()}
    new_c, new_v = fetch(sorted(symbols.values()), start.isoformat(),
                         (end + timedelta(days=1)).isoformat())
    if len(new_c) == 0 or not len(new_c.columns):
        if close is not None:
            # 휴장일·주말이면 새 데이터가 없는 게 정상이다. 실패로 처리하지 않는다.
            print("새로 받은 거래일이 없습니다 (휴장 또는 이미 최신).")
            write_meta(data_dir, names, close, volume,
                       close.index.max().strftime("%Y%m%d"))
            return 0
        print("수집된 데이터가 없습니다. 야후 접근이 막혔을 수 있습니다.")
        return 1

    new_c = new_c.rename(columns=lambda c: rev.get(str(c), str(c))).dropna(how="all")
    new_v = new_v.rename(columns=lambda c: rev.get(str(c), str(c))).dropna(how="all")
    new_c.index = pd.to_datetime(new_c.index).tz_localize(None)
    new_v.index = pd.to_datetime(new_v.index).tz_localize(None)

    close = new_c if close is None else pd.concat([close, new_c]).groupby(level=0).last()
    volume = new_v if volume is None else pd.concat([volume, new_v]).groupby(level=0).last()

    # 448일선 + 여유만 남기고 잘라 파일 크기를 일정하게 유지
    keep = max(args.days, 470)
    close = close.sort_index().tail(keep)
    volume = volume.sort_index().tail(keep).reindex(columns=close.columns)

    save(close, close_p)
    save(volume, vol_p)
    last = close.index.max().strftime("%Y%m%d")
    print(f"저장: {close.shape[0]}거래일 × {close.shape[1]:,}종목 (최종 {last})")
    write_meta(data_dir, names, close, volume, last)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
