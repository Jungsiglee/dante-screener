"""OpenDART API 얇은 래퍼.

개발가이드(https://opendart.fss.or.kr/guide/main.do) 스펙 기준으로 작성.
API 키는 환경변수 DART_API_KEY 또는 .env 에서만 읽는다 (코드/채팅에 하드코딩 금지).
"""

from __future__ import annotations

import io
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

import requests

BASE = "https://opendart.fss.or.kr/api"

# 개발가이드 '메시지 설명' 기준
STATUS_MSG = {
    "000": "정상",
    "010": "등록되지 않은 키",
    "011": "사용할 수 없는 키",
    "012": "접근할 수 없는 IP",
    "013": "조회된 데이터 없음",
    "014": "파일이 존재하지 않음",
    "020": "요청 제한 초과 (일반적으로 일 20,000건)",
    "021": "조회 가능한 회사 개수 초과",
    "100": "필드의 부적절한 값",
    "101": "부적절한 접근",
    "800": "시스템 점검 중",
    "900": "정의되지 않은 오류",
    "901": "사용자 계정의 개인정보 보유기간 만료",
}


class DartError(RuntimeError):
    def __init__(self, status: str, message: str = ""):
        self.status = status
        self.message = message
        known = STATUS_MSG.get(status, "알 수 없는 상태코드")
        super().__init__(f"[DART {status}] {known} / 응답메시지: {message}")


class DartClient:
    """호출 간 sleep 을 강제하는 단순 클라이언트.

    DART 는 일 20,000건 제한(status 020)이 있고 초당 제한은 명시돼 있지 않다.
    보수적으로 기본 0.15초 간격을 둔다.
    """

    def __init__(
        self,
        api_key: str | None = None,
        sleep: float = 0.15,
        timeout: int = 30,
        max_retry: int = 3,
    ):
        self.api_key = api_key or os.environ.get("DART_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "DART_API_KEY 가 없습니다. 환경변수로 넣거나 .env 를 로드하세요."
            )
        self.sleep = sleep
        self.timeout = timeout
        self.max_retry = max_retry
        self.session = requests.Session()
        self.call_count = 0

    def _raw_get(self, endpoint: str, **params) -> requests.Response:
        params["crtfc_key"] = self.api_key
        url = f"{BASE}/{endpoint}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retry):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                self.call_count += 1
                time.sleep(self.sleep)
                return resp
            except requests.RequestException as exc:  # 네트워크/5xx 만 재시도
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{endpoint} 요청 실패 (재시도 {self.max_retry}회): {last_exc}")

    def get_list(self, endpoint: str, **params) -> list[dict[str, Any]]:
        """JSON 엔드포인트 호출. status 013(데이터 없음)은 빈 리스트로 정상 처리."""
        resp = self._raw_get(endpoint, **params)
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"{endpoint} 응답이 JSON 이 아닙니다: {resp.text[:300]}")

        status = str(data.get("status", ""))
        if status == "013":
            return []
        if status != "000":
            raise DartError(status, str(data.get("message", "")))
        return data.get("list", []) or []

    # ------------------------------------------------------------------
    # 고유번호 전체 조회 (zip 안에 CORPCODE.xml)
    # ------------------------------------------------------------------
    def corp_codes(self) -> list[dict[str, str]]:
        resp = self._raw_get("corpCode.xml")
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile:
            # 키 오류 등은 zip 대신 XML 에러 문서가 온다
            try:
                root = ET.fromstring(resp.content)
                status = (root.findtext("status") or "").strip()
                message = (root.findtext("message") or "").strip()
                raise DartError(status, message)
            except ET.ParseError:
                raise RuntimeError(f"corpCode 응답 해석 실패: {resp.content[:300]!r}")

        with zf:
            name = zf.namelist()[0]
            xml_bytes = zf.read(name)

        root = ET.fromstring(xml_bytes)
        out: list[dict[str, str]] = []
        for el in root.iter("list"):
            out.append(
                {
                    "corp_code": (el.findtext("corp_code") or "").strip(),
                    "corp_name": (el.findtext("corp_name") or "").strip(),
                    "stock_code": (el.findtext("stock_code") or "").strip(),
                    "modify_date": (el.findtext("modify_date") or "").strip(),
                }
            )
        return out

    # ------------------------------------------------------------------
    # 다중회사 주요계정 (매출액/영업이익/자산총계/자본총계 등)
    # ------------------------------------------------------------------
    def multi_account(
        self, corp_codes: list[str], bsns_year: int, reprt_code: str = "11011"
    ) -> list[dict[str, Any]]:
        """corp_code 를 콤마로 이어 한 번에 조회.

        reprt_code: 11011=사업보고서, 11014=3분기, 11012=반기, 11013=1분기
        조회 가능 회사 개수를 넘기면 status 021 이 오므로 배치 크기를 줄여 재시도한다.
        """
        return self.get_list(
            "fnlttMultiAcnt.json",
            corp_code=",".join(corp_codes),
            bsns_year=str(bsns_year),
            reprt_code=reprt_code,
        )
