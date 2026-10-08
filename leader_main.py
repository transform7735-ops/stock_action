"""
홍인기식 일일 스크리닝 -> 노션 '주도주 스크리닝 (홍인기 단타)' DB 적재.

사용법:
    python leader_main.py                    # 가장 최근 영업일
    python leader_main.py --date 2026-10-07  # 특정일 소급
    python leader_main.py --dry-run          # 노션에 쓰지 않고 결과만 출력
    python leader_main.py --no-themes        # 네이버 테마 수집 생략
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from leader_screener import load_config, resolve_base_date, screen
from notion_sync import NotionSync, _stock_urls

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("leader")


class LeaderNotionSync(NotionSync):
    """주도주 DB 컬럼 구조에 맞춘 적재기. upsert 로직은 부모 것을 그대로 쓴다."""

    @staticmethod
    def _properties(r: dict) -> dict:
        def text(v):
            return {"rich_text": [{"text": {"content": v}}]}

        props = {
            "종목명": {"title": [{"text": {"content": r["종목명"]}}]},
            "종목코드": text(r["종목코드"]),
            "시장": {"select": {"name": r["시장"]}},
            "기준일": {"date": {"start": r["기준일"]}},
            "단계": {"select": {"name": r["단계"]}},
            "종가": {"number": r["종가"]},
            "등락률": {"number": r["등락률"]},
            "거래대금": {"number": r["거래대금"]},
            "거래대금순위": {"number": r["거래대금순위"]},
            "테마동반상승": {"number": r["테마동반상승"]},
            "테마내순위": {"number": r["테마내순위"]},
            "대장주": {"checkbox": r["대장주"]},
            "첫돌파": {"checkbox": r["첫돌파"]},
            "몸통비율": {"number": r["몸통비율"]},
            "테마": {"multi_select": [{"name": t[:100]} for t in r["테마"]]},
            "메모": text(r["메모"]),
            "레코드키": text(r["레코드키"]),
        }
        props["신고가구분"] = (
            {"select": {"name": r["신고가구분"]}} if r["신고가구분"] else {"select": None}
        )
        for name, url in _stock_urls(r["종목코드"]).items():
            if name in ("종목페이지", "차트", "뉴스"):
                props[name] = {"url": url}
        return props

    def create_defaults(self) -> dict:
        """새로 만드는 행에만 '판단=관찰'을 넣는다(직접 바꾼 판단은 덮어쓰지 않음)."""
        return {"판단": {"select": {"name": "관찰"}}}


def _sync(syncer: LeaderNotionSync, records: list[dict]) -> dict[str, int]:
    counts = {"created": 0, "updated": 0, "failed": 0}
    for r in records:
        try:
            props = syncer._properties(r)
            page_id = syncer.find_by_key(r["레코드키"])
            if page_id:
                syncer._request("PATCH", f"/pages/{page_id}", json={"properties": props})
                counts["updated"] += 1
            else:
                props.update(syncer.create_defaults())
                syncer._request(
                    "POST",
                    "/pages",
                    json={"parent": {"database_id": syncer.database_id}, "properties": props},
                )
                counts["created"] += 1
        except Exception as exc:
            log.error("적재 실패 %s: %s", r["레코드키"], exc)
            counts["failed"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="기준일 YYYY-MM-DD. 비우면 가장 최근 영업일")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-themes", action="store_true")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    base = resolve_base_date(target)
    if target and base != target.strftime("%Y%m%d"):
        log.info("%s는 휴장일이라 직전 영업일 %s 기준으로 수집한다.", target, base)

    cfg = load_config()
    records = screen(base, cfg, use_themes=not args.no_themes)
    if not records:
        log.info("%s: 조건에 맞는 종목이 없다. 적재할 것 없음.", base)
        return 0

    if args.dry_run:
        for r in records:
            log.info(
                "[%s] %-12s %6.2f%%  %8.1f억(%d위)  테마동반%d  %s  몸통%s  | %s",
                r["단계"], r["종목명"], r["등락률"], r["거래대금"], r["거래대금순위"],
                r["테마동반상승"], r["신고가구분"], r["몸통비율"], r["메모"],
            )
        return 0

    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_LEADER_DB_ID")
    if not token or not db_id:
        log.error("NOTION_TOKEN 또는 NOTION_LEADER_DB_ID 환경변수가 없다.")
        return 1

    counts = _sync(LeaderNotionSync(token, db_id), records)
    log.info("%s 적재 결과: %s", base, counts)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
