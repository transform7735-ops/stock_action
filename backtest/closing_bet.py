"""
종가베팅 백테스트 — 오늘 종가에 사고, 다음 날 판다.

매수 (오늘 종가, config.json 기준)
    - 거래대금 순위(KOSPI+KOSDAQ 통합) 상위 N위 이내
    - 등락률 기준 이상, 고가 대비 밀림이 기준 이내 (고가권 마감)
    - 직전 N거래일 고가 돌파 (신고가)
    - 상한가 마감 종목은 제외 (종가에 살 수 없으므로)

매도 시나리오 (다음 날)
    A. 시가 전량 매도
    B. 익절/손절: 시가가 이미 익절선 위 또는 손절선 아래면 시가에,
       장중 둘 다 닿으면 손절이 먼저 났다고 본다(보수적), 아니면 종가에
    C. 다음 날 종가 매도 (하루 더 들고 가면 어떤지 비교용)
    D. 다음 날 고가 매도 (현실에선 불가능한 이론 상한, 참고용)

한계: 일봉 데이터라 15:20 진입은 종가로, 장중 순서는 추정으로 대신한다.
      과거 테마 데이터는 없어 테마 조건은 넣지 않는다. 넥스트레이드 시간외는 반영되지 않는다.

사용법:
    python backtest/closing_bet.py                          # 최근 '기간_개월' 동안
    python backtest/closing_bet.py --start 2025-01-01 --end 2026-10-14
    python backtest/closing_bet.py --no-notion              # 파일만 만든다
    python backtest/closing_bet.py --from-csv panel.csv.gz  # 미리 받은 시세로 (시험용)

환경변수:
    KRX_ID, KRX_PW                  pykrx 로그인
    NOTION_TOKEN                    노션 보고서
    NOTION_BACKTEST_PAGE_ID         보고서를 만들 상위 페이지
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("closing_bet")

KST = ZoneInfo("Asia/Seoul")
MARKETS = ["KOSPI", "KOSDAQ"]
CACHE_DIR = ROOT / ".cache" / "backtest"
COLS = ["시가", "고가", "저가", "종가", "거래량", "거래대금", "등락률"]
SCENARIOS = {
    "A": "A. 다음날 시가 매도",
    "B": "B. 익절/손절 규칙",
    "C": "C. 다음날 종가 매도",
    "D": "D. 다음날 고가 (이론 상한)",
}


def gh_annotate(level: str, msg: str) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level} title=종가베팅 백테스트::{msg.replace(chr(10), ' ')}", flush=True)


def load_config(path: Path = ROOT / "backtest" / "config.json") -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in cfg.items() if not k.startswith("_")}


# --- 1. 시세 수집 (일자별 캐시) ------------------------------------------------

def _fetch_day(day: str) -> pd.DataFrame | None:
    """하루치 KOSPI+KOSDAQ 전종목 시세. 휴장일이면 빈 DataFrame."""
    from pykrx import stock

    frames = []
    for market in MARKETS:
        for attempt in range(3):
            try:
                df = stock.get_market_ohlcv_by_ticker(day, market=market)
                break
            except Exception as exc:  # 네트워크 일시 오류
                log.warning("%s %s 조회 실패(%d/3): %s", day, market, attempt + 1, exc)
                time.sleep(3 * (attempt + 1))
        else:
            return None
        if df is None or df.empty:
            continue
        df = df.copy()
        df["시장"] = market
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    if df["거래대금"].sum() == 0:
        return pd.DataFrame()  # 휴장일
    df.index.name = "종목코드"
    return df.reset_index()


def load_panel(start: date, end: date) -> pd.DataFrame:
    """start~end 전종목 일봉. 받은 날은 .cache/backtest에 두고 다시 받지 않는다."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(KST).date()
    frames, fetched, day = [], 0, start
    while day <= end:
        if day.weekday() < 5:
            key = day.strftime("%Y%m%d")
            path = CACHE_DIR / f"{key}.csv.gz"
            hol = CACHE_DIR / f"{key}.holiday"
            if path.exists():
                frames.append(pd.read_csv(path, dtype={"종목코드": str}))
            elif not hol.exists():
                df = _fetch_day(key)
                fetched += 1
                if df is None:
                    log.warning("%s 수집 실패: 건너뜀", key)
                elif df.empty:
                    if day < today:
                        hol.touch()
                else:
                    df["일자"] = key
                    # 오늘 장중 데이터는 확정치가 아니므로 캐시하지 않는다
                    if day < today:
                        df.to_csv(path, index=False, compression="gzip")
                    frames.append(df)
                time.sleep(0.2)
                if fetched % 50 == 0:
                    log.info("수집 중… %s (새로 받은 날 %d)", key, fetched)
        day += timedelta(days=1)
    log.info("시세 준비: 영업일 %d일 (새로 받은 날 %d)", len(frames), fetched)
    if not frames:
        return pd.DataFrame()
    panel = pd.concat(frames, ignore_index=True)
    panel["일자"] = panel["일자"].astype(str)
    return panel


# --- 2. 신호와 다음 날 결과 ----------------------------------------------------

def build_signals(panel: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df = panel.copy()
    df = df[df["종목코드"].str.isdigit() & df["종목코드"].str.endswith("0")]  # 우선주 제외
    df = df.sort_values(["종목코드", "일자"]).reset_index(drop=True)

    # 거래대금 통합 순위 (일자별)
    df["거래대금순위"] = df.groupby("일자")["거래대금"].rank(ascending=False, method="min")

    g = df.groupby("종목코드", sort=False)
    n = int(cfg["매수_신고가_기간"])
    df["직전고가"] = g["고가"].transform(lambda s: s.shift(1).rolling(n, min_periods=n).max())
    for col in ("시가", "고가", "저가", "종가", "거래량"):
        df[f"익일{col}"] = g[col].shift(-1)
    df["익일일자"] = g["일자"].shift(-1)

    traded = df["거래량"] > 0
    cond = (
        traded
        & (df["거래대금순위"] <= cfg["매수_거래대금순위_이내"])
        & (df["등락률"] >= cfg["매수_최소등락률"])
        & (df["거래대금"] >= cfg["매수_최소거래대금_억"] * 1e8)
        & (df["종가"] >= df["고가"] * (1 - cfg["매수_고가대비_최대밀림_p"] / 100))
    )
    if cfg.get("매수_신고가_필수", True):
        cond &= df["종가"] > df["직전고가"]
    if cfg.get("상한가_제외", True):
        cond &= df["등락률"] < 29.5
    sig = df[cond].copy()

    # 다음 날이 없거나(최근일), 거래정지, 권리락·액면분할처럼 가격이 이어지지 않는 경우 제외
    ratio = sig["익일시가"] / sig["종가"]
    valid = sig["익일일자"].notna() & (sig["익일거래량"] > 0) & (sig["익일시가"] > 0) & ratio.between(0.69, 1.31)
    dropped = int((~valid).sum())
    sig = sig[valid].copy()
    if dropped:
        log.info("다음 날 정보가 없거나 가격이 끊긴 신호 %d건 제외", dropped)
    sig["고가대비밀림"] = (1 - sig["종가"] / sig["고가"]) * 100
    sig["갭"] = (sig["익일시가"] / sig["종가"] - 1) * 100
    return sig


def exit_returns(sig: pd.DataFrame, tp: float, sl: float, cost: float) -> pd.DataFrame:
    """시나리오별 수익률(%, 왕복비용 차감)."""
    e = sig["종가"]
    o, h, l, c = sig["익일시가"], sig["익일고가"], sig["익일저가"], sig["익일종가"]
    tp_px, sl_px = e * (1 + tp / 100), e * (1 - sl / 100)

    # B: 시가에서 이미 선을 넘었으면 시가, 장중 둘 다 닿으면 손절 우선, 하나만 닿으면 그 값, 아니면 종가
    b = c.copy()
    hit_tp, hit_sl = h >= tp_px, l <= sl_px
    b[hit_tp] = tp_px[hit_tp]
    b[hit_sl] = sl_px[hit_sl]
    b[o >= tp_px] = o[o >= tp_px]
    b[o <= sl_px] = o[o <= sl_px]
    b_reason = pd.Series("종가청산", index=sig.index)
    b_reason[hit_tp] = "익절"
    b_reason[hit_sl] = "손절"
    b_reason[hit_tp & hit_sl] = "손절(둘다닿음)"
    b_reason[o >= tp_px] = "시가익절"
    b_reason[o <= sl_px] = "시가손절"

    out = pd.DataFrame(index=sig.index)
    for key, px in (("A", o), ("B", b), ("C", c), ("D", h)):
        out[key] = (px / e - 1) * 100 - cost
    out["B사유"] = b_reason
    return out


# --- 3. 통계 ------------------------------------------------------------------

def _max_losing_streak(daily: pd.Series) -> int:
    best = cur = 0
    for v in daily:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def stats(r: pd.Series, days: pd.Series) -> dict:
    """r: 거래별 수익률(%), days: 신호 일자 (같은 날 여러 종목은 균등 분산으로 본다)."""
    if r.empty:
        return {}
    wins, losses = r[r > 0], r[r <= 0]
    avg_w, avg_l = wins.mean() if len(wins) else 0.0, losses.mean() if len(losses) else 0.0
    daily = r.groupby(days.values).mean().sort_index()  # 하루 자금을 그날 신호에 나눠 투자
    equity = (1 + daily / 100).cumprod()
    mdd = ((equity / equity.cummax()) - 1).min() * 100
    return {
        "거래수": int(len(r)),
        "매매일수": int(len(daily)),
        "승률": len(wins) / len(r) * 100,
        "평균": r.mean(),
        "중앙값": r.median(),
        "평균이익": avg_w,
        "평균손실": avg_l,
        "손익비": (avg_w / abs(avg_l)) if avg_l else float("nan"),
        "최악": r.min(),
        "최고": r.max(),
        "최대연속손실일": _max_losing_streak(daily),
        "누적_복리": (equity.iloc[-1] - 1) * 100,
        "MDD": mdd,
    }


def analyze(sig: pd.DataFrame, cfg: dict) -> dict:
    cost = cfg["왕복비용_p"]
    rets = exit_returns(sig, cfg["매도B_익절_p"], cfg["매도B_손절_p"], cost)
    trades = pd.concat([sig, rets], axis=1)
    days = trades["일자"]

    res = {"scenarios": {k: stats(trades[k], days) for k in SCENARIOS}}
    res["gap"] = {
        "갭상승비율": (trades["갭"] > 0).mean() * 100,
        "평균갭": trades["갭"].mean(),
        "중앙갭": trades["갭"].median(),
        "3%이상갭": (trades["갭"] >= 3).mean() * 100,
        "-3%이하갭": (trades["갭"] <= -3).mean() * 100,
    }
    res["b_reason"] = trades["B사유"].value_counts().to_dict()

    # 등락률 구간별 (시나리오 A)
    bands = pd.cut(trades["등락률"], [0, 10, 15, 20, 29.5], right=False,
                   labels=["5~10%", "10~15%", "15~20%", "20%~상한가 전"])
    res["by_band"] = {str(b): stats(t["A"], t["일자"]) for b, t in trades.groupby(bands, observed=True)}

    # 시장별, 연도별
    res["by_market"] = {m: stats(t["A"], t["일자"]) for m, t in trades.groupby("시장")}
    res["by_year"] = {
        y: {k: stats(t[k], t["일자"]) for k in ("A", "B")} for y, t in trades.groupby(trades["일자"].str[:4])
    }

    # B 민감도 (익절 x 손절 → 평균 수익률/승률)
    grid = []
    for tp in cfg["민감도_익절_p"]:
        for sl in cfg["민감도_손절_p"]:
            r = exit_returns(sig, tp, sl, cost)["B"]
            grid.append({"익절": tp, "손절": sl, "평균": r.mean(), "승률": (r > 0).mean() * 100})
    res["grid"] = grid
    res["trades"] = trades
    return res


# --- 4. 보고서 ----------------------------------------------------------------

def f(v, d=2, sign=True) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    return f"{v:+.{d}f}" if sign else f"{v:.{d}f}"


def conditions_text(cfg: dict) -> list[str]:
    out = [
        f"거래대금 통합 순위 {cfg['매수_거래대금순위_이내']}위 이내, 거래대금 {cfg['매수_최소거래대금_억']}억 이상",
        f"등락률 +{cfg['매수_최소등락률']}% 이상, 고가 대비 {cfg['매수_고가대비_최대밀림_p']}% 이내 마감",
    ]
    if cfg.get("매수_신고가_필수", True):
        out.append(f"종가가 직전 {cfg['매수_신고가_기간']}거래일 고가 돌파")
    if cfg.get("상한가_제외", True):
        out.append("상한가 마감 종목 제외 (종가에 살 수 없음)")
    out.append(f"매도 B: 익절 +{cfg['매도B_익절_p']}% / 손절 -{cfg['매도B_손절_p']}% · 왕복비용 {cfg['왕복비용_p']}% 차감")
    return out


def scenario_rows(res: dict) -> list[list[str]]:
    rows = []
    for k, label in SCENARIOS.items():
        s = res["scenarios"].get(k)
        if not s:
            continue
        rows.append([label, s["거래수"], f(s["승률"], 1, False), f(s["평균"]), f(s["평균이익"]), f(s["평균손실"]),
                     f(s["손익비"], 2, False), f(s["최악"]), f(s["MDD"], 1), s["최대연속손실일"]])
    return rows


SCEN_HEADER = ["시나리오", "거래", "승률%", "평균%", "평균이익%", "평균손실%", "손익비", "최악%", "MDD%", "연속손실일"]


def insights(res: dict, cfg: dict) -> list[str]:
    s, g = res["scenarios"], res["gap"]
    a, b, c = s["A"], s["B"], s["C"]
    out = [
        f"신호 {a['거래수']}건 ({a['매매일수']}일). 다음 날 갭상승 {g['갭상승비율']:.0f}%, 평균 갭 {g['평균갭']:+.2f}%.",
        f"시가 매도(A) 승률 {a['승률']:.0f}%, 거래당 평균 {a['평균']:+.2f}% (비용 차감).",
    ]
    best = max(("A", "B", "C"), key=lambda k: s[k]["평균"])
    out.append(f"평균 기준 가장 나은 매도는 {SCENARIOS[best]}.")
    if c["평균"] < a["평균"]:
        out.append(f"하루 더 들고 가면(C) 평균 {c['평균']:+.2f}%로 시가 매도보다 못하다 — '갭에 판다'는 가설과 맞는 방향.")
    else:
        out.append(f"하루 더 들고 가도(C) 평균 {c['평균']:+.2f}%로 시가 매도보다 낫다 — 시가 매도 가설과는 반대 방향.")
    out.append(f"B 규칙의 최악 {b['최악']:+.2f}%, 최대 연속 손실 {b['최대연속손실일']}일 — 손절선이 실제로 지켜질 수 있는지 볼 숫자.")
    if a["평균"] <= 0:
        out.append("비용을 빼면 평균이 0 이하. 이 조건 그대로는 매수 원칙으로 삼기 어렵다.")
    return out


def to_markdown(res: dict, cfg: dict, period: str) -> str:
    def table(header, rows):
        lines = ["| " + " | ".join(map(str, header)) + " |", "|" + "---|" * len(header)]
        lines += ["| " + " | ".join(map(str, r)) + " |" for r in rows]
        return "\n".join(lines)

    md = [f"# 종가베팅 백테스트 ({period})", "", "## 핵심", *[f"- {t}" for t in insights(res, cfg)],
          "", "## 매수 조건", *[f"- {t}" for t in conditions_text(cfg)],
          "", "## 매도 시나리오별 성과", table(SCEN_HEADER, scenario_rows(res)),
          "", "## B 민감도 (평균% / 승률%)",
          table(["익절\\손절"] + [f"-{sl}%" for sl in cfg["민감도_손절_p"]],
                [[f"+{tp}%"] + [f"{x['평균']:+.2f} / {x['승률']:.0f}" for x in res["grid"] if x["익절"] == tp]
                 for tp in cfg["민감도_익절_p"]]),
          "", "## 등락률 구간별 (A 시가 매도)",
          table(["구간", "거래", "승률%", "평균%"],
                [[k, v["거래수"], f(v["승률"], 1, False), f(v["평균"])] for k, v in res["by_band"].items() if v]),
          "", "## 연도별",
          table(["연도", "거래", "A 승률%", "A 평균%", "B 승률%", "B 평균%"],
                [[y, v["A"]["거래수"], f(v["A"]["승률"], 1, False), f(v["A"]["평균"]), f(v["B"]["승률"], 1, False),
                  f(v["B"]["평균"])] for y, v in res["by_year"].items()]),
          "", "## 한계",
          "- 일봉 기준: 15:20 진입은 종가로, 장중 익절·손절 순서는 추정(둘 다 닿으면 손절 우선).",
          "- 테마 조건 미적용, 넥스트레이드 시간외 미반영, 슬리피지 미반영."]
    return "\n".join(md)


def notion_report(res: dict, cfg: dict, period: str, csv_path: Path) -> str | None:
    token, parent = os.environ.get("NOTION_TOKEN"), os.environ.get("NOTION_BACKTEST_PAGE_ID")
    if not (token and parent):
        log.info("노션 설정 없음: 보고서 생략")
        return None
    import requests
    from weekly_report import _rt, _table, notion_call, notion_headers

    def bullets(items):
        return [{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(t)}} for t in items]

    def h2(t):
        return {"type": "heading_2", "heading_2": {"rich_text": _rt(t)}}

    run_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    title = f"종가베팅 백테스트 {period} (실행 {run_at})"
    grid_rows = [[f"+{tp}%"] + [f"{x['평균']:+.2f} / {x['승률']:.0f}" for x in res["grid"] if x["익절"] == tp]
                 for tp in cfg["민감도_익절_p"]]
    children = [
        {"type": "callout", "callout": {"icon": {"type": "emoji", "emoji": "📌"},
                                        "rich_text": _rt("수익률은 거래당 %, 왕복비용 차감. 같은 날 여러 종목은 자금을 균등 분산했다고 보고 MDD를 계산.")}},
        h2("핵심"), *bullets(insights(res, cfg)),
        h2("매수 조건"), *bullets(conditions_text(cfg)),
        h2("매도 시나리오별 성과"), _table(SCEN_HEADER, scenario_rows(res)),
        h2("B 민감도 (평균% / 승률%)"),
        _table(["익절\\손절"] + [f"-{sl}%" for sl in cfg["민감도_손절_p"]], grid_rows),
        h2("등락률 구간별 (A 시가 매도)"),
        _table(["구간", "거래", "승률%", "평균%"],
               [[k, v["거래수"], f(v["승률"], 1, False), f(v["평균"])] for k, v in res["by_band"].items() if v]),
        h2("연도별"),
        _table(["연도", "거래", "A 승률%", "A 평균%", "B 승률%", "B 평균%"],
               [[y, v["A"]["거래수"], f(v["A"]["승률"], 1, False), f(v["A"]["평균"]),
                 f(v["B"]["승률"], 1, False), f(v["B"]["평균"])] for y, v in res["by_year"].items()]),
        h2("한계"),
        *bullets(["일봉 기준: 15:20 진입은 종가로, 장중 익절·손절 순서는 추정(둘 다 닿으면 손절 우선).",
                  "테마 조건 미적용, 넥스트레이드 시간외 미반영, 슬리피지 미반영."]),
        h2("거래 내역"),
    ]
    page = notion_call("POST", "/pages", token, json={
        "parent": {"page_id": parent}, "icon": {"type": "emoji", "emoji": "🧪"},
        "properties": {"title": {"title": _rt(title)}}, "children": children})
    try:
        fu = notion_call("POST", "/file_uploads", token, json={"filename": csv_path.name, "content_type": "text/csv"})
        r = requests.post(f"https://api.notion.com/v1/file_uploads/{fu['id']}/send",
                          headers=notion_headers(token, json_body=False),
                          files={"file": (csv_path.name, csv_path.read_bytes(), "text/csv")}, timeout=120)
        if not r.ok:
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        notion_call("PATCH", f"/blocks/{page['id']}/children", token, json={"children": [{
            "type": "file", "file": {"type": "file_upload", "file_upload": {"id": fu["id"]}, "name": csv_path.name}}]})
    except Exception as exc:
        log.warning("CSV 첨부 실패(보고서 본문은 생성됨): %s", exc)
        gh_annotate("warning", f"CSV 첨부 실패: {exc}")
    log.info("노션 보고서: %s", page["url"])
    return page["url"]


# --- 실행 ----------------------------------------------------------------------

def parse_date(text: str | None) -> date | None:
    return datetime.strptime(text, "%Y-%m-%d").date() if text else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out", default="reports/backtest")
    ap.add_argument("--no-notion", action="store_true")
    ap.add_argument("--from-csv", help="일봉 패널 CSV(종목코드,일자,시장,시가,고가,저가,종가,거래량,거래대금,등락률)")
    args = ap.parse_args()
    cfg = load_config()

    end = parse_date(args.end) or datetime.now(KST).date()
    start = parse_date(args.start) or (end - timedelta(days=int(cfg["기간_개월"] * 30.5)))
    # 신고가 계산에 필요한 앞부분 여유 (거래일 기준 → 달력일 약 1.6배)
    warmup = start - timedelta(days=int(cfg["매수_신고가_기간"] * 1.6) + 7)

    if args.from_csv:
        panel = pd.read_csv(args.from_csv, dtype={"종목코드": str, "일자": str})
    else:
        panel = load_panel(warmup, end)
    if panel.empty:
        log.error("시세가 비어 있다.")
        gh_annotate("error", "시세 수집 실패")
        return 1

    sig = build_signals(panel, cfg)
    sig = sig[sig["일자"] >= start.strftime("%Y%m%d")]
    if sig.empty:
        log.error("조건을 만족한 신호가 없다. config.json 기준을 완화해 보세요.")
        gh_annotate("warning", "신호 0건")
        return 1

    # 종목명은 신호 종목만 조회하고, 스팩·리츠 등은 이름으로 거른다
    if "종목명" not in sig.columns:
        from pykrx import stock
        names = {t: stock.get_market_ticker_name(t) for t in sig["종목코드"].unique()}
        sig["종목명"] = sig["종목코드"].map(names)
    bad = ("스팩", "리츠", "인프라", "ETN", "선물")
    sig = sig[~sig["종목명"].fillna("").str.contains("|".join(bad))]

    res = analyze(sig, cfg)
    period = f"{sig['일자'].min()}~{sig['일자'].max()}"

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M")
    csv_path = out / f"closing_bet_trades_{stamp}.csv"
    cols = ["일자", "종목코드", "종목명", "시장", "종가", "등락률", "거래대금순위", "고가대비밀림", "익일일자",
            "익일시가", "익일고가", "익일저가", "익일종가", "갭", "A", "B", "B사유", "C", "D"]
    res["trades"][cols].round(3).to_csv(csv_path, index=False, encoding="utf-8-sig")
    md = to_markdown(res, cfg, period)
    md_path = out / f"closing_bet_{stamp}.md"
    md_path.write_text(md, encoding="utf-8")
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(md + "\n")
    gh_annotate("notice", insights(res, cfg)[1])

    if not args.no_notion:
        try:
            notion_report(res, cfg, period, csv_path)
        except Exception as exc:
            # 노션이 실패해도 결과는 실행 요약과 첨부 파일로 남는다
            log.error("노션 보고서 실패: %s", exc)
            gh_annotate("warning", f"노션 보고서 실패: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
