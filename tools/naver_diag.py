"""네이버 모바일 테마 API 구조 확인용 일회성 진단."""
import json, re
from pathlib import Path
import requests
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
     "Referer": "https://m.stock.naver.com/"}
out = Path("diag"); out.mkdir(exist_ok=True)
rep = []
def j(url):
    r = requests.get(url, headers=H, timeout=20)
    rep.append(f"{r.status_code} {url} len={len(r.content)}")
    try: return r.json()
    except Exception: return {"_text": r.text[:500]}
lst = j("https://m.stock.naver.com/api/stocks/theme?page=1&pageSize=100")
rep.append("목록 키: " + ",".join(lst.keys()) + f" / groups {len(lst.get('groups', []))}개 / totalCount={lst.get('totalCount')}")
p9 = j("https://m.stock.naver.com/api/stocks/theme?page=9&pageSize=100")
rep.append(f"9페이지 groups {len(p9.get('groups', []))}개")
for no in (591, 608):
    for q in ("page=1&pageSize=100", "page=1&pageSize=20", ""):
        d = j(f"https://m.stock.naver.com/api/stocks/theme/{no}?{q}")
        st = d.get("stocks", [])
        rep.append(f"  theme {no} [{q}] stocks={len(st)} totalCount={d.get('totalCount')}")
        if st and no == 591 and q.startswith("page=1&pageSize=100"):
            (out / "detail_591.json").write_text(json.dumps(d, ensure_ascii=False, indent=1)[:6000], encoding="utf-8")
(out / "list.json").write_text(json.dumps(lst, ensure_ascii=False, indent=1)[:3000], encoding="utf-8")
(out / "report.txt").write_text("\n".join(rep), encoding="utf-8")
print("\n".join(rep))
