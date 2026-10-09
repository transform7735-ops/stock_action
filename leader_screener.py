"""
홍인기식 일일 스크리닝 (끼 종목 → 주도주 깔때기) + 물량 테스트 판정.

1단계 '끼 종목'   : 등락률·거래대금이 기준 이상인 종목 (넓은 그물)
2단계 '주도주'    : 끼 종목 중 아래 조건을 모두 충족
    - 거래대금 순위(KOSPI+KOSDAQ 통합) 상위 N위 이내
    - 테마 동반상승 (같은 테마에서 기준 등락률 이상 종목이 M개 이상)
    - 신고가 장대양봉 (종가가 직전 60거래일 고가 돌파 + 몸통비율 기준 이상)
물량 테스트 의심 : 긴 윗꼬리(윗꼬리비율) + 장중 고점에서 크게 밀림

테마 출처는 아래 순서로 시도한다.
    1) 네이버 증권 테마 (모바일 JSON API, 실시간)
    2) 직전에 성공한 네이버 테마 캐시 (.cache/themes.json)
    3) KRX 업종 분류 (테마보다 거칠지만 항상 받을 수 있다)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from pykrx import stock

log = logging.getLogger(__name__)

MARKETS = ["KOSPI", "KOSDAQ"]
NAVER_API = "https://m.stock.naver.com/api/stocks"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://m.stock.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
CACHE_PATH = Path(".cache/themes.json")


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


# --- 테마 ----------------------------------------------------------------

def _get_json(url: str, diag: list[str] | None = None) -> dict | None:
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if diag is not None and not diag:
                # 첫 응답의 상태를 남겨 실패 원인을 추적한다
                diag.append(f"status={resp.status_code} url={resp.url} len={len(resp.content)}")
            if resp.status_code == 404:
                return None  # 마지막 페이지를 넘긴 경우
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("요청 실패(%d/3) %s: %s", attempt + 1, url, exc)
            if diag is not None and not diag:
                diag.append(f"error={exc}")
            time.sleep(2 * (attempt + 1))
    return None


def fetch_naver_themes(page_size: int = 100, pause: float = 0.12) -> dict[str, list[str]]:
    """네이버 증권 테마: 테마명 -> 종목코드 목록. 실패하면 빈 dict.

    2026년 10월 기준 PC 테마 페이지(finance.naver.com)는 새 사이트로 넘어가
    화면을 스크립트로 그리므로, 같은 데이터를 주는 모바일 JSON API를 쓴다.
    """
    diag: list[str] = []
    themes: dict[str, int] = {}
    for page in range(1, 20):
        data = _get_json(f"{NAVER_API}/theme?page={page}&pageSize={page_size}",
                         diag if page == 1 else None)
        groups = (data or {}).get("groups") or []
        for g in groups:
            if g.get("name") and g.get("no") is not None:
                themes[g["name"]] = g["no"]
        total = (data or {}).get("totalCount") or 0
        if not groups or len(themes) >= total:
            break
        time.sleep(pause)

    if not themes:
        reason = " / ".join(diag) if diag else "응답 없음"
        log.warning("네이버 테마 수집 실패 (%s)", reason)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning title=네이버 테마::수집 실패 {reason}", flush=True)
        return {}

    theme_map: dict[str, list[str]] = {}
    for name, no in themes.items():
        codes: list[str] = []
        for page in range(1, 10):
            data = _get_json(f"{NAVER_API}/theme/{no}?page={page}&pageSize={page_size}")
            stocks = (data or {}).get("stocks") or []
            codes += [s["itemCode"] for s in stocks if s.get("itemCode")]
            if len(stocks) < page_size:
                break
        if codes:
            theme_map[name] = sorted(set(codes))
        time.sleep(pause)
    log.info("네이버 테마 %d개 수집 (목록 %d개)", len(theme_map), len(themes))
    return theme_map


def _save_cache(theme_map: dict[str, list[str]], base: str) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"saved": base, "themes": theme_map}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("테마 캐시 저장 실패: %s", exc)


def _load_cache() -> tuple[dict[str, list[str]], str | None]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data.get("themes", {}), data.get("saved")
    except (OSError, ValueError):
        return {}, None


def krx_sector_map(base: str) -> dict[str, list[str]]:
    """KRX 업종 분류: '업종:반도체' -> 종목코드 목록."""
    sectors: dict[str, list[str]] = {}
    for market in MARKETS:
        try:
            df = stock.get_market_sector_classifications(base, market)
        except Exception as exc:
            log.warning("KRX 업종 조회 실패(%s): %s", market, exc)
            continue
        if df is None or df.empty:
            continue
        for ticker, sector in df["업종명"].items():
            sectors.setdefault(f"업종:{sector}", []).append(ticker)
    log.info("KRX 업종 %d개 수집", len(sectors))
    return sectors


def load_theme_map(base: str) -> tuple[dict[str, list[str]], str]:
    """(테마맵, 출처설명). 네이버 → 캐시 → KRX 업종 순으로 시도한다."""
    themes = fetch_naver_themes()
    if themes:
        _save_cache(themes, base)
        return themes, "네이버"
    cached, saved = _load_cache()
    if cached:
        log.info("네이버 실패 → %s 캐시 테마 %d개 사용", saved, len(cached))
        return cached, f"네이버 캐시({saved})"
    sectors = krx_sector_map(base)
    if sectors:
        return sectors, "KRX 업종 대체"
    return {}, "없음"


def theme_strength(
    ticker: str, snap: pd.DataFrame, theme_map: dict[str, list[str]], up_rate: float
) -> tuple[list[str], int, int | None]:
    """종목이 속한 테마 중 동반상승이 가장 강한 테마 기준으로
    (테마목록[동반상승 많은 순], 동반상승 종목수 최댓값, 그 테마 내 등락률 순위)를 반환."""
    scored = []
    best_count, best_rank = 0, None
    for theme, codes in theme_map.items():
        if ticker not in codes:
            continue
        members = snap.loc[snap.index.intersection(codes), "등락률"]
        count = int((members >= up_rate).sum())
        rank = int((members > members.get(ticker, -999)).sum()) + 1
        scored.append((count, theme))
        if count > best_count:
            best_count, best_rank = count, rank
    scored.sort(reverse=True)
    return [t for _, t in scored[:5]], best_count, best_rank


# --- 캔들 / 신고가 -----------------------------------------------------------

def candle_shape(row: pd.Series) -> dict:
    """당일 캔들 모양. 기준일 전종목 시세의 같은 행에서 계산해 날짜가 어긋나지 않게 한다."""
    o, h, l, c, rate = row["시가"], row["고가"], row["저가"], row["종가"], row["등락률"]
    rng = h - l
    prev_close = c / (1 + rate / 100) if rate > -100 else None
    out = {
        "몸통비율": None,
        "윗꼬리비율": None,
        "장중최고등락률": None,
        "테스트기준가": float(round((h + l) / 2)) if rng > 0 else None,
    }
    if rng > 0:
        out["몸통비율"] = round(float((c - o) / rng), 2)
        out["윗꼬리비율"] = round(float((h - max(o, c)) / rng), 2)
    if prev_close:
        out["장중최고등락률"] = round(float((h / prev_close - 1) * 100), 2)
    return out


def price_history(ticker: str, base: str, cfg: dict) -> dict:
    """기준일 이전 일봉으로 신고가 구분과 첫돌파 여부를 계산한다."""
    long_n = cfg["장기신고가_기간"]
    short_n = cfg["주도주_신고가_기간"]
    first_n = cfg["첫돌파_확인_거래일"]
    base_dt = datetime.strptime(base, "%Y%m%d")
    start = (base_dt - timedelta(days=long_n * 1.6)).strftime("%Y%m%d")

    out = {"신고가구분": None, "첫돌파": False}
    try:
        df = stock.get_market_ohlcv_by_date(start, base, ticker)
    except Exception as exc:
        log.warning("일봉 조회 실패 %s: %s", ticker, exc)
        return out
    if df is None or df.empty:
        return out

    past = df[df.index < base_dt]  # 기준일 당일은 제외하고 '직전 고점'만 본다
    if len(past) < short_n:
        return out
    close = float(df[df.index == base_dt]["종가"].iloc[0]) if (df.index == base_dt).any() else None
    if close is None:
        return out

    if len(past) >= long_n and close > past["고가"].tail(long_n).max():
        out["신고가구분"] = f"{long_n}일"
    elif close > past["고가"].tail(short_n).max():
        out["신고가구분"] = f"{short_n}일"

    if out["신고가구분"] and len(past) > first_n:
        # 직전 first_n 거래일 동안 그 이전 전고점을 종가로 넘은 적이 없으면 첫돌파
        ref_high = past["고가"].iloc[:-first_n].tail(short_n).max()
        out["첫돌파"] = bool(past["종가"].tail(first_n).max() <= ref_high)
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

    theme_map, source = load_theme_map(base) if use_themes else ({}, "생략")
    base_iso = f"{base[:4]}-{base[4:6]}-{base[6:]}"
    records = []
    for ticker, row in kki.iterrows():
        names, together, t_rank = theme_strength(
            ticker, snap, theme_map, cfg["주도주_동반상승_기준등락률"]
        )
        shape = candle_shape(row)
        hist = price_history(ticker, base, cfg)

        checks = {
            "거래대금순위": row["거래대금순위"] <= cfg["주도주_거래대금순위_이내"],
            "테마동반상승": together >= cfg["주도주_테마동반상승_최소종목수"],
            "신고가": hist["신고가구분"] is not None,
            "장대양봉": (shape["몸통비율"] or 0) >= cfg["주도주_최소몸통비율"],
        }
        is_leader = all(checks.values())

        pullback = (shape["장중최고등락률"] or 0) - float(row["등락률"])
        suspect = (
            (shape["윗꼬리비율"] or 0) >= cfg["물량테스트_최소윗꼬리비율"]
            and pullback >= cfg["물량테스트_최소고점대비밀림_p"]
        )

        missed = [k for k, ok in checks.items() if not ok]
        memo = "주도주 조건 모두 충족" if is_leader else "미충족: " + ", ".join(missed)
        if suspect:
            memo += f" | 물량테스트 의심: 장중 +{shape['장중최고등락률']}%에서 {pullback:.1f}%p 밀림"
        memo += f" | 테마출처: {source}"

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
                "테마": names,
                "테마동반상승": together,
                "테마내순위": t_rank,
                "대장주": bool(t_rank == 1 and together >= 2),
                "신고가구분": hist["신고가구분"],
                "첫돌파": hist["첫돌파"],
                "몸통비율": shape["몸통비율"],
                "윗꼬리비율": shape["윗꼬리비율"],
                "장중최고등락률": shape["장중최고등락률"],
                "물량테스트": "의심" if suspect else None,
                "테스트기준가": shape["테스트기준가"] if suspect else None,
                "메모": memo,
                "레코드키": f"{base}_{ticker}",
            }
        )
    leaders = sum(r["단계"] == "2차 주도주" for r in records)
    tests = sum(r["물량테스트"] == "의심" for r in records)
    log.info("%s 주도주 %d / 물량테스트 의심 %d / 끼 종목 %d (테마출처: %s)",
             base, leaders, tests, len(records), source)
    return records


# --- 물량 테스트 다음 날 판정 ---------------------------------------------------

def judge_test(ticker: str, base_iso: str, ref_price: float, upto: str) -> dict | None:
    """테스트일(base_iso) 다음 거래일 종가가 기준가(테스트 캔들 중간값) 위면 '지지'.

    다음 거래일이 아직 없으면 None.
    """
    start = base_iso.replace("-", "")
    try:
        df = stock.get_market_ohlcv_by_date(start, upto, ticker)
    except Exception as exc:
        log.warning("판정용 일봉 조회 실패 %s: %s", ticker, exc)
        return None
    if df is None or df.empty:
        return None
    nxt = df[df.index > datetime.strptime(start, "%Y%m%d")]
    if nxt.empty:
        return None
    day = nxt.iloc[0]
    held = float(day["종가"]) >= ref_price
    return {
        "테스트판정": "지지" if held else "이탈",
        "익일등락률": round(float(day["등락률"]), 2),
        "판정일": nxt.index[0].strftime("%Y-%m-%d"),
    }
