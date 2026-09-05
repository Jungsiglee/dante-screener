"""DART 재무 DB 구축.

  python build_db.py --years 2022 2023 2024 2025 --db fin.db

1) corpCode.xml 전체 받아서 stock_code 가 있는 상장사만 corps 테이블에 적재
   (V5 의 dart_service.py 가 쓰던 '전체 공시 3페이지 훑어 필터링' 방식을 대체)
2) fnlttMultiAcnt 로 연도별 주요계정을 배치 수집해 financials 테이블에 적재
   - rcept_no 앞 8자리가 접수일(YYYYMMDD) -> 백테스트 look-ahead 방지에 사용
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from dart_client import DartClient, DartError

# fnlttMultiAcnt 의 account_nm 은 회사마다 표기가 다르다. 정규화 매핑.
ACCOUNT_ALIASES: dict[str, str] = {
    "매출액": "revenue",
    "수익(매출액)": "revenue",
    "영업수익": "revenue",  # 금융/지주사
    "영업이익": "operating_profit",
    "영업이익(손실)": "operating_profit",
    "당기순이익": "net_income",
    "당기순이익(손실)": "net_income",
    "자산총계": "total_assets",
    "부채총계": "total_liabilities",
    "자본총계": "total_equity",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS corps (
    corp_code   TEXT PRIMARY KEY,
    corp_name   TEXT NOT NULL,
    stock_code  TEXT NOT NULL,
    modify_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_corps_stock ON corps(stock_code);

CREATE TABLE IF NOT EXISTS financials (
    corp_code  TEXT NOT NULL,
    bsns_year  INTEGER NOT NULL,
    reprt_code TEXT NOT NULL,
    fs_div     TEXT NOT NULL,        -- CFS(연결) / OFS(별도)
    metric     TEXT NOT NULL,        -- revenue / operating_profit / ...
    amount     INTEGER,              -- 원 단위
    rcept_no   TEXT,
    rcept_dt   TEXT,                 -- rcept_no[:8], YYYYMMDD
    PRIMARY KEY (corp_code, bsns_year, reprt_code, fs_div, metric)
);
CREATE INDEX IF NOT EXISTS idx_fin_year ON financials(bsns_year, metric);

CREATE TABLE IF NOT EXISTS fetch_log (
    bsns_year  INTEGER,
    reprt_code TEXT,
    corp_code  TEXT,
    ok         INTEGER,
    PRIMARY KEY (bsns_year, reprt_code, corp_code)
);
"""


def parse_amount(raw) -> int | None:
    """'1,234,567' / '-1,234' / '' / '-' 를 int 로."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "N/A"):
        return None
    # 괄호 음수 표기 (1,234) 대응
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    val = int(float(s))
    return -val if neg else val


def is_ordinary_share(stock_code: str) -> bool:
    """우선주/신주인수권 제외. 보통주는 종목코드 끝자리가 0."""
    return len(stock_code) == 6 and stock_code.endswith("0")


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def load_corps(conn: sqlite3.Connection, client: DartClient, include_spac: bool) -> list[str]:
    rows = client.corp_codes()
    listed = []
    for r in rows:
        sc = r["stock_code"]
        if not sc or len(sc) != 6:
            continue
        if not is_ordinary_share(sc):
            continue
        if not include_spac and "스팩" in r["corp_name"]:
            continue
        listed.append(r)

    conn.executemany(
        "INSERT OR REPLACE INTO corps VALUES (?,?,?,?)",
        [(r["corp_code"], r["corp_name"], r["stock_code"], r["modify_date"]) for r in listed],
    )
    conn.commit()
    print(f"  corps 적재: {len(listed):,}개 (전체 {len(rows):,}건 중 상장 보통주)")
    return [r["corp_code"] for r in listed]


def fetch_year(
    conn: sqlite3.Connection,
    client: DartClient,
    codes: list[str],
    year: int,
    reprt_code: str,
    batch: int,
    resume: bool,
) -> None:
    if resume:
        done = {
            row[0]
            for row in conn.execute(
                "SELECT corp_code FROM fetch_log WHERE bsns_year=? AND reprt_code=?",
                (year, reprt_code),
            )
        }
        codes = [c for c in codes if c not in done]
        if not codes:
            print(f"  {year} {reprt_code}: 이미 완료, 건너뜀")
            return

    total = len(codes)
    stored = 0
    for i in range(0, total, batch):
        chunk = codes[i : i + batch]
        rows = _multi_with_backoff(client, chunk, year, reprt_code)
        stored += _store(conn, rows, year, reprt_code)
        conn.executemany(
            "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,1)",
            [(year, reprt_code, c) for c in chunk],
        )
        conn.commit()
        done_n = min(i + batch, total)
        print(
            f"\r  {year} {reprt_code}: {done_n:,}/{total:,}개 조회, "
            f"{stored:,}행 저장 (API {client.call_count:,}콜)",
            end="",
            flush=True,
        )
    print()


def _multi_with_backoff(client: DartClient, chunk: list[str], year: int, reprt_code: str):
    """status 021(회사 개수 초과)이면 배치를 반으로 쪼개 재시도."""
    try:
        return client.multi_account(chunk, year, reprt_code)
    except DartError as e:
        if e.status == "021" and len(chunk) > 1:
            mid = len(chunk) // 2
            return _multi_with_backoff(client, chunk[:mid], year, reprt_code) + _multi_with_backoff(
                client, chunk[mid:], year, reprt_code
            )
        if e.status == "020":
            raise SystemExit(
                "일일 요청 한도(20,000건)를 초과했습니다. --resume 으로 내일 이어서 실행하세요."
            )
        if e.status in ("013",):
            return []
        raise


def _store(conn: sqlite3.Connection, rows: list[dict], year: int, reprt_code: str) -> int:
    payload = []
    for r in rows:
        metric = ACCOUNT_ALIASES.get((r.get("account_nm") or "").strip())
        if metric is None:
            continue
        amount = parse_amount(r.get("thstrm_amount"))
        rcept_no = (r.get("rcept_no") or "").strip()
        payload.append(
            (
                r.get("corp_code"),
                year,
                reprt_code,
                (r.get("fs_div") or "").strip() or "UNK",
                metric,
                amount,
                rcept_no,
                rcept_no[:8] if len(rcept_no) >= 8 else None,
            )
        )
    conn.executemany("INSERT OR REPLACE INTO financials VALUES (?,?,?,?,?,?,?,?)", payload)
    return len(payload)


def main() -> int:
    ap = argparse.ArgumentParser(description="DART 재무 DB 구축")
    ap.add_argument("--years", type=int, nargs="+", required=True, help="사업연도 (예: 2022 2023 2024 2025)")
    ap.add_argument("--db", default="fin.db")
    ap.add_argument("--reprt-code", default="11011", help="11011=사업보고서")
    ap.add_argument("--batch", type=int, default=50, help="한 번에 조회할 회사 수")
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--include-spac", action="store_true")
    ap.add_argument("--skip-corps", action="store_true", help="corps 테이블 갱신 생략")
    ap.add_argument("--resume", action="store_true", help="fetch_log 기준으로 이어서 수집")
    args = ap.parse_args()

    client = DartClient(sleep=args.sleep)
    conn = open_db(args.db)
    print(f"DB: {Path(args.db).resolve()}")

    if args.skip_corps:
        codes = [row[0] for row in conn.execute("SELECT corp_code FROM corps ORDER BY corp_code")]
        print(f"  corps 재사용: {len(codes):,}개")
    else:
        print("[1/2] corpCode.xml 수집")
        codes = load_corps(conn, client, args.include_spac)

    if not codes:
        print("상장사 목록이 비어 있습니다.", file=sys.stderr)
        return 1

    print(f"[2/2] 주요계정 수집 ({len(args.years)}개 연도)")
    for year in sorted(args.years):
        fetch_year(conn, client, codes, year, args.reprt_code, args.batch, args.resume)

    n = conn.execute("SELECT COUNT(*) FROM financials").fetchone()[0]
    print(f"\n완료. financials {n:,}행, API 호출 {client.call_count:,}건")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
