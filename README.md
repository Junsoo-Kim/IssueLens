# IssueLens

오픈소스 해결 이력 기반 장애 진단 RAG 챗봇입니다. 공개 오픈소스 저장소에서 해결된 이슈, 유지보수자 댓글, 해결 PR을 모아 색인하고, 장애 증상이나 오류 로그를 물으면 실제 근거를 인용해 원인 후보와 확인 절차를 답합니다. 근거가 없으면 지어내지 않고 "제공된 문서에서 확인되지 않습니다."라고 답합니다.

- **결과물 1 (제출 대상, 구현 완료)**: 장애 진단 RAG 챗봇 (Streamlit, CLI)
- **결과물 2 (확장, 구현 완료·실사용 검증 중)**: Python AI·LLM 프로젝트용 PR 선제 리뷰 에이전트 (GitHub Action). [사용법](#pr-선제-리뷰-에이전트), 설계는 [기획서 17장](docs/IssueLens_기획서.md)

> 이 답변은 공개 이슈와 문서를 검색해 제공하는 기술 조사 보조 결과입니다. 생성된 명령과 코드 변경은 저장소의 기여 규칙을 확인하고 격리된 환경에서 재현 테스트를 수행한 뒤 적용해야 합니다.

## 주요 기능

| 기능 | 설명 | 요구사항 |
|---|---|---|
| 근거 기반 질의응답 | 질문 → 검색어 추출(프롬프트 1) → Hybrid 검색 → 근거 판정 → 근거만 사용한 답변(프롬프트 3)과 [근거 N] 인용 | F2006~F2008 |
| 재시도와 질문 시점 수집 | 근거가 부족하면 ① 원래 질문 ② 영어 재작성 검색어 ③ GitHub에서 관련 이슈를 찾아 선별(프롬프트 2)·색인한 뒤 재검색. 같은 검색어는 캐시를 써서 다시 호출하지 않음 | F2003~F2004, NF2002 |
| 답변 거부 | 세 번 모두 근거가 부족하면 답하지 않고 가까운 사례 링크만 표시. RAG 미사용 일반 답변은 사용자가 켠 경우에만 별도 섹션으로 표시 | F2008 |
| 이슈 선택 대화 | 이슈를 고르면 그 이슈의 메타데이터와 본문을 항상 근거에 넣고, 이슈마다 대화를 따로 저장 | F2009 |
| 조건 탐색 | "Keycloak 26 업그레이드 후 토큰 검증이 실패한 종료 버그를 찾아줘" → 저장소·유형·버전·예외명 필터 + 증상 검색 + LLM 조건 판정 | F2011 |
| 로그 진단 | 스택 트레이스에서 예외·라이브러리·버전·환경을 뽑고 유사 종료 이슈와 근거 있는 원인 후보 제시 | F2010 |
| 처리 상태와 오류 안내 | 단계별 진행 표시, 색인 없음·키 없음·검색 결과 없음·호출 한도 안내. 키·프롬프트·스택 트레이스는 화면에 표시하지 않음 | F2012, NF2004 |

## 구조

```text
GitHub REST API -> 수집기(얕은 수집 7,319건 -> 상세 수집) -> 정제·마스킹·해결 근거 판정
  -> RAG 1 issue_metadata (이슈 580건)  +  RAG 2 issue_documents (본문·댓글·PR, 청크 10,520개)
     각각 Chroma(Dense, text-embedding-3-small) + BM25(Sparse), RRF로 결합
  -> LangGraph: retrieve (최대 3회 시도, 근거 판정) -> generate (근거 기반 답변, 인용 검증)
  -> Streamlit 화면 / CLI
```

| 폴더 | 역할 |
|---|---|
| `src/collectors/` | GitHub 클라이언트(재시도, Rate Limit, ETag 캐시), 이슈·댓글·타임라인·PR 수집, 질문 시점 수집 |
| `src/preprocessing/` | 정제, 비밀정보 마스킹, 태깅, 해결 근거 판정, 문서 생성 |
| `src/indexing/` | Chroma, BM25, 결정적 ID, 임베딩 |
| `src/retrieval/` | 질의 분석, 저장소 라우팅, Dense·Sparse·RRF, 근거 선별 |
| `src/prompts/` | 프롬프트 1~3, 근거 판정, 조건 탐색, LLM 호출 |
| `src/graph/` | LangGraph `retrieve`, `generate` |
| `src/services/` | 조립, 조건 탐색, 로그 진단, 인용 검증 |
| `src/ui/` | Streamlit 화면 |
| `scripts/` | 수집, 색인, 질의, 검증, 증빙 CLI |
| `configs/` | 앱·검색·저장소 설정 |

## 설치

Python 3.10 이상.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env               # 키를 채운다
```

`.env` (Git에 올리지 않습니다):

| 변수 | 설명 |
|---|---|
| `GITHUB_TOKEN` | 수집과 질문 시점 수집용. 공개 저장소 읽기 전용 Fine-grained 토큰 권장 |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | 채팅·임베딩. 주소는 `/v1`까지만 적습니다 |
| `OPENAI_EMBEDDING_BASE_URL`, `OPENAI_EMBEDDING_API_KEY` | 임베딩 주소·키가 채팅과 다를 때만 |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY` | 선택. 키 없이 `true`면 자동으로 끕니다 |

연결 확인: `python scripts/check_openai.py` (키는 출력하지 않습니다)

## 데이터 수집과 색인

수집 원본과 색인은 이슈 원문을 담고 있어 **로컬에만 둡니다** (`data/raw`, `data/processed`, `chroma_db`, `sparse_index`는 `.gitignore`). 저장소에는 원문 없는 manifest만 커밋돼 있습니다.

```bash
python scripts/collect_issues.py --mode shallow                 # 저장소별 최대 1,000건 얕은 수집
python scripts/tag_and_select.py                                # 태깅, 상세 수집 대상 선별
python scripts/collect_issues.py --mode deep --selection-file data/manifests/deep_selection.jsonl \
    --include-comments --include-timeline --include-linked-prs  # 댓글·타임라인·연결 PR
python scripts/inspect_deep.py                                  # 해결 근거 판정, 색인/평가/예비 분리
python scripts/analyze_scope.py                                 # 범위 확정 점검
python scripts/build_indexes.py                                 # 문서·청크·Chroma·BM25 (바뀐 레코드만 임베딩)
```

수집은 `--resume`으로 중단 지점부터 이어갈 수 있고, 같은 요청은 ETag 캐시로 다시 받지 않습니다.

## 실행

```bash
streamlit run app.py                                            # 화면 (질의응답, 조건 탐색, 로그 진단)
python scripts/ask.py "sync job fails with timeout on large MySQL tables"
python scripts/ask.py "이 문제 다른 버전에도 있나요?" --issue airbytehq/airbyte#9784
python scripts/ask.py "..." --allow-general                     # 근거가 없으면 RAG 미사용 일반 답변도 표시
python scripts/ask.py "..." --no-collect                        # 질문 시점 수집 끄기
python scripts/ask.py --graph                                   # LangGraph 구조 (Mermaid)
```

## 검증

| 명령 | 내용 |
|---|---|
| `pytest` | 단위·통합·화면(AppTest) 테스트. 가짜 LLM·임베딩·GitHub 응답 사용 |
| `python scripts/run_eval.py` | 검증 질문 세트(`data/eval/questions.yaml`, 17개)를 실행해 질문마다 PASS/FAIL |
| `python scripts/run_checks.py` | 대표 질문과 평가용 이슈 로그 일괄 점검 요약 |
| `python scripts/verify_idempotency.py` | 재색인 전후 개수·ID 집합 불변 확인 |
| `python scripts/export_evidence.py` | 요구사항 정의서 4장 증빙 중 명령 출력 항목 생성 ([체크리스트](docs/evidence_checklist.md)) |

평가용 이슈(160건)는 색인에서 격리돼 있어, 그 이슈의 로그로 진단할 때 원본이 결과에 섞이지 않는지 확인합니다. 검증 출력 파일은 원문 일부를 담을 수 있어 `data/processed/` 아래(커밋 제외)에 저장합니다.

## PR 선제 리뷰 에이전트

Python AI·LLM 프로젝트의 PR을 읽고, 바뀐 코드가 쓰는 라이브러리(OpenAI, LangChain, Pydantic 등 18개)의 해결된 이슈·유지보수자 답변·Discussions 채택 답변을 근거로 리뷰 코멘트를 하나 남깁니다.

```text
PR 변경 파일 -> 마스킹 -> 변경 신호(의존성·import·API 호출·설정) -> 패키지 -> 라이브러리 저장소
  -> 리뷰 후보 추출(프롬프트 4) -> 후보별 3단계 검색·근거 판정(챗봇 RAG 코어 재사용) -> 근거 기반 해결 방법
  -> 코멘트 "알려진 이슈 기반 (RAG)" + "일반 코드 리뷰 (RAG 미사용)" -> 기존 코멘트 갱신 또는 새로 게시
```

| 명령 | 내용 |
|---|---|
| `python scripts/check_agent_corpus.py` | 코퍼스 저장소 목록 점검 (규모, Discussions, PyPI 매핑) |
| `python scripts/build_agent_corpus.py` | 라이브러리 코퍼스 구축 (`agent_index/`, 커밋 제외). `--repo`로 일부만, `--rebuild`로 다시 판정 |
| `python -m agent.main --diff changes.patch --dry-run` | 로컬 diff 리뷰 (코멘트를 게시하지 않고 출력) |
| `python -m agent.main --pr owner/repo#12 --dry-run` | 실제 PR 리뷰 |
| `python scripts/run_agent_checks.py` | 검증 PR 세트(`tests/fixtures/agent_diffs/cases.yaml`, 9개) dry-run과 판정 |

GitHub Action으로 쓰려면 [워크플로 예시](docs/issuelens_review_workflow.yml)를 대상 레포의 `.github/workflows/`에 복사하고 secrets에 `OPENAI_API_KEY`를 넣습니다. `pull_request` 이벤트만 쓰고(포크 PR은 건너뜀), 권한은 `contents: read`, `pull-requests: write`뿐입니다. 라이브러리 코퍼스는 Action 캐시에 두고 필요한 라이브러리만 실행당 최대 3개씩 구축합니다. 리뷰 설정은 대상 레포의 `.github/issuelens.yml`로 바꿀 수 있습니다(`exclude_paths`, `max_general_findings`, `general_review` 등).

## 데이터 출처와 이용 조건

| 저장소 | 이슈 출처 | 저장소 라이선스 (SPDX) |
|---|---|---|
| `datahub-project/datahub` | https://github.com/datahub-project/datahub/issues | Apache-2.0 |
| `open-metadata/OpenMetadata` | https://github.com/open-metadata/OpenMetadata/issues | Apache-2.0 |
| `langgenius/dify` | https://github.com/langgenius/dify/issues | NOASSERTION ([LICENSE](https://github.com/langgenius/dify/blob/main/LICENSE)) |
| `thingsboard/thingsboard` | https://github.com/thingsboard/thingsboard/issues | NOASSERTION ([LICENSE](https://github.com/thingsboard/thingsboard/blob/master/LICENSE)) |
| `conductor-oss/conductor` | https://github.com/conductor-oss/conductor/issues | Apache-2.0 |
| `airbytehq/airbyte` | https://github.com/airbytehq/airbyte/issues | NOASSERTION ([LICENSE](https://github.com/airbytehq/airbyte/blob/master/LICENSE)) |
| `getsentry/sentry` | https://github.com/getsentry/sentry/issues | NOASSERTION ([LICENSE](https://github.com/getsentry/sentry/blob/master/LICENSE)) |
| `keycloak/keycloak` | https://github.com/keycloak/keycloak/issues | Apache-2.0 |

- 데이터는 GitHub REST API로 수집했습니다 (`is:issue is:closed`, 2026-10-21). 저장소별 수집 시각과 라이선스는 `data/manifests/source_manifest.json`에 있습니다.
- **NOASSERTION**: GitHub가 라이선스를 표준 SPDX 식별자로 판별하지 못한 경우입니다 (Dify, ThingsBoard, Airbyte, Sentry). 이 저장소들은 자체 조건이 붙은 라이선스를 쓰므로 위 LICENSE 원문을 확인해야 합니다.
- 저장소의 코드 라이선스가 이슈와 댓글의 사용자 작성 내용에 자동으로 적용된다고 가정하지 않습니다. 이슈와 댓글은 공개 URL, 작성자 계정명, 수집일을 기록하고 교육 목적에 필요한 범위에서만 사용합니다.
- 이슈 원문은 별도 데이터셋으로 재배포하지 않습니다. 이 저장소에는 수집기, 설정, 원문 없는 manifest(선별 결과, 건수, 라이선스)만 포함합니다.
- 답변의 근거에는 항상 원문 URL을 표시합니다.
- 수집 시 사용자 이메일, 아바타 URL, 인증 정보는 저장하지 않고, 본문의 키·토큰·이메일·IP 후보는 마스킹합니다.
- GitHub API는 인증 요청, 순차 호출, 요청 간격(일반 0.5초, 검색 2초), `Retry-After`·`X-RateLimit-Reset` 준수, 지수 백오프, ETag 조건부 요청으로 호출합니다.

## 문서

- [기획서](docs/IssueLens_기획서.md): 챗봇과 에이전트 전체 설계
- [범위 확정](docs/scope.md)
- [증빙 체크리스트](docs/evidence_checklist.md): 요구사항 정의서 4장 항목별 확인 방법
