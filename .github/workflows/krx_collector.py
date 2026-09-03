"""
KRX 주간 투자자별 순매수 데이터 수집.

pykrx를 통해 KRX 정보데이터시스템의 공개 데이터를 조회한다.
- 주간 누적 순매수 상위 종목 (투자주체별, 시장별)
- 주간 등락률
- 연속 순매수일 (일별 데이터를 거슬러 올라가며 계산)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from pykrx import stock

log = logging.getLogger(__name__)

# pykrx가 받아들이는 투자주체 문자열. 노션 DB의 '투자주체' 선택지와 일치시킨다.
INVESTORS = ["외국인", "기관합계", "연기금"]
MARKETS = ["KOSPI", "KOSDAQ"]

# 연속 순매수일을 계산할 때 거슬러 올라갈 달력 일수
STREAK_LOOKBACK_DAYS = 60

# 일별 상세 조회에서 투자주체 컬럼명. 위 INVESTORS와 다를 수 있어 따로 둔다.
DAILY_COLUMN = {"외국인": "외국인", "기관합계": "기관합계", "연기금": "연기금"}


@dataclass
class Week:
    """기준 주간 (월요일 ~ 금요일)."""

    monday: date
    friday: date

    @property
    def label(self) -> str:
        """ISO 주차 라벨. 예: 2026-W36"""
        iso = self.monday.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    @property
    def start(self) -> str:
        return self.monday.strftime("%Y%m%d")

    @property
    def end(self) -> str:
        return self.friday.strftime("%Y%m%d")

    def __str__(self) -> str:
        return f"{self.label} ({self.monday} ~ {self.friday})"


def last_completed_week(today: date | None = None) -> Week:
    """오늘 기준으로 가장 최근에 끝난 주(월~금)를 반환한다.

    월요일에 실행하면 지난주가 잡힌다. 주중에 수동 실행해도
    이번 주가 아직 안 끝났으므로 지난주를 잡는다.
    """
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    return Week(monday=last_monday, friday=last_monday + timedelta(days=4))


def week_from_label(label: str) -> Week:
    """'2026-W36' 형식의 라벨을 Week로 되돌린다. 과거 데이터 소급 수집용."""
    year_str, week_str = label.split("-W")
    monday = date.fromisocalendar(int(year_str), int(week_str), 1)
    return Week(monday=monday, friday=monday + timedelta(days=4))


def load_sector_map(path: str | Path = "sectors.json") -> dict[str, list[str]]:
    """종목코드 -> 섹터 목록 매핑. 파일이 없으면 빈 매핑."""
    p = Path(path)
    if not p.exists():
        log.info("섹터 매핑 파일 없음(%s). 섹터는 비워둔다.", p)
        return {}
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def _net_purchases(week: Week, market: str, investor: str) -> pd.DataFrame:
    """한 시장 × 한 투자주체의 주간 누적 순매수."""
    df = stock.get_market_net_purchases_of_equities(
        week.start, week.end, market, investor
    )
    if df is None or df.empty:
        return pd.DataFrame()
    out = df[["종목명", "순매수거래대금"]].copy()
    out["시장"] = market
    return out


def _price_change(week: Week, market: str) -> pd.DataFrame:
    """한 시장의 주간 등락률."""
    df = stock.get_market_price_change(week.start, week.end, market=market)
    if df is None or df.empty:
        return pd.DataFrame()
    return df[["등락률"]].copy()


def _streak(ticker: str, investor: str, upto: date, cache: dict) -> int | None:
    """upto 날짜부터 거슬러 올라가며 연속 순매수 거래일 수를 센다.

    순매수가 0 이하인 날을 만나면 멈춘다. 조회 실패 시 None.
    """
    key = (ticker, investor)
    if key in cache:
        return cache[key]

    col = DAILY_COLUMN[investor]
    start = (upto - timedelta(days=STREAK_LOOKBACK_DAYS)).strftime("%Y%m%d")
    try:
        df = stock.get_market_trading_value_by_date(
            start, upto.strftime("%Y%m%d"), ticker, on="순매수", detail=True
        )
    except Exception as exc:  # 상장폐지, ETF 등 조회가 안 되는 종목이 있다
        log.warning("연속일 조회 실패 %s/%s: %s", ticker, investor, exc)
        cache[key] = None
        return None

    if df is None or df.empty or col not in df.columns:
        cache[key] = None
        return None

    count = 0
    for value in reversed(df[col].tolist()):
        if value > 0:
            count += 1
        else:
            break
    cache[key] = count
    return count


def collect(
    week: Week,
    top_n: int = 10,
    with_streak: bool = True,
    sector_map: dict[str, list[str]] | None = None,
) -> list[dict]:
    """한 주간의 투자주체별 순매수 상위 종목을 수집한다.

    반환: 노션에 그대로 넣을 수 있는 dict 목록.
    """
    sector_map = sector_map or {}
    streak_cache: dict = {}
    records: list[dict] = []

    # 등락률은 시장별로 한 번씩만 받아 재사용한다
    change = {}
    for market in MARKETS:
        try:
            change[market] = _price_change(week, market)
        except Exception as exc:
            log.warning("%s 등락률 조회 실패: %s", market, exc)
            change[market] = pd.DataFrame()

    for investor in INVESTORS:
        frames = []
        for market in MARKETS:
            try:
                frames.append(_net_purchases(week, market, investor))
            except Exception as exc:
                log.error("%s/%s 순매수 조회 실패: %s", market, investor, exc)
        frames = [f for f in frames if not f.empty]
        if not frames:
            log.error("%s: 수집된 데이터가 없다. 건너뛴다.", investor)
            continue

        merged = pd.concat(frames)
        # KOSPI/KOSDAQ을 합쳐 투자주체 기준으로 다시 순위를 매긴다
        merged = merged.sort_values("순매수거래대금", ascending=False).head(top_n)

        for rank, (ticker, row) in enumerate(merged.iterrows(), start=1):
            market = row["시장"]
            rate = None
            cdf = change.get(market)
            if cdf is not None and not cdf.empty and ticker in cdf.index:
                rate = round(float(cdf.loc[ticker, "등락률"]), 2)

            streak = (
                _streak(ticker, investor, week.friday, streak_cache)
                if with_streak
                else None
            )

            records.append(
                {
                    "종목명": row["종목명"],
                    "종목코드": ticker,
                    "시장": market,
                    "투자주체": investor,
                    "주차": week.label,
                    "기준주간_시작": week.monday.isoformat(),
                    "기준주간_종료": week.friday.isoformat(),
                    # KRX는 원 단위로 준다. 억원으로 환산.
                    "순매수금액": round(float(row["순매수거래대금"]) / 1e8, 1),
                    "순위": rank,
                    "연속순매수일": streak,
                    "주간등락률": rate,
                    "섹터": sector_map.get(ticker, []),
                    "레코드키": f"{week.label}_{ticker}_{investor}",
                }
            )
        log.info("%s: %d건 수집", investor, len(merged))

    return records
