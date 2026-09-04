"""
수집한 레코드를 노션 '주간 수급 데이터' DB에 적재한다.

레코드키로 먼저 조회해서 있으면 갱신, 없으면 생성한다(upsert).
같은 주를 몇 번 돌려도 행이 중복되지 않는다.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 노션 API는 초당 약 3회로 제한된다. 여유를 두고 호출 간격을 준다.
THROTTLE_SEC = 0.4

# 종목별 참고 URL 베이스. 종목코드만 있으면 조립되므로 캐싱 불필요.
NAVER_ITEM_BASE = "https://finance.naver.com/item"
NAVER_RESEARCH_BASE = "https://finance.naver.com/research"


def _stock_urls(ticker: str) -> dict[str, str]:
    """종목코드로 네이버 증권 관련 URL 4종을 조립한다.

    노션 DB의 URL 열 이름과 정확히 일치해야 한다.
    """
    return {
        "종목페이지": f"{NAVER_ITEM_BASE}/main.naver?code={ticker}",
        "차트": f"{NAVER_ITEM_BASE}/fchart.naver?code={ticker}",
        "뉴스": f"{NAVER_ITEM_BASE}/news.naver?code={ticker}",
        "리포트": f"{NAVER_RESEARCH_BASE}/company_list.naver?searchType=itemCode&itemCode={ticker}",
    }


class NotionSync:
    def __init__(self, token: str, database_id: str):
        self.database_id = database_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    # --- 내부 유틸 -----------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(4):
            resp = self.session.request(method, f"{API}{path}", timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2))
                log.warning("레이트 리밋. %d초 대기 후 재시도", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if not resp.ok:
                raise RuntimeError(f"{method} {path} -> {resp.status_code} {resp.text}")
            time.sleep(THROTTLE_SEC)
            return resp.json()
        raise RuntimeError(f"{method} {path} 재시도 초과")

    @staticmethod
    def _properties(record: dict) -> dict:
        """수집 레코드를 노션 property 형식으로 변환."""
        props = {
            "종목명": {"title": [{"text": {"content": record["종목명"]}}]},
            "종목코드": {"rich_text": [{"text": {"content": record["종목코드"]}}]},
            "시장": {"select": {"name": record["시장"]}},
            "투자주체": {"select": {"name": record["투자주체"]}},
            "주차": {"rich_text": [{"text": {"content": record["주차"]}}]},
            "기준주간": {
                "date": {
                    "start": record["기준주간_시작"],
                    "end": record["기준주간_종료"],
                }
            },
            "순매수금액": {"number": record["순매수금액"]},
            "순위": {"number": record["순위"]},
            "레코드키": {"rich_text": [{"text": {"content": record["레코드키"]}}]},
        }
        # 값이 없을 수 있는 항목은 None으로 넣어 노션에서 비워둔다
        props["연속순매수일"] = {"number": record.get("연속순매수일")}
        props["주간등락률"] = {"number": record.get("주간등락률")}
        if record.get("섹터"):
            props["섹터"] = {
                "multi_select": [{"name": s} for s in record["섹터"]]
            }
        # 종목별 참고 URL 4개 (네이버 증권 도메인)
        for name, url in _stock_urls(record["종목코드"]).items():
            props[name] = {"url": url}
        return props

    # --- 공개 메서드 ---------------------------------------------------

    def find_by_key(self, record_key: str) -> str | None:
        """레코드키로 기존 페이지 ID를 찾는다."""
        payload = {
            "filter": {
                "property": "레코드키",
                "rich_text": {"equals": record_key},
            },
            "page_size": 1,
        }
        data = self._request(
            "POST", f"/databases/{self.database_id}/query", json=payload
        )
        results = data.get("results", [])
        return results[0]["id"] if results else None

    def upsert(self, record: dict) -> str:
        """있으면 갱신, 없으면 생성. 'created' 또는 'updated'를 반환."""
        props = self._properties(record)
        page_id = self.find_by_key(record["레코드키"])
        if page_id:
            self._request("PATCH", f"/pages/{page_id}", json={"properties": props})
            return "updated"
        self._request(
            "POST",
            "/pages",
            json={
                "parent": {"database_id": self.database_id},
                "properties": props,
            },
        )
        return "created"

    def sync(self, records: list[dict]) -> dict[str, int]:
        counts = {"created": 0, "updated": 0, "failed": 0}
        for record in records:
            try:
                counts[self.upsert(record)] += 1
            except Exception as exc:
                log.error("적재 실패 %s: %s", record["레코드키"], exc)
                counts["failed"] += 1
        return counts
