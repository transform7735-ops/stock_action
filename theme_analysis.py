"""
주간 수급 '테마별 분석'.

주간 수급 상위 종목을 네이버 증권 테마에 대응시켜, 수급 종목이 2개 이상 모인 테마만
이번 주 / 최근 6주 누적으로 집계하고 규칙 기반 해설을 만든다.

- 한 종목이 여러 테마에 속하면 모든 테마에 넣는다(테마 합계끼리는 중복될 수 있다).
- 이번 주 구성 종목이 똑같은 테마들은 하나로 묶어 '2차전지 / 2차전지(소재/부품)'처럼 표시한다.
- 테마 출처: 네이버 테마(실시간) → 직전 성공 캐시(.cache/themes.json) → 없으면 분석 생략.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

INV = ["외국인", "기관합계", "연기금"]
MIN_STOCKS = 2        # 수급 종목이 이 개수 이상 모인 테마만 본다
ACCEL = 1.5           # 이번 주가 직전 3주 평균의 1.5배 이상이면 '가속'
SLOW = 0.5            # 0.5배 이하면 '둔화'
CACHE_PATH = Path(".cache/themes.json")


# --- 테마 지도 -------------------------------------------------------------------

def load_theme_map() -> tuple[dict[str, list[str]], str]:
    """(테마명 -> 종목코드 목록, 출처). 실패하면 ({}, 사유)."""
    try:
        from leader_screener import fetch_naver_themes  # 주도주 스크리닝과 같은 수집기
        themes = fetch_naver_themes()
    except Exception as exc:  # pykrx/네트워크 문제로 import·수집이 실패해도 보고서는 만든다
        log.warning("네이버 테마 수집 실패: %s", exc)
        themes = {}
    if themes:
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps({"saved": "weekly", "themes": themes}, ensure_ascii=False),
                                  encoding="utf-8")
        except OSError:
            pass
        return themes, "네이버 증권 테마"
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("themes"):
            return data["themes"], f"네이버 테마 캐시({data.get('saved')})"
    except (OSError, ValueError):
        pass
    return {}, "테마 수집 실패"


# --- 집계 ------------------------------------------------------------------------

def _code(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().split(".")[0]
    return s.zfill(6) if s.isdigit() else s


def _fmt(v: float) -> str:
    return f"{v / 10000:.1f}조" if abs(v) >= 10000 else f"{v:,.0f}억"


def analyze(df: pd.DataFrame, theme_map: dict[str, list[str]], source: str, window: int = 6) -> dict:
    """df: report_builder.normalize()를 거친 수급 데이터."""
    out = {"source": source, "current": [], "cumulative": [], "stock_themes": {}, "insights": []}
    if not theme_map:
        out["insights"] = [f"테마 분석 생략: {source}."]
        return out

    df = df.copy()
    df["코드"] = df["종목코드"].map(_code)
    weeks = sorted(df["주차"].unique())
    cur = weeks[-1]
    prev3 = weeks[-4:-1]
    win = weeks[-window:]
    wdf = df[df["주차"].isin(win)]

    code_to_themes: dict[str, list[str]] = {}
    for theme, codes in theme_map.items():
        for c in codes:
            code_to_themes.setdefault(c, []).append(theme)

    # 종목 × 테마 펼치기 (최근 window주만)
    rows = []
    for r in wdf.itertuples():
        for t in code_to_themes.get(r.코드, []):
            rows.append({"테마": t, "종목명": r.종목명, "주차": r.주차, "투자주체": r.투자주체,
                         "금액": r.순매수금액 or 0.0, "연속": r.연속순매수일, "등락": r.주간등락률})
    if not rows:
        out["insights"] = ["테마 분석: 이번 수급 종목 중 네이버 테마에 속한 종목이 없습니다."]
        return out
    x = pd.DataFrame(rows)

    def summarize(sub: pd.DataFrame) -> pd.DataFrame:
        g = sub.groupby("테마")
        res = pd.DataFrame({
            "종목수": g["종목명"].nunique(),
            "참여주체수": g["투자주체"].nunique(),
            "합계": g["금액"].sum(),
        })
        piv = sub.pivot_table(index="테마", columns="투자주체", values="금액", aggfunc="sum", fill_value=0)
        for inv in INV:
            res[inv] = piv[inv] if inv in piv else 0.0
        amt = sub.groupby(["테마", "종목명"])["금액"].sum().reset_index().sort_values("금액", ascending=False)
        res["종목"] = amt.groupby("테마", sort=False)["종목명"].agg(tuple)
        return res[res["종목수"] >= MIN_STOCKS]

    def merge_same(res: pd.DataFrame) -> pd.DataFrame:
        """구성 종목이 똑같은 테마는 하나로 합친다 (이름은 ' / '로 연결)."""
        groups: dict[frozenset, list[str]] = {}
        for theme, r in res.iterrows():
            groups.setdefault(frozenset(r["종목"]), []).append(theme)
        rows = []
        for names in groups.values():
            names.sort(key=lambda n: (len(n), n))  # 짧은 이름을 대표로
            rows.append({"테마": " / ".join(names), **res.loc[names[0]].to_dict()})
        return pd.DataFrame(rows).set_index("테마") if rows else res

    # 이번 주
    cx = x[x["주차"] == cur]
    cur_res = merge_same(summarize(cx)) if len(cx) else pd.DataFrame()
    # 누적
    cum_res = merge_same(summarize(x))
    weeks_present = x.groupby("테마")["주차"].nunique()
    prev_avg = x[x["주차"].isin(prev3)].groupby("테마")["금액"].sum() / max(len(prev3), 1)
    cur_amt = cx.groupby("테마")["금액"].sum()

    def base_name(merged_name: str) -> str:
        return merged_name.split(" / ")[0]

    def status(theme: str) -> str:
        c = float(cur_amt.get(theme, 0.0))
        p = float(prev_avg.get(theme, 0.0))
        if c > 0 and p == 0:
            return "새로 부상"
        if c == 0:
            return "이번 주 이탈"
        if c >= ACCEL * p:
            return "가속"
        if c <= SLOW * p:
            return "둔화"
        return "유지"

    for name, r in (cur_res.sort_values("합계", ascending=False).iterrows() if len(cur_res) else []):
        b = base_name(name)
        out["current"].append({
            "테마": name, "종목수": int(r["종목수"]), "참여주체수": int(r["참여주체수"]),
            **{inv: float(r[inv]) for inv in INV}, "합계": float(r["합계"]),
            "종목": list(r["종목"]), "상태": status(b), "등장주수": int(weeks_present.get(b, 0)),
        })
    for name, r in cum_res.sort_values("합계", ascending=False).iterrows():
        b = base_name(name)
        out["cumulative"].append({
            "테마": name, "종목수": int(r["종목수"]), "참여주체수": int(r["참여주체수"]),
            **{inv: float(r[inv]) for inv in INV}, "합계": float(r["합계"]),
            "종목": list(r["종목"]), "상태": status(b), "등장주수": int(weeks_present.get(b, 0)),
            "이번주": float(cur_amt.get(b, 0.0)),
        })

    # 종목별 대표 테마 (수급 종목이 모인 테마 중 이번 주 금액이 큰 순으로 최대 2개)
    shown = {base_name(t["테마"]) for t in out["current"]} | {base_name(t["테마"]) for t in out["cumulative"]}
    for r in df[df["주차"] == cur][["종목명", "코드"]].drop_duplicates().itertuples():
        ts = [t for t in code_to_themes.get(r.코드, []) if t in shown]
        ts.sort(key=lambda t: -float(cur_amt.get(t, 0.0)))
        out["stock_themes"][r.종목명] = ts[:2]

    out["insights"] = _insights(out, len(win))
    return out


def _insights(out: dict, n_win: int) -> list[str]:
    cur, cum = out["current"], out["cumulative"]
    lines = []
    if cur:
        top = cur[:3]
        lines.append("이번 주 수급이 가장 몰린 테마: " + ", ".join(
            f"{t['테마'].split(' / ')[0]}({_fmt(t['합계'])}·{t['종목수']}종목)" for t in top) + ".")
        multi = [t for t in cur if t["참여주체수"] >= 3]
        if multi:
            lines.append("외국인·기관·연기금이 함께 들어온 테마: "
                         + ", ".join(t["테마"].split(" / ")[0] for t in multi[:4]) + ".")
        rising = [t for t in cur if t["상태"] in ("새로 부상", "가속")]
        if rising:
            lines.append("새로 부상·가속 중인 테마: " + ", ".join(
                f"{t['테마'].split(' / ')[0]}({t['상태']})" for t in rising[:4]) + ".")
    else:
        lines.append("이번 주에는 수급 종목이 2개 이상 모인 테마가 없습니다 (수급이 흩어진 주).")
    fading = [t for t in cum[:8] if t["상태"] in ("둔화", "이번 주 이탈")]
    if fading:
        lines.append(f"최근 {n_win}주 누적 상위인데 이번 주 식은 테마: " + ", ".join(
            f"{t['테마'].split(' / ')[0]}({t['상태']})" for t in fading[:4]) + ".")
    steady = [t for t in cum if t["등장주수"] >= max(4, n_win - 2)]
    if steady:
        lines.append(f"꾸준한 테마({n_win}주 중 {max(4, n_win - 2)}주 이상 등장): "
                     + ", ".join(t["테마"].split(" / ")[0] for t in steady[:4]) + ".")
    return lines


def commentary(t: dict) -> str:
    """테마 한 줄 해설 (규칙 기반)."""
    who = [inv for inv in INV if t.get(inv, 0) > 0]
    names = ", ".join(t["종목"][:4]) + (" 외" if len(t["종목"]) > 4 else "")
    return (f"{t['종목수']}종목({names}) · 매수 주체 {'·'.join(who) or '-'} · "
            f"이번 주 {_fmt(t.get('이번주', t['합계']))} [{t['상태']}] · 최근 6주 중 {t['등장주수']}주 등장")
