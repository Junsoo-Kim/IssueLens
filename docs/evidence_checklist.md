# 요구사항 정의서 4장 증빙 체크리스트

기획서 15장의 27개 항목을 어떻게 만들고 무엇을 확인하는지 정리한다. 명령 출력 항목은 `scripts/export_evidence.py`가 `data/processed/evidence/`에 텍스트로 만들고(커밋 제외), 화면 항목은 아래 순서대로 Streamlit에서 캡처한다. 캡처 파일명은 오른쪽 열을 따른다.

```bash
python scripts/export_evidence.py              # 01~09, 21~24 (02는 재색인이라 몇 분 걸림)
python scripts/export_evidence.py --only 04 06 # 일부만
python scripts/run_eval.py                     # 검증 질문 세트 (08·09·11·12 보조 근거)
streamlit run app.py                           # 10~13 화면 캡처
```

## 명령 출력 항목

| 번호 | 요구사항 | 만드는 방법 | 확인할 것 | 파일 |
|---:|---|---|---|---|
| 1 | F2001 | `export_evidence.py --only 01` | 최종 선별 색인 대상 수 = RAG 1 항목 수, 색인 텍스트 예시 | `01_f2001_count_and_text` |
| 2 | F2002 | `export_evidence.py --only 02` (`verify_idempotency.py` 실행) | 재임베딩 신규·갱신·삭제 0, 재색인 전후 개수와 ID 집합 동일, `결과: PASS` | `02_f2002_idempotent` |
| 3 | F2003, F2004 | `export_evidence.py --only 03` (마지막 질문 시점 수집 기록) | 프롬프트 1 검색어, 후보, 프롬프트 2 선택·탈락과 이유 | `03_f2003_f2004_selection` |
| 4 | F2004, NF2002 | `export_evidence.py --only 04` | 같은 질문 두 번째 실행에서 GitHub 검색 0회, 캐시 적중 | `04_cache_hit_and_latency` |
| 5 | F2005 | `export_evidence.py --only 05` | 청크 크기·겹침을 절반으로 바꾸면 청크 수가 달라짐 (색인은 바꾸지 않음) | `05_chunk_count_compare` |
| 6 | F2006 | `export_evidence.py --only 06` | 이슈 Top-k·근거 상한을 바꾸면 근거 목록이 달라짐 | `06_topk_compare` |
| 7 | F2007 | `export_evidence.py --only 07` 또는 `ask.py --graph` | 그래프에 `retrieve`, `generate` 두 노드 | `07_langgraph_structure` |
| 8 | F2008 | `export_evidence.py --only 08` | 답변의 근거 번호 = 화면 근거 목록 번호, 출처 유형(이슈 본문·댓글·PR·유지보수자) 표시 | `08_citation_match` |
| 9 | F2007, F2008 | `export_evidence.py --only 09` | 색인에 없는 질문에 "제공된 문서에서 확인되지 않습니다." | `09_abstention` |
| 20 | NF2001 | `04` 파일의 처리 시간 줄 | 첫 실행과 캐시 실행의 구간별 시간 (query_plan, 시도별, generate) | `20_latency` (04 파일 사용) |
| 21 | NF2003, NF2004 | `export_evidence.py --only 21` + 13번 화면 | 커밋 파일에 키 문자열 0건, `.env` 제외. 오류 화면에 키·스택 트레이스 없음 | `21_security_check` |
| 22 | NF2005 | `export_evidence.py --only 22` | 수집·정제·색인·검색·프롬프트·그래프·서비스·화면 모듈 분리 | `22_module_structure` |
| 23 | NF2006 | `export_evidence.py --only 23` | User-Agent, API 버전 헤더, Retry-After, Rate Limit 대기, 요청 간격, 재시도, ETag | `23_api_etiquette` |
| 24 | NF2007 | `export_evidence.py --only 24` | README의 데이터 출처와 이용 조건, 저장소별 라이선스 | `24_sources_and_terms` |

## 화면 캡처 항목 (`streamlit run app.py`)

| 번호 | 요구사항 | 조작 순서 | 확인할 것 | 파일 |
|---:|---|---|---|---|
| 10 | F2009 | ① 사이드바에서 이슈 A(예: `airbytehq/airbyte#9784`) 선택 → "이 문제를 해결하려면 어떤 설정을 확인해야 해?" ② 이슈 B(예: `datahub-project/datahub#13067`) 선택 → 대화가 비어 있음 확인 후 다른 질문 ③ 다시 이슈 A 선택 | B에서 A의 대화가 보이지 않고, A로 돌아오면 A의 대화가 남아 있음. 선택 이슈 헤더 표시 | `10_session_isolation.png` |
| 11 | F2011 | 조건 탐색 탭 → "Airbyte MySQL 대용량 테이블 동기화가 타임아웃으로 실패한 버그 찾아줘" → 결과 카드의 저장소·유형·라벨을 GitHub 원문과 비교 | 추출 조건, 조건별 제외 건수, 카드 메타데이터가 원문과 일치 | `11_condition_search.png` |
| 12 | F2010 | 로그 진단 탭 → `python scripts/sample_logs.py --repo conductor-oss/conductor`의 로그 붙여넣기 → 진단 | 로그 신호, 유사 종료 이슈(근거 사용 표시), [근거 N]이 붙은 진단 | `12_incident_diagnosis.png` |
| 13 | F2012 | ① 질문 중 상태 상자 펼쳐 진행 단계 캡처 ② `.env`의 `OPENAI_API_KEY`를 비우고 재실행 → 키 없음 안내 ③ `chroma_db` 폴더 이름을 잠시 바꾸고 재실행 → 색인 없음 안내 (확인 후 원래대로) ④ 근거 없는 질문 → 검색 결과 없음 안내 | 단계 표시와 세 가지 안내 문구, 화면에 키·스택 트레이스 없음 | `13_status_and_errors.png` |

## 해당 없음

| 번호 | 요구사항 | 사유 |
|---:|---|---|
| 14 | F2012 (위험 도메인 면책) | 건강·금융 도메인이 아니어서 해당 없음. 기술 조사 보조 안전 안내는 모든 답변 아래에 표시 |
| 15~19 | F2013~F2016 | 심화 기능은 선택하지 않음 (기획서 0장) |

## 제출물 점검

| 번호 | 내용 | 만드는 방법 | 파일 |
|---:|---|---|---|
| 25 | 요구사항 전체 결과 집계 | 이 문서의 표에 항목별 PASS/해당 없음/미확인을 적는다 (`export_evidence.py` 요약 + 캡처 확인) | `25_requirement_summary.png` |
| 26 | 증빙 파일 목록과 실제 파일 대조 | `ls data/processed/evidence` 와 캡처 폴더 목록을 이 문서의 파일 열과 비교 | `26_evidence_manifest.png` |
| 27 | 제출물 폴더 구조 | `git ls-files \| sed 's#/[^/]*$##' \| sort -u` 또는 `tree -L 2 -I '.venv\|data\|chroma_db\|sparse_index'` | `27_submission_tree.png` |
