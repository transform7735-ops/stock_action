# 주간 수급 데이터 자동 수집

KRX 투자자별 주간 순매수 상위 종목을 매주 월요일 아침에 노션 DB로 적재한다.
PC와 무관하게 GitHub 서버에서 실행된다.

```
GitHub Actions (일요일 22:00 UTC = 월요일 07:00 KST)
   └─ main.py
        ├─ krx_collector.py  → KRX 조회 (pykrx)
        └─ notion_sync.py    → 노션 DB upsert
```

수집 대상: **외국인 / 기관합계 / 연기금** × KOSPI+KOSDAQ 통합 상위 10종목.

---

## 세팅

### 1. 저장소 만들기

GitHub에서 새 저장소를 만들고 이 폴더의 파일을 전부 올린다.
웹 UI의 "uploading an existing file"로 드래그해도 된다.

### 2. 노션 통합 연결

1. https://www.notion.so/my-integrations 에서 새 integration 생성
2. Internal Integration Token 복사 (`ntn_`으로 시작)
3. **주간 수급 데이터** DB 페이지 → 우측 상단 `...` → 연결 → 만든 integration 선택

3번을 빠뜨리면 토큰이 맞아도 DB가 안 보인다. 가장 흔한 실패 지점이다.

### 3. Secrets 등록

저장소 → Settings → Secrets and variables → Actions → New repository secret

| 이름 | 값 |
|---|---|
| `NOTION_TOKEN` | 2단계에서 복사한 토큰 |
| `NOTION_DATABASE_ID` | `9dc2c57b5a0f4f16a2d2cd56c58a7041` |

### 4. 첫 실행

Actions 탭 → "주간 수급 데이터 수집" → Run workflow.
`week` 칸을 비우면 지난주, 채우면 그 주차를 수집한다.

---

## 수동 실행

과거 데이터를 채우려면 로컬에서 구간 지정으로 돌린다.

```bash
pip install -r requirements.txt
export NOTION_TOKEN=... NOTION_DATABASE_ID=...

python main.py                          # 지난주
python main.py --week 2026-W35          # 특정 주차
python main.py --weeks 2026-W30 2026-W35  # 구간 일괄
python main.py --dry-run                # 노션에 쓰지 않고 확인만
```

`--dry-run`으로 먼저 결과를 눈으로 보고 적재하는 습관을 권한다.

---

## 알아둘 것

**중복 적재는 일어나지 않는다.** 각 행에 `주차_종목코드_투자주체` 형태의 레코드키가 있고,
적재 전에 이 키로 조회해서 있으면 갱신한다. 같은 주를 열 번 돌려도 행은 늘지 않는다.

**연속순매수일은 종목마다 별도 조회가 필요하다.** 30건이면 KRX에 30번 더 묻는다.
느리면 `--no-streak`으로 끌 수 있지만, 이 값이 금액보다 신호가 강하므로 켜두길 권한다.

**연기금 데이터는 지연 공시된다.** 장 마감 직후가 아니라 며칠 뒤 확정되는 경우가 있다.
월요일 아침 수집이 비어 있으면 며칠 뒤 같은 주차로 다시 돌리면 갱신된다.

**섹터는 자동으로 붙지 않는다.** `sectors.json`에 종목코드→섹터를 직접 채운다.
없는 종목은 비워둔 채 적재되므로 노션에서 손으로 골라도 된다.

**공개 저장소는 60일간 커밋이 없으면 스케줄이 자동 중단된다.**
GitHub이 메일로 알려주니 그때 아무 커밋이나 하나 넣으면 되살아난다.
비공개 저장소는 해당 없다.

**pykrx는 KRX 정보데이터시스템의 공개 화면을 조회한다.** 공식 계약 API가 아니라,
KRX가 화면 구조를 바꾸면 깨질 수 있다. 그때는 pykrx를 최신 버전으로 올리면 대개 해결된다.

---

본 도구는 데이터 수집용이며 투자 자문이 아니다.
