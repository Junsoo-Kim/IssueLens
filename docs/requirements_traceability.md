# 요구사항 대조표

요구사항 정의서의 기능(F2001~F2012)과 비기능(NF2001~NF2007) 요구사항마다 구현 위치, 증빙, 결과를 정리한다. 요구사항 이름은 기획서 기준 요약이며, 제출 시 정의서 원문 표기로 맞춘다. 증빙 번호는 [체크리스트](evidence_checklist.md)의 번호다.

결과 표기: `PASS` 확인 완료, `해당 없음`. 화면 항목(10~13)은 `scripts/capture_ui.py`로 캡처했다 (README의 화면).

## 기능 요구사항

| ID | 요구사항 (요약) | 구현 위치 | 자동 테스트 | 증빙 | 결과 |
|---|---|---|---|---|---|
| F2001 | 항목 메타데이터 적재, 로드 수와 색인 수 일치, 색인 텍스트 확인 | `src/preprocessing/metadata_builder.py`, `src/indexing/metadata_indexer.py`, `scripts/build_indexes.py` | `tests/test_indexing.py`, `tests/test_builders.py` | 01 | PASS |
| F2002 | 재색인 시 중복 없음 (개수·ID 불변) | `src/indexing/ids.py`, `src/indexing/chroma_store.py`, `scripts/verify_idempotency.py` | `tests/test_indexing.py` | 02 | PASS |
| F2003 | 질문에서 검색어 추출 (프롬프트 1) | `src/prompts/query_extractor.py`, `src/graph/nodes.py` | `tests/test_prompts.py` | 03 | PASS |
| F2004 | 외부 문서 후보 선별(프롬프트 2)과 수집, 재수집 없음 | `src/prompts/candidate_selector.py`, `src/collectors/query_time.py` | `tests/test_graph.py` (질문 시점 수집·캐시) | 03, 04 | PASS |
| F2005 | 문서 청킹, 설정 변경 시 청크 수 변화 | `src/indexing/document_indexer.py`, `configs/retrieval.yaml` | `tests/test_indexing.py` | 05 | PASS |
| F2006 | Hybrid 검색, Top-k 변경 시 근거 목록 변화 | `src/retrieval/hybrid.py`, `rrf_fusion.py`, `evidence_selector.py` | `tests/test_retrieval.py` | 06 | PASS |
| F2007 | LangGraph `retrieve`, `generate` 노드 | `src/graph/workflow.py`, `src/graph/nodes.py` | `tests/test_graph.py` | 07, 09 | PASS |
| F2008 | 근거 번호·출처 표시, 인용 검증, 근거 없음 거부 | `src/prompts/answer_generator.py`, `src/services/citation_validator.py`, `src/ui/sources.py` | `tests/test_citations.py`, `tests/test_graph.py` | 08, 09 | PASS |
| F2009 | 이슈 선택 대화, 이슈별 대화 분리 | `src/ui/state.py`, `src/ui/chat.py`, `src/ui/sidebar.py` | `tests/test_ui.py` | 10 | PASS |
| F2010 | 도메인 특화: 로그 기반 유사 장애 진단 | `src/services/incident_diagnoser.py`, `src/ui/chat.py` | `tests/test_services.py`, `tests/test_ui.py` | 12 | PASS |
| F2011 | 자연어 조건 기반 이슈 탐색 | `src/services/issue_explorer.py`, `src/prompts/condition_search.py` | `tests/test_services.py`, `tests/test_ui.py` | 11 | PASS |
| F2012 | 처리 상태 표시, 오류 안내, 안전 안내 | `src/graph/nodes.py`(단계 보고), `src/ui/main.py`, `src/ui/state.py` | `tests/test_ui.py` | 13, 14 | PASS (14는 해당 없음) |
| F2013~F2016 | 심화 기능 | 선택하지 않음 (기획서 0장) | | 15~19 | 해당 없음 |

## 비기능 요구사항

| ID | 요구사항 (요약) | 구현 위치 | 증빙 | 결과 |
|---|---|---|---|---|
| NF2001 | 응답 시간 측정 (구간별) | `src/graph/nodes.py`(timings), `scripts/run_checks.py` | 20 (04 파일) | PASS |
| NF2002 | 같은 요청 재수집 없음 (캐시) | `src/collectors/cache.py`(ETag), `src/collectors/query_time.py`(검색어 캐시) | 04 | PASS |
| NF2003 | API 키를 코드와 저장소에 두지 않음 | `.env`(`.gitignore`), `.env.example`, `src/services/assistant.py` | 21 | PASS |
| NF2004 | 화면에 키·프롬프트 전문·스택 트레이스 비노출 | `src/ui/chat.py`(예외는 서버 로그만), `src/ui/sources.py` | 21, 13 | PASS |
| NF2005 | 수집·색인·검색·생성·화면 모듈 분리 | `src/` 하위 패키지 | 22 | PASS |
| NF2006 | API 예절 (인증, 간격, 재시도, Rate Limit, User-Agent) | `src/collectors/github_client.py`, `configs/app.yaml` | 23 | PASS |
| NF2007 | 데이터 출처와 이용 조건 기록 | `README.md`, `data/manifests/source_manifest.json` | 24 | PASS |

## 검증 질문 세트

`scripts/run_eval.py` (17개), 2026-10-22 제출 전 전체 실행: **PASS 16 / FAIL 1**

| 유형 | 결과 |
|---|---|
| 근거 있는 질문 (q01~q05) | 5 / 5 |
| 로그 진단 (q06~q09, q17) | 4 / 5 |
| 근거 없는 질문 (q10~q12) | 3 / 3 |
| 조건 탐색 (q13, q14) | 2 / 2 |
| 선택 이슈 대화 (q15, q16) | 2 / 2 |

- 실패 q09 (`thingsboard/thingsboard#15636` 로그 진단): 1·2차 시도는 정확 토큰이 근거에 없어 부족, 3차 질문 시점 수집 후 검색에서 저장소를 하나로 좁히지 못해 다른 제품(`conductor-oss/conductor#423`)의 근거를 함께 인용했다. 같은 gRPC 연결 끊김 증상이라 내용상 무관하지는 않지만 다른 제품 근거 인용 금지 기준에 따라 실패로 본다. 개선 방향: 3차 수집 후 검색 범위를 수집한 저장소로 한정
- 이전 실행(PASS 14 / FAIL 2)의 실패 원인(토큰 추출, 저장소 지정 조건, 데이터에 근거가 없는 질문의 기대값)은 수정했다
