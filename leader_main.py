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
import re
import sys
import time
from datetime import date

from leader_screener import judge_test, load_config, resolve_base_date, screen
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
            "윗꼬리비율": {"number": r["윗꼬리비율"]},
            "장중최고등락률": {"number": r["장중최고등락률"]},
            "테스트기준가": {"number": r["테스트기준가"]},
            # 노션 다중선택 값에는 쉼표를 쓸 수 없다
            "테마": {"multi_select": [{"name": t.replace(",", "·")[:100]} for t in r["테마"]]},
            "메모": text(r["메모"][:2000]),
            "레코드키": text(r["레코드키"]),
        }
        props["신고가구분"] = (
            {"select": {"name": r["신고가구분"]}} if r["신고가구분"] else {"select": None}
        )
        props["물량테스트"] = (
            {"select": {"name": r["물량테스트"]}} if r["물량테스트"] else {"select": None}
        )
        for name, url in _stock_urls(r["종목코드"]).items():
            if name in ("종목페이지", "차트", "뉴스"):
                props[name] = {"url": url}
        return props

    def create_defaults(self) -> dict:
        """새로 만드는 행에만 '판단=관찰'을 넣는다(직접 바꾼 판단은 덮어쓰지 않음)."""
        return {"판단": {"select": {"name": "관찰"}}}

    def pending_tests(self, before_iso: str) -> list[dict]:
        """물량테스트 '의심'인데 아직 판정이 비어 있는, 기준일 이전 행들."""
        payload = {
            "filter": {
                "and": [
                    {"property": "물량테스트", "select": {"equals": "의심"}},
                    {"property": "테스트판정", "select": {"is_empty": True}},
                    {"property": "기준일", "date": {"before": before_iso}},
                ]
            },
            "page_size": 100,
        }
        data = self._request("POST", f"/databases/{self.database_id}/query", json=payload)
        rows = []
        for page in data.get("results", []):
            p = page["properties"]
            code = "".join(t["plain_text"] for t in p["종목코드"]["rich_text"])
            ref = p["테스트기준가"]["number"]
            day = (p["기준일"]["date"] or {}).get("start")
            if code and ref and day:
                rows.append({"page_id": page["id"], "종목코드": code, "기준가": ref, "기준일": day})
        return rows

    def write_verdict(self, page_id: str, v: dict) -> None:
        self._request(
            "PATCH",
            f"/pages/{page_id}",
            json={
                "properties": {
                    "테스트판정": {"select": {"name": v["테스트판정"]}},
                    "익일등락률": {"number": v["익일등락률"]},
                }
            },
        )


def _judge_pending(syncer: LeaderNotionSync, base: str) -> dict[str, int]:
    """앞선 날의 물량테스트 의심 종목을 다음 거래일 종가로 판정한다."""
    base_iso = f"{base[:4]}-{base[4:6]}-{base[6:]}"
    counts = {"지지": 0, "이탈": 0, "대기": 0, "failed": 0}
    try:
        rows = syncer.pending_tests(base_iso)
    except Exception as exc:
        log.error("판정 대상 조회 실패: %s", exc)
        return counts
    for row in rows:
        try:
            v = judge_test(row["종목코드"], row["기준일"], row["기준가"], base)
            if v is None:
                counts["대기"] += 1
                continue
            syncer.write_verdict(row["page_id"], v)
            counts[v["테스트판정"]] += 1
            log.info("물량테스트 판정 %s(%s): %s, 익일 %s%%",
                     row["종목코드"], row["기준일"], v["테스트판정"], v["익일등락률"])
        except Exception as exc:
            log.error("판정 실패 %s: %s", row["종목코드"], exc)
            counts["failed"] += 1
    return counts


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
            if not counts["failed"]:
                gh_annotate("error", f"노션 적재 실패 예시 {r['종목명']}: {str(exc)[:300]}")
            counts["failed"] += 1
    return counts


def gh_annotate(level: str, msg: str) -> None:
    """GitHub Actions 실행 요약 화면의 '주석'에 메시지를 띄운다(로그를 안 열어도 보이게)."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level} title=주도주 스크리닝::{msg.replace(chr(10), ' ')}", flush=True)


def parse_date(text: str | None) -> date | None:
    """2026-10-07, 20261007, 2026.10.07, 2026/10/7, 10/7 처럼 흔한 입력을 모두 받는다."""
    if not text or not text.strip():
        return None
    nums = re.findall(r"\d+", text)
    if len(nums) == 1 and len(nums[0]) == 8:
        y, m, d = int(nums[0][:4]), int(nums[0][4:6]), int(nums[0][6:])
    elif len(nums) == 3:
        y, m, d = (int(n) for n in nums)
    elif len(nums) == 2:  # 연도 생략
        y, (m, d) = date.today().year, (int(n) for n in nums)
    else:
        raise ValueError(f"날짜를 읽을 수 없다: '{text}' (예: 2026-10-07)")
    return date(y, m, d)


def resolve_with_retry(target: date | None) -> str:
    """KRX 첫 접속이 일시적으로 실패하는 경우가 있어 몇 번 다시 시도한다."""
    last = None
    for attempt in range(4):
        try:
            return resolve_base_date(target)
        except Exception as exc:
            last = exc
            log.warning("기준일 조회 실패(%d/4): %s", attempt + 1, exc)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"KRX 기준일 조회 실패: {last}")


def main() -> int:
    try:
        return run()
    except Exception as exc:
        log.exception("실행 실패")
        gh_annotate("error", f"{type(exc).__name__}: {exc}")
        return 1


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="기준일 YYYY-MM-DD. 비우면 가장 최근 영업일")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-themes", action="store_true")
    args = parser.parse_args()

    target = parse_date(args.date)
    base = resolve_with_retry(target)
    if target and base != target.strftime("%Y%m%d"):
        log.info("%s는 휴장일이라 직전 영업일 %s 기준으로 수집한다.", target, base)

    cfg = load_config()
    records = screen(base, cfg, use_themes=not args.no_themes)

    if args.dry_run:
        for r in records:
            log.info(
                "[%s] %-12s %6.2f%%(장중 %s%%)  %8.1f억(%d위)  테마동반%d  %s  "
                "몸통%s 윗꼬리%s  %s | %s",
                r["단계"], r["종목명"], r["등락률"], r["장중최고등락률"], r["거래대금"],
                r["거래대금순위"], r["테마동반상승"], r["신고가구분"], r["몸통비율"],
                r["윗꼬리비율"], r["물량테스트"] or "", r["메모"],
            )
        return 0

    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_LEADER_DB_ID")
    if not token or not db_id:
        log.error("NOTION_TOKEN 또는 NOTION_LEADER_DB_ID 환경변수가 없다.")
        return 1
    syncer = LeaderNotionSync(token, db_id)

    failed = 0
    if records:
        counts = _sync(syncer, records)
        log.info("%s 적재 결과: %s", base, counts)
        failed += counts["failed"]
    else:
        log.info("%s: 조건에 맞는 종목이 없다.", base)

    verdicts = _judge_pending(syncer, base)
    log.info("물량테스트 판정 결과: %s", verdicts)
    failed += verdicts["failed"]

    source = records[0]["메모"].split("테마출처: ")[-1] if records else "-"
    summary = (
        f"{base} 끼 {len(records)}종목 · 주도주 "
        f"{sum(r['단계'] == '2차 주도주' for r in records)} · 물량테스트 의심 "
        f"{sum(r['물량테스트'] == '의심' for r in records)} · 테마출처 {source} · "
        f"판정 지지 {verdicts['지지']}/이탈 {verdicts['이탈']}"
    )
    gh_annotate("notice", summary)
    if failed:
        gh_annotate("error", f"노션 적재/판정 실패 {failed}건 (로그의 '적재 실패' 줄 참고)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
