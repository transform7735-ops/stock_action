"""
주간 수급 분석 엑셀 생성 -> 노션 보고서 페이지(요약·표·엑셀 첨부).

토요일 수급 적재(main.py)가 끝난 뒤 같은 워크플로에서 실행된다.
PC 폴더 저장은 PC의 작업 스케줄러가 tools/sync_reports.ps1로 노션 첨부 엑셀을 내려받아 처리한다.

사용법:
    python weekly_report.py                         # 노션 DB 전체로 만들고 노션 보고서까지
    python weekly_report.py --no-deliver            # 파일만 만든다
    python weekly_report.py --from-xlsx 파일.xlsx    # 노션 대신 내보낸 엑셀로 만든다 (스킬용)

환경변수:
    NOTION_TOKEN, NOTION_DATABASE_ID          노션 수급 DB 읽기 (필수, --from-xlsx면 불필요)
    NOTION_REPORT_PAGE_ID                     노션 보고서를 만들 상위 페이지
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
import requests

from report_builder import RAW_COLUMNS, build_workbook

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("weekly_report")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def gh_annotate(level: str, msg: str) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level} title=주간 수급 분석::{msg.replace(chr(10), ' ')}", flush=True)


# --- 노션 공통 -------------------------------------------------------------------

def notion_headers(token: str, json_body: bool = True) -> dict:
    h = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def notion_call(method: str, path: str, token: str, **kw) -> dict:
    for attempt in range(5):
        resp = requests.request(method, f"{NOTION_API}{path}", headers=notion_headers(token), timeout=60, **kw)
        if resp.status_code == 429 or resp.status_code >= 500:
            time.sleep(int(resp.headers.get("Retry-After", 2 ** attempt)))
            continue
        if not resp.ok:
            raise RuntimeError(f"노션 {method} {path} -> {resp.status_code} {resp.text[:300]}")
        time.sleep(0.35)
        return resp.json()
    raise RuntimeError(f"노션 {method} {path} 재시도 초과")


def _prop_value(p: dict):
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v) or None
    if t in ("number", "url", "created_time", "email", "phone_number"):
        return v
    if t == "select":
        return v["name"] if v else None
    if t == "multi_select":
        return ", ".join(x["name"] for x in v) or None
    if t == "date":
        if not v:
            return None
        return f"{v['start']} → {v['end']}" if v.get("end") else v["start"]
    return None  # relation 등은 분석에 쓰지 않는다


def fetch_notion_rows(token: str, db_id: str) -> pd.DataFrame:
    rows, cursor = [], None
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        data = notion_call("POST", f"/databases/{db_id}/query", token, json=body)
        for page in data["results"]:
            props = page["properties"]
            rows.append({c: _prop_value(props[c]) if c in props else None for c in RAW_COLUMNS})
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    log.info("노션 수급 DB %d행 읽음", len(rows))
    return pd.DataFrame(rows, columns=RAW_COLUMNS)


# --- 수식 재계산 (계산값을 파일에 넣어 미리보기에서도 숫자가 보이게) ------------------

RECALC_MACRO = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">
<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1" script:language="StarBasic">
    Sub RecalculateAndSave()
      ThisComponent.calculateAll()
      ThisComponent.store()
      ThisComponent.close(True)
    End Sub
</script:module>"""


def recalc(path: Path) -> bool:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        log.warning("LibreOffice 없음: 계산값 없이 저장 (엑셀은 열 때 계산함)")
        return False
    with tempfile.TemporaryDirectory() as prof:
        url = Path(prof).as_uri()
        subprocess.run([soffice, "--headless", "--terminate_after_init", f"-env:UserInstallation={url}"],
                       capture_output=True, timeout=120)
        macro_dir = Path(prof) / "user" / "basic" / "Standard"
        if not macro_dir.exists():
            log.warning("LibreOffice 프로필 생성 실패: 재계산 생략")
            return False
        (macro_dir / "Module1.xba").write_text(RECALC_MACRO, encoding="utf-8")
        before = path.stat().st_mtime_ns
        res = subprocess.run(
            [soffice, "--headless", "--norestore", f"-env:UserInstallation={url}",
             "vnd.sun.star.script:Standard.Module1.RecalculateAndSave?language=Basic&location=application",
             str(path.resolve())],
            capture_output=True, text=True, timeout=240)
        ok = res.returncode == 0 and path.stat().st_mtime_ns != before
        log.info("수식 재계산 %s", "완료" if ok else f"실패: {res.stderr[:200]}")
        return ok


# --- 노션 보고서 페이지 --------------------------------------------------------------

def _rt(text: str, bold: bool = False, link: str | None = None) -> list:
    t = {"type": "text", "text": {"content": str(text)[:2000]}}
    if link:
        t["text"]["link"] = {"url": link}
    if bold:
        t["annotations"] = {"bold": True}
    return [t]


def _table(header: list[str], rows: list[list]) -> dict:
    def row(cells):
        return {"type": "table_row", "table_row": {"cells": [_rt(c) for c in cells]}}
    return {"type": "table", "table": {"table_width": len(header), "has_column_header": True,
                                       "has_row_header": False, "children": [row(header)] + [row(r) for r in rows]}}


def notion_report(info: dict, path: Path) -> str | None:
    token, parent = os.environ.get("NOTION_TOKEN"), os.environ.get("NOTION_REPORT_PAGE_ID")
    if not (token and parent):
        log.info("노션 보고서 상위 페이지 설정 없음: 건너뜀")
        return None
    title = f"{info['title']} 수급 분석 ({info['week']})"

    # 같은 주 보고서가 있으면 보관(archive)하고 새로 만든다
    cursor = None
    while True:
        q = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        data = notion_call("GET", f"/blocks/{parent}/children{q}", token)
        for b in data["results"]:
            if b["type"] == "child_page" and b["child_page"]["title"] == title:
                notion_call("PATCH", f"/pages/{b['id']}", token, json={"archived": True})
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]

    eok = lambda v: f"{v:,.0f}" if v else "-"
    children = [
        {"type": "callout", "callout": {"icon": {"type": "emoji", "emoji": "📌"},
                                        "rich_text": _rt(f"{info['week']} 기준 · 최근 {len(info['window'])}주 누적 · 금액 억원 · 주체별 상위 10종목 기준")}},
        {"type": "heading_2", "heading_2": {"rich_text": _rt("핵심 요약")}},
        *[{"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(t)}} for t in info["insights"]],
        {"type": "heading_2", "heading_2": {"rich_text": _rt("이번 주 수급 신호판")}},
        _table(["종목명", "외국인", "기관합계", "연기금", "참여", "연속(일)", "주간등락(%)", "신호"],
               [[s["종목명"], eok(s["외국인"]), eok(s["기관합계"]), eok(s["연기금"]), s["참여"], s["연속"],
                 f"{s['등락']:+.2f}", s["신호"] or "-"] for s in info["signals"]]),
        {"type": "heading_2", "heading_2": {"rich_text": _rt(f"최근 {len(info['window'])}주 누적 상위 10")}},
        _table(["순위", "종목명", "외국인", "기관합계", "연기금", "합계"],
               [[i + 1, c["종목명"], eok(c.get("외국인", 0)), eok(c.get("기관합계", 0)), eok(c.get("연기금", 0)),
                 eok(c["합계"])] for i, c in enumerate(info["cumulative"])]),
        {"type": "heading_2", "heading_2": {"rich_text": _rt("엑셀 원본")}},
    ]
    children.append({"type": "paragraph", "paragraph": {"rich_text": _rt(
        "아래 첨부 엑셀은 PC의 '주간 수급 분석' 폴더로 자동 동기화됩니다 (tools/sync_reports.ps1).")}})
    page = notion_call("POST", "/pages", token, json={
        "parent": {"page_id": parent}, "icon": {"type": "emoji", "emoji": "📊"},
        "properties": {"title": {"title": _rt(title)}}, "children": children})

    # 엑셀 파일 첨부 (노션 파일 업로드 API)
    try:
        fu = notion_call("POST", "/file_uploads", token, json={"filename": path.name, "content_type": XLSX_MIME})
        r = requests.post(f"{NOTION_API}/file_uploads/{fu['id']}/send", headers=notion_headers(token, json_body=False),
                          files={"file": (path.name, path.read_bytes(), XLSX_MIME)}, timeout=120)
        if not r.ok:
            raise RuntimeError(f"{r.status_code} {r.text[:200]}")
        notion_call("PATCH", f"/blocks/{page['id']}/children", token, json={"children": [{
            "type": "file", "file": {"type": "file_upload", "file_upload": {"id": fu["id"]}, "name": path.name,
                                     # 노션이 파일명의 공백을 _로 바꿔 저장하므로, PC 동기화가 쓸 원래 이름을 캡션에 남긴다
                                     "caption": _rt(path.name)}}]})
    except Exception as exc:
        # PC 폴더 동기화가 이 첨부를 내려받으므로 실패하면 실행 전체를 실패로 알린다
        raise RuntimeError(f"엑셀 첨부 실패(보고서 본문은 생성됨): {exc}") from exc
    log.info("노션 보고서: %s", page["url"])
    return page["url"]


# --- 실행 -----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-xlsx", help="노션 대신 이 엑셀(노션 내보내기 형식)로 만든다")
    ap.add_argument("--out", default="reports", help="엑셀을 저장할 폴더")
    ap.add_argument("--no-deliver", action="store_true", help="노션 보고서를 만들지 않는다")
    ap.add_argument("--no-recalc", action="store_true")
    args = ap.parse_args()

    if args.from_xlsx:
        df = pd.read_excel(args.from_xlsx)
    else:
        token, db = os.environ.get("NOTION_TOKEN"), os.environ.get("NOTION_DATABASE_ID")
        if not (token and db):
            log.error("NOTION_TOKEN 또는 NOTION_DATABASE_ID가 없다.")
            return 1
        df = fetch_notion_rows(token, db)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_building.xlsx"
    info = build_workbook(df, str(tmp))
    path = out_dir / info["filename"]
    tmp.replace(path)
    if not args.no_recalc:
        recalc(path)
    log.info("엑셀 생성: %s", path)
    gh_annotate("notice", f"{info['filename']} 생성 · " + info["insights"][1][:120])

    if args.no_deliver:
        print(json.dumps({k: info[k] for k in ("week", "filename", "insights")}, ensure_ascii=False, indent=2))
        return 0

    try:
        notion_report(info, path)
    except Exception as exc:
        log.error("노션 보고서 실패: %s", exc)
        gh_annotate("error", f"노션 보고서 실패: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
