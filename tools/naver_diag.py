"""새 네이버 증권(stock.naver.com) 테마 데이터 위치를 찾는 일회성 진단 스크립트."""
import json, re, sys
from pathlib import Path
import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
     "Accept-Language": "ko-KR,ko;q=0.9", "Referer": "https://stock.naver.com/"}
out = Path("diag"); out.mkdir(exist_ok=True)
report = []

def get(url, **kw):
    try:
        r = requests.get(url, headers=H, timeout=20, **kw)
        report.append(f"{r.status_code} {url} -> {r.url} len={len(r.content)} type={r.headers.get('Content-Type')}")
        return r
    except Exception as e:
        report.append(f"ERR {url}: {e}")
        return None

page = get("https://stock.naver.com/market/stock/kr/theme/1")
if page is not None:
    html = page.text
    (out / "theme_page.html").write_text(html, encoding="utf-8")
    nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    report.append(f"__NEXT_DATA__ 존재: {bool(nd)}")
    if nd:
        (out / "next_data.json").write_text(nd.group(1), encoding="utf-8")
    report.append("self.__next_f 조각 수: %d" % html.count("self.__next_f.push"))
    # HTML 안에 보이는 API 경로
    apis = sorted(set(re.findall(r'["\'](/?(?:api|front-api)/[^"\'\s]{3,120})["\']', html)))
    report.append("HTML API 경로: " + json.dumps(apis[:80], ensure_ascii=False))
    # 자바스크립트 묶음에서 API 경로 수집
    chunks = sorted(set(re.findall(r'src="(/_next/static/[^"]+\.js)"', html)))
    report.append(f"JS 조각 {len(chunks)}개")
    found = set()
    for c in chunks:
        r = get("https://stock.naver.com" + c)
        if r is None or not r.ok:
            continue
        for m in re.findall(r'["\'`]((?:https://[a-z.]*naver\.com)?/?(?:api|front-api)/[^"\'`\s]{3,160})["\'`]', r.text):
            found.add(m)
        for m in re.findall(r'["\'`]([^"\'`\s]{0,80}theme[^"\'`\s]{0,80})["\'`]', r.text):
            if "/" in m:
                found.add("THEME? " + m)
    (out / "js_api_paths.txt").write_text("\n".join(sorted(found)), encoding="utf-8")
    report.append(f"JS에서 찾은 경로 {len(found)}개")

# 알려진 후보 주소들
for u in [
    "https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no=1",
    "https://m.stock.naver.com/api/stocks/theme?page=1&pageSize=20",
    "https://m.stock.naver.com/api/stocks/theme/1?page=1&pageSize=20",
    "https://stock.naver.com/api/domestic/market/theme?page=1&pageSize=20",
    "https://m.stock.naver.com/front-api/marketIndex/theme?page=1&pageSize=20",
]:
    r = get(u)
    if r is not None:
        (out / (re.sub(r"\W+", "_", u)[-80:] + ".txt")).write_text(r.text[:5000], encoding="utf-8")

(out / "report.txt").write_text("\n".join(report), encoding="utf-8")
print("\n".join(report))
