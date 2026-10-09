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
`week` 칸을 비우면 가장 최근에 끝난 주(토·일 실행 시 이번 주), 채우면 그 주차를 수집한다.

---

## 수동 실행

과거 데이터를 채우려면 로컬에서 구간 지정으로 돌린다.

```bash
pip install -r requirements.txt
export NOTION_TOKEN=... NOTION_DATABASE_ID=...

python main.py                          # 가장 최근에 끝난 주
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

## 주도주 일일 스크리닝 (홍인기 단타)

평일 18:30(KST)에 `.github/workflows/daily_leader.yml`이 `leader_main.py`를 실행해
노션 **주도주 스크리닝 (홍인기 단타)** DB에 적재한다.

- **1차 끼**: 등락률 10% 이상 + 거래대금 500억 이상 (우선주·스팩·리츠 제외)
- **2차 주도주**: 끼 종목 중 거래대금 통합 30위 이내 + 네이버 테마 동반상승 3종목 이상 + 60일 신고가 + 몸통비율 0.6 이상
- **물량테스트 의심**: 윗꼬리비율 0.3 이상 + 장중 고점 대비 5%p 이상 밀림. 다음 거래일 실행 때
  종가가 `테스트기준가`(테스트 캔들 중간값) 이상이면 `테스트판정=지지`, 미만이면 `이탈`로 자동 기록한다.
- **테마 출처**: 네이버 증권 테마 → 실패 시 직전 성공 캐시(Actions cache) → 그것도 없으면 KRX 업종. 사용한 출처는 `메모`에 남는다.
- 기준값은 `screener_config.json`에서 바꾼다. 미충족 조건은 노션 `메모` 열에 남는다.
- 새 행은 `판단=관찰`로 들어가며, 직접 바꾼 판단은 재실행해도 덮어쓰지 않는다.

```bash
python leader_main.py --dry-run            # 노션에 쓰지 않고 결과 확인
python leader_main.py --date 2026-10-07    # 특정일 소급
```

## 주간 수급 분석 엑셀 (자동)

토요일 수급 적재가 끝나면 같은 워크플로(`weekly.yml`)가 `weekly_report.py`를 실행한다.

1. 노션 '주간 수급 데이터' DB 전체를 읽어 `report_builder.py`로 엑셀을 만든다
   (`26 10월 둘째주 수급누적.xlsx` 형식, '수급분석' 시트 = 수식 기반 피벗 4종 + 차트 2개 + 자동 요약).
2. LibreOffice로 수식 계산값을 채운다.
3. 노션 '📊 주간 수급 분석 보고서' 아래에 주차별 페이지(요약·신호판·누적 상위·엑셀 첨부)를 만든다.
   같은 주를 다시 돌리면 이전 페이지는 보관(archive)하고 새로 만든다.
4. PC 폴더 저장은 PC의 작업 스케줄러가 맡는다 → 아래 'PC 폴더 동기화'.

수동 실행: Actions → 주간 수급 데이터 수집 → Run workflow → `report_only` 체크 (수집 없이 분석만).
로컬: `python weekly_report.py --from-xlsx 노션내보내기.xlsx --no-deliver`

### PC 폴더 동기화 (Windows, 한 번 설정)

클라우드는 PC에 직접 쓸 수 없으므로, PC가 노션 보고서의 첨부 엑셀을 내려받는다.

1. `tools/install_sync_task.ps1`, `tools/sync_reports.ps1`을 PC의 같은 폴더에 둔다.
2. 그 폴더에서 `powershell -ExecutionPolicy Bypass -File install_sync_task.ps1` 실행
   → 노션 연동 '라르'의 시크릿 입력(이 PC 사용자 계정으로 암호화 저장) → 작업 '주간 수급 분석 동기화' 등록 → 첫 동기화.
3. 매일 08:30 확인하고, 새 보고서가 있을 때만 `C:\Users\S.J.KO\Documents\T_room\stock-autotrader\주간 수급 분석`에 저장한다.
   PC가 꺼져 있었으면 켜진 뒤 바로 실행한다. 기록은 그 폴더의 `_sync.log`.

### 테마별 분석 (`theme_analysis.py`)

주간 수급 종목을 네이버 증권 테마에 대응시켜, **수급 종목이 2개 이상 모인 테마만** 집계한다.

- 이번 주 테마 순위와 최근 6주 누적 테마를 나란히 두고 상태를 붙인다:
  새로 부상(직전 3주 없음) · 가속(직전 3주 평균의 1.5배 이상) · 둔화(0.5배 이하) · 유지 · 이번 주 이탈
- 한 종목이 여러 테마에 속하면 모두 포함한다(테마 합계끼리 중복 가능). 구성 종목이 같은 테마는 ' / '로 묶는다.
- 결과: 엑셀 '테마분석' 시트(표·차트·한 줄 해설), 노션 보고서 '테마별 분석' 섹션, 신호판의 '대표 테마' 열.
- 테마 출처: 네이버 테마 → 실패 시 직전 성공 캐시(Actions cache, 주도주 스크리닝과 공유) → 없으면 섹션 생략.
- 로컬 시험: `python weekly_report.py --from-xlsx 파일.xlsx --themes-json 테마.json --no-deliver`
