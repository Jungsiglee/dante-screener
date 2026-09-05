"""전종목 일봉 수집 (증분·재개 가능) — yfinance 기반.

  python scripts/prices.py --data data --days 520

왜 pykrx 를 버렸나:
  GitHub Actions 러너에서 KRX 데이터 API 가 세션 없음(응답에 LOGOUT)으로 요청을
  거부한다. 2026-09-05 확인. DART 는 러너에서 정상 동작한다(status 000).

왜 재개 가능하게 만들었나:
  야후는 존재하지 않는 심볼 하나하나에도 재시도를 붙여 응답이 느리다. 상장사
  2,700종목의 .KS/.KQ 를 한 번에 판별하려다 45분 타임아웃에 걸렸다(2026-09-05).
  그래서 한 번 실행에서 다 끝내려 하지 않는다. 아래 세 가지로 나눠 저장한다.
    - 심볼 해석은 회당 --resolve-limit 종목까지만, 배치마다 캐시 저장
    - 백필은 종목 배치마다 parquet 에 병합 저장
    - --budget-min 을 넘기면 그 자리에서 저장하고 정상 종료(다음 실행이 이어받음)
  즉 첫 며칠은 매일 조금씩 채워지고, 다 채워지면 그 뒤로는 하루치 증분만 돈다.

산출물:
  close.parquet / volume.parquet  index=거래일, columns=6자리 티커 (float32)
  meta.json                       종목명, 20일 평균 거래대금, 마지막 갱신일
  symbols.json                    티커 -> 야후 심볼 해석 캐시 (빈 값 = 야후에 없음)
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

RESOLVE_BATCH = 50    # 심볼 판별용 배치 (없는 심볼이 많아 작게)
DATA_BATCH = 100      # 일봉 수집용 배치
PAUSE = 0.5
SUFFIXES = (".KS", ".KQ")

_T0 = time.monotonic()
_BUDGET = 1e9


def over_budget() -> bool:
    return (time.monotonic() - _T0) > _BUDGET


def elapsed() -> str:
    return f"{(time.monotonic() - _T0) / 60:.1f}분"


# ----------------------------------------------------------------------
def dart_listed() -> dict[str, str]:
    """DART 고유번호 파일에서 상장사 {6자리 티커: 종목명}."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dart_client import DartClient

    if not os.environ.get("DART_API_KEY", "").strip():
        raise SystemExit(
            "DART_API_KEY 가 필요합니다. 종목 리스트를 DART 에서 받기 때문입니다."
        )
    out: dict[str, str] = {}
    for r in DartClient().corp_codes():
        code = (r.get("stock_code") or "").strip()
        if len(code) == 6 and code.isdigit():
            out[code] = (r.get("corp_name") or "").strip() or code
    if not out:
        raise SystemExit("DART corpCode 에서 상장사를 찾지 못했습니다.")
    return out


def _yf():
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance 가 필요합니다: pip install yfinance")
    # 상장폐지·데이터없음 경고를 종목마다 뱉어 로그가 수천 줄로 불어난다.
    # 어차피 해당 종목은 symbols.json 에 빈 값으로 기록되므로 출력은 불필요하다.
    import logging
    for name in ("yfinance", "yfinance.utils", "peewee", "urllib3"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    logging.getLogger().setLevel(logging.ERROR)
    return yf


def _download(symbols, start, end):
    yf = _yf()
    raw = yf.download(symbols, start=start, end=end, auto_adjust=False,
                      progress=False, threads=True, group_by="column")
    if raw is None or len(raw) == 0:
        return pd.DataFrame(), pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        lv0 = raw.columns.get_level_values(0)
        close = raw["Close"] if "Close" in lv0 else pd.DataFrame()
        vol = raw["Volume"] if "Volume" in lv0 else pd.DataFrame()
    else:
        close = raw[["Close"]].rename(columns={"Close": symbols[0]})
        vol = raw[["Volume"]].rename(columns={"Volume": symbols[0]})
    return close, vol


def batches(items, size):
    for i in range(0, len(items), size):
        yield i // size + 1, (len(items) + size - 1) // size, items[i : i + size]


# ----------------------------------------------------------------------
def resolve_symbols(tickers, cache_path: Path, limit: int) -> dict[str, str]:
    """6자리 티커 -> 야후 심볼. 회당 limit 종목까지만 판별하고 배치마다 저장한다."""
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    unknown = [t for t in tickers if t not in cache]

    if unknown and not over_budget():
        todo = unknown[:limit]
        print(f"심볼 미판별 {len(unknown):,}종목 중 이번 회차 {len(todo):,}종목 처리")
        end = date.today()
        s = (end - timedelta(days=12)).isoformat()
        e = (end + timedelta(days=1)).isoformat()

        remaining = list(todo)
        for suf in SUFFIXES:
            if not remaining or over_budget():
                break
            print(f"  {suf} 시도: {len(remaining):,}종목")
            still = []
            for n, tot, chunk in batches(remaining, RESOLVE_BATCH):
                if over_budget():
                    still.extend(chunk)
                    continue
                try:
                    close, _ = _download([t + suf for t in chunk], s, e)
                except Exception as exc:
                    print(f"    배치 {n}/{tot} 실패: {exc}", file=sys.stderr)
                    still.extend(chunk)
                    continue
                hit = {str(c).split(".")[0] for c in close.columns
                       if close[c].notna().any()} if len(close.columns) else set()
                for t in chunk:
                    if t in hit:
                        cache[t] = t + suf
                    else:
                        still.append(t)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                print(f"    배치 {n}/{tot} 확인 {len(hit)}종목 (누적 {elapsed()})", flush=True)
                time.sleep(PAUSE)
            remaining = still

        # 두 접미사 모두 실패 = 야후에 없는 종목. 빈 값으로 못박아 재시도 방지.
        # 단 예산 초과로 못 본 것은 캐시에 넣지 않고 다음 회차로 넘긴다.
        if not over_budget():
            for t in remaining:
                cache[t] = ""
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

        left = len([t for t in tickers if t not in cache])
        if left:
            print(f"  아직 미판별 {left:,}종목 — 다음 실행에서 이어서 처리합니다")

    return {t: cache[t] for t in tickers if cache.get(t)}


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


def merge(old, new):
    if new is None or len(new) == 0:
        return old
    if old is None:
        return new.sort_index()
    return pd.concat([old, new]).groupby(level=0).last().sort_index()


def collect(symbols, rev, start, end, close, volume, close_p, vol_p, label):
    """심볼 배치마다 받아서 즉시 병합 저장. 예산 초과 시 중단(진행분은 남는다)."""
    done = 0
    for n, tot, chunk in batches(symbols, DATA_BATCH):
        if over_budget():
            print(f"  시간 예산 초과 — {label} 중단 ({done:,}/{len(symbols):,}종목 완료)")
            break
        try:
            c, v = _download(chunk, start, end)
        except Exception as exc:
            print(f"  배치 {n}/{tot} 실패: {exc}", file=sys.stderr)
            continue
        if len(c.columns) == 0:
            continue
        c = c.rename(columns=lambda x: rev.get(str(x), str(x))).dropna(how="all")
        v = v.rename(columns=lambda x: rev.get(str(x), str(x))).dropna(how="all")
        c.index = pd.to_datetime(c.index).tz_localize(None)
        v.index = pd.to_datetime(v.index).tz_localize(None)
        close, volume = merge(close, c), merge(volume, v)
        save(close, close_p)
        save(volume, vol_p)
        done += len(chunk)
        print(f"  {label} 배치 {n}/{tot} ({close.shape[1]:,}종목 누적, {elapsed()})", flush=True)
        time.sleep(PAUSE)
    return close, volume


def write_meta(data_dir: Path, names, close, volume, last_day: str) -> None:
    turnover = (close * volume.reindex_like(close)).tail(20).mean()
    meta = {}
    for t in close.columns:
        v = turnover.get(t)
        meta[str(t)] = {"name": names.get(str(t), str(t)), "cap": 0,
                        "value": int(v) if pd.notna(v) else 0}
    (data_dir / "meta.json").write_text(
        json.dumps({"as_of": last_day, "tickers": meta}, ensure_ascii=False),
        encoding="utf-8")
    print(f"  meta.json: {len(meta):,}종목")


# ----------------------------------------------------------------------
def main() -> int:
    global _BUDGET
    ap = argparse.ArgumentParser(description="전종목 일봉 수집 (yfinance, 재개 가능)")
    ap.add_argument("--data", default="data")
    ap.add_argument("--days", type=int, default=520)
    ap.add_argument("--end", default=None, help="YYYYMMDD (기본: 오늘)")
    ap.add_argument("--resolve-limit", type=int, default=600,
                    help="한 회차에 판별할 미확인 종목 수")
    ap.add_argument("--budget-min", type=float, default=32,
                    help="이 시간을 넘기면 저장하고 정상 종료 (워크플로 타임아웃보다 작게)")
    args = ap.parse_args()
    _BUDGET = args.budget_min * 60

    data_dir = Path(args.data)
    close_p, vol_p = data_dir / "close.parquet", data_dir / "volume.parquet"
    end = datetime.strptime(args.end, "%Y%m%d").date() if args.end else date.today()
    close, volume = load(close_p), load(vol_p)

    names = dart_listed()
    print(f"DART 상장사: {len(names):,}종목")
    symbols = resolve_symbols(sorted(names), data_dir / "symbols.json", args.resolve_limit)
    print(f"야후 심볼 확보: {len(symbols):,}종목 ({elapsed()} 경과)")
    if not symbols:
        print("아직 판별된 심볼이 없습니다. 다음 실행에서 이어집니다.")
        return 0

    rev = {v: k for k, v in symbols.items()}
    have = set(close.columns) if close is not None else set()

    # 1) 신규 종목 전체 백필
    fresh = sorted(symbols[t] for t in symbols if t not in have)
    if fresh:
        s = (end - timedelta(days=int(args.days * 1.55))).isoformat()
        print(f"신규 백필 {len(fresh):,}종목 ({s} ~ {end})")
        close, volume = collect(fresh, rev, s, (end + timedelta(days=1)).isoformat(),
                                close, volume, close_p, vol_p, "백필")

    # 2) 기존 종목 증분
    if have and close is not None and not over_budget():
        start = (close.index.max() + pd.Timedelta(days=1)).date()
        if start <= end:
            old = sorted(symbols[t] for t in symbols if t in have)
            print(f"증분 {len(old):,}종목 ({start} ~ {end})")
            close, volume = collect(old, rev, start.isoformat(),
                                    (end + timedelta(days=1)).isoformat(),
                                    close, volume, close_p, vol_p, "증분")
        else:
            print("증분: 갱신할 날짜가 없습니다 (휴장 또는 이미 최신)")

    if close is None or len(close) == 0:
        print("저장된 데이터가 없습니다.")
        return 1

    keep = max(args.days, 470)
    close = close.sort_index().tail(keep)
    volume = volume.sort_index().tail(keep).reindex(columns=close.columns)
    save(close, close_p)
    save(volume, vol_p)
    last = close.index.max().strftime("%Y%m%d")
    print(f"저장: {close.shape[0]}거래일 × {close.shape[1]:,}종목 (최종 {last}, {elapsed()})")
    write_meta(data_dir, names, close, volume, last)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
