"""
홍인기식 일일 스크리닝 (끼 종목 → 주도주 깔때기).

1단계 '끼 종목'   : 등락률·거래대금이 기준 이상인 종목 (넓은 그물)
2단계 '주도주'    : 끼 종목 중 아래 조건을 모두 충족
    - 거래대금 순위(KOSPI+KOSDAQ 통합) 상위 N위 이내
    - 네이버 증권 테마 동반상승 (같은 테마에서 기준 등락률 이상 종목이 M개 이상)
    - 신고가 장대양봉 (종가가 직전 60거래일 고가 돌파 + 몸통비율 기준 이상)

데이터: pykrx(KRX 정보데이터시스템) + 네이버 증권 테마 페이지
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup
from pykrx import stock

log = logging.getLogger(__name__)

MARKETS = ["KOSPI", "KOSDAQ"]
NAVER = "https://finance.naver.com"
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
THEME_LINK = re.compile(r"sise_group_detail\.naver\?type=theme&no=(\d+)")
ITEM_LINK = re.compile(r"/item/main\.naver\?code=(\w{6})")


# --- 설정 -----------------------------------------------------------------

def load_config(path: str | Path = "screener_config.json") -> dict:
    with Path(path).open(encoding="utf-8") as f:
        cfg = json.load(f)
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


# --- 기준일 / 전종목 시세 ----------------------------------------------------

def resolve_base_date(target: date | None = None) -> str:
    """target 이전(포함) 가장 가까운 영업일을 YYYYMMDD로 반환."""
    target = target or date.today()
    return stock.get_nearest_business_day_in_a_week(
        date=target.strftime("%Y%m%d"), prev=True
    )


def _is_common_stock(ticker: str, name: str) -> bool:
    """우선주·스팩·리츠 등 단타 관찰 대상이 아닌 종목을 걸러낸다."""
    if not ticker.isdigit() or not ticker.endswith("0"):
        return False  # 우선주(…5, …7, …K 등)
    bad = ("스팩", "리츠", "인프라", "ETN", "선물")
    return not any(b in name for b in bad)


def market_snapshot(base: str) -> pd.DataFrame:
    """기준일의 KOSPI+KOSDAQ 전종목 시세. 거래대금 통합 순위 포함."""
    frames = []
    for market in MARKETS:
        df = stock.get_market_ohlcv_by_ticker(base, market=market)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["시장"] = market
        frames.append(df)
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames)
    df = df[df["종가"] > 0]
    if df["거래대금"].sum() == 0:
        return pd.DataFrame()  # 휴장일
    df["종목명"] = [stock.get_market_ticker_name(t) for t in df.index]
    df = df[[_is_common_stock(t, n) for t, n in zip(df.index, df["종목명"])]]
    df["거래대금_억"] = (df["거래대금"] / 1e8).round(1)
    df["거래대금순위"] = df["거래대금"].rank(ascending=False, method="min").astype(int)
    return df


# --- 네이버 테마 -----------------------------------------------------------

def _get(url: str) -> str:
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=UA, timeout=15)
            resp.raise_for_status()
            resp.encoding = "euc-kr"
            return resp.text
        except requests.RequestException as exc:
            log.warning("요청 실패(%d/3) %s: %s", attempt + 1, url, exc)
            time.sleep(2 * (attempt + 1))
    return ""


def fetch_theme_map(max_pages: int = 15, pause: float = 0.15) -> dict[str, list[str]]:
    """테마명 -> 종목코드 목록. 실패하면 빈 dict (테마 조건만 비활성)."""
    themes: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        html = _get(f"{NAVER}/sise/theme.naver?&page={page}")
        if not html:
            break
        soup = BeautifulSoup(html, "lxml")
        found = 0
        for a in soup.find_all("a", href=THEME_LINK):
            no = THEME_LINK.search(a["href"]).group(1)
            name = a.get_text(strip=True)
            if name and no not in themes.values():
                themes[name] = no
                found += 1
        if found == 0:
            break
        time.sleep(pause)

    theme_map: dict[str, list[str]] = {}
    for name, no in themes.items():
        html = _get(f"{NAVER}/sise/sise_group_detail.naver?type=theme&no={no}")
        if html:
            codes = sorted(set(ITEM_LINK.findall(html)))
            if codes:
                theme_map[name] = codes
        time.sleep(pause)
    log.info("네이버 테마 %d개 수집", len(theme_map))
    return theme_map


def theme_strength(
    ticker: str, snap: pd.DataFrame, theme_map: dict[str, list[str]], up_rate: float
) -> tuple[list[str], int, int | None]:
    """종목이 속한 테마 중 동반상승이 가장 강한 테마 기준으로
    (동반상승 테마목록, 동반상승 종목수 최댓값, 그 테마 내 등락률 순위)를 반환."""
    best_count, best_rank, hot = 0, None, []
    for theme, codes in theme_map.items():
        if ticker not in codes:
            continue
        members = snap.loc[snap.index.intersection(codes), "등락률"]
        count = int((members >= up_rate).sum())
        rank = int((members > members.get(ticker, -999)).sum()) + 1
        if count >= 2:
            hot.append(theme)
        if count > best_count:
            best_count, best_rank = count, rank
    return hot, best_count, best_rank


# --- 신고가 / 장대양봉 -------------------------------------------------------

def price_pattern(ticker: str, base: str, cfg: dict) -> dict:
    """신고가 구분, 첫돌파 여부, 몸통비율을 계산한다."""
    long_n = cfg["장기신고가_기간"]
    short_n = cfg["주도주_신고가_기간"]
    first_n = cfg["첫돌파_확인_거래일"]
    start = (datetime.strptime(base, "%Y%m%d") - timedelta(days=long_n * 1.6)).strftime("%Y%m%d")

    out = {"신고가구분": None, "첫돌파": False, "몸통비율": None}
    try:
        df = stock.get_market_ohlcv_by_date(start, base, ticker)
    except Exception as exc:
        log.warning("일봉 조회 실패 %s: %s", ticker, exc)
        return out
    if df is None or len(df) < short_n + 1:
        return out

    today, past = df.iloc[-1], df.iloc[:-1]
    rng = today["고가"] - today["저가"]
    if rng > 0:
        out["몸통비율"] = round(float((today["종가"] - today["시가"]) / rng), 2)

    close = today["종가"]
    if len(past) >= long_n and close > past["고가"].tail(long_n).max():
        out["신고가구분"] = f"{long_n}일"
    elif close > past["고가"].tail(short_n).max():
        out["신고가구분"] = f"{short_n}일"

    if out["신고가구분"]:
        # 직전 first_n 거래일 동안 한 번도 그때의 전고점을 종가로 넘은 적이 없으면 첫돌파
        ref_high = past["고가"].iloc[: -first_n].tail(short_n).max() if len(past) > first_n else None
        recent_close = past["종가"].tail(first_n).max()
        out["첫돌파"] = bool(ref_high is not None and recent_close <= ref_high)
    return out


# --- 메인 스크리닝 -----------------------------------------------------------

def screen(base: str, cfg: dict, use_themes: bool = True) -> list[dict]:
    snap = market_snapshot(base)
    if snap.empty:
        log.warning("%s: 시세가 없다(휴장일 가능성).", base)
        return []

    kki = snap[
        (snap["등락률"] >= cfg["끼_최소등락률"])
        & (snap["거래대금_억"] >= cfg["끼_최소거래대금_억"])
    ].sort_values("거래대금", ascending=False)
    log.info("%s 끼 종목 %d개", base, len(kki))
    if kki.empty:
        return []

    theme_map = fetch_theme_map() if use_themes else {}
    base_iso = f"{base[:4]}-{base[4:6]}-{base[6:]}"
    records = []
    for ticker, row in kki.iterrows():
        hot, together, t_rank = theme_strength(
            ticker, snap, theme_map, cfg["주도주_동반상승_기준등락률"]
        )
        pat = price_pattern(ticker, base, cfg)

        checks = {
            "거래대금순위": row["거래대금순위"] <= cfg["주도주_거래대금순위_이내"],
            "테마동반상승": together >= cfg["주도주_테마동반상승_최소종목수"],
            "신고가": pat["신고가구분"] is not None,
            "장대양봉": (pat["몸통비율"] or 0) >= cfg["주도주_최소몸통비율"],
        }
        is_leader = all(checks.values())
        missed = [k for k, ok in checks.items() if not ok]
        memo = "주도주 조건 모두 충족" if is_leader else "미충족: " + ", ".join(missed)
        if use_themes and not theme_map:
            memo += " (테마 수집 실패)"

        records.append(
            {
                "종목명": row["종목명"],
                "종목코드": ticker,
                "시장": row["시장"],
                "기준일": base_iso,
                "단계": "2차 주도주" if is_leader else "1차 끼",
                "종가": float(row["종가"]),
                "등락률": round(float(row["등락률"]), 2),
                "거래대금": float(row["거래대금_억"]),
                "거래대금순위": int(row["거래대금순위"]),
                "테마": hot[:5],
                "테마동반상승": together,
                "테마내순위": t_rank,
                "대장주": bool(t_rank == 1 and together >= 2),
                "신고가구분": pat["신고가구분"],
                "첫돌파": pat["첫돌파"],
                "몸통비율": pat["몸통비율"],
                "메모": memo,
                "레코드키": f"{base}_{ticker}",
            }
        )
    leaders = sum(r["단계"] == "2차 주도주" for r in records)
    log.info("%s 주도주 %d개 / 끼 종목 %d개", base, leaders, len(records))
    return records
