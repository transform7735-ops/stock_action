"""
주간 수급 데이터 수집 -> 노션 적재.

사용법:
    python main.py                      # 가장 최근에 끝난 주
    python main.py --week 2026-W35      # 특정 주차 소급 수집
    python main.py --weeks 2026-W30 2026-W35   # 구간 일괄 수집
    python main.py --dry-run            # 노션에 쓰지 않고 결과만 출력
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta

from krx_collector import (
    Week,
    collect,
    last_completed_week,
    load_sector_map,
    week_from_label,
)
from notion_sync import NotionSync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def week_range(start_label: str, end_label: str) -> list[Week]:
    start, end = week_from_label(start_label), week_from_label(end_label)
    weeks, cursor = [], start.monday
    while cursor <= end.monday:
        weeks.append(Week(monday=cursor, friday=cursor + timedelta(days=4)))
        cursor += timedelta(days=7)
    return weeks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", help="수집할 주차. 예: 2026-W35")
    parser.add_argument(
        "--weeks", nargs=2, metavar=("START", "END"), help="주차 구간 일괄 수집"
    )
    parser.add_argument("--top", type=int, default=10, help="투자주체별 상위 N종목")
    parser.add_argument("--no-streak", action="store_true", help="연속순매수일 계산 생략")
    parser.add_argument("--dry-run", action="store_true", help="노션에 쓰지 않는다")
    args = parser.parse_args()

    if args.weeks:
        weeks = week_range(*args.weeks)
    elif args.week:
        weeks = [week_from_label(args.week)]
    else:
        weeks = [last_completed_week()]

    sector_map = load_sector_map()

    syncer = None
    if not args.dry_run:
        token = os.environ.get("NOTION_TOKEN")
        db_id = os.environ.get("NOTION_DATABASE_ID")
        if not token or not db_id:
            log.error("NOTION_TOKEN 또는 NOTION_DATABASE_ID 환경변수가 없다.")
            return 1
        syncer = NotionSync(token, db_id)

    total = {"created": 0, "updated": 0, "failed": 0}
    for week in weeks:
        log.info("=== %s 수집 시작 ===", week)
        records = collect(
            week,
            top_n=args.top,
            with_streak=not args.no_streak,
            sector_map=sector_map,
        )
        if not records:
            log.warning("%s: 수집된 레코드가 없다.", week.label)
            continue

        if args.dry_run:
            for r in records:
                log.info(
                    "%s %s %-14s %8.1f억  순위%2d  연속%s일  %s%%",
                    r["주차"], r["투자주체"], r["종목명"], r["순매수금액"],
                    r["순위"], r["연속순매수일"], r["주간등락률"],
                )
            continue

        counts = syncer.sync(records)
        log.info("%s 적재 결과: %s", week.label, counts)
        for k in total:
            total[k] += counts[k]

    if not args.dry_run:
        log.info("전체 결과: %s", total)
        # 실패가 있으면 워크플로를 실패로 표시해 알림을 받는다
        if total["failed"]:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
