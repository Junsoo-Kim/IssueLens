# 범위 확정 (기획서 5.4절 2단계)

작성일: 2026-10-21 · 근거 데이터: `data/manifests/final_selection.jsonl`, `data/manifests/scope_report.json`
재현: `python scripts/analyze_scope.py`

## 결정

**범위: 기업 운영 플랫폼과 유사한 오픈소스 서비스의 장애 및 운영 이슈**

- 대상 저장소: 8개 전체 유지 (DataHub, OpenMetadata, Dify, ThingsBoard, Conductor, Airbyte, Sentry, Keycloak)
- 평가 대상 공통 상위 범주: 5개

  | 범주 | 설명 |
  |---|---|
  | `async_event` | 비동기·이벤트 처리 (동기화, 워크플로, 큐, 스케줄러, 커넥터) |
  | `api_ui` | API 및 UI 연동 |
  | `deployment_upgrade` | 배포 및 업그레이드 |
  | `database_search` | 데이터베이스 및 검색 |
  | `auth` | 인증 및 권한 |

- `performance_reliability`(성능·신뢰성)는 **대표 범주에서 제외하고 부가 태그로만 유지**한다. 대표 범주가 성능·신뢰성이면 다음 순위 범주를, 없으면 저장소 기본 범주를 쓴다.
- 이슈 유형: 장애·결함(`bug`) 중심. 버그만으로 후보가 부족한 ThingsBoard, Conductor에 한해 유지보수자가 답변한 질문(`question`)을 설정·사용법 유형으로 포함한다.
- 제외: 테스트 불안정(flaky test), 작업·CVE·의존성 업데이트, 기능 요청, 무효(`invalid`, `not us`, `wrong-repo`), 중복, 봇 자동 종료, 해결 근거 없음(`none`).

## 근거

얕은 수집 7,319건 → 상세 수집 1,527건 → 최종 색인 580건 / 평가 160건 기준.

| 범주 | 색인 | 평가 | 판단 |
|---|---:|---:|---|
| async_event | 182 | 53 | 유지 |
| api_ui | 134 | 36 | 유지 |
| deployment_upgrade | 103 | 17 | 유지 |
| database_search | 81 | 26 | 유지 |
| auth | 80 | 28 | 유지 (Keycloak 59건 편중) |
| performance_reliability | 대표 범주 19 / 평가 1 | | 부가 태그로 전환: 대표 범주로는 평가 표본 부족 |

부가 태그로는 성능·신뢰성 신호가 색인 45건, 평가 13건에 붙는다. 대표 범주로 세울 만큼은 아니지만 검색 필터와 답변의 환경 비교에는 쓸 수 있다. 수치는 `scope_report.json`(태깅 결과로 대표 범주를 다시 계산한 값)과 같다.

### 확정 기준 충족 여부

| 기준 (기획서 5.4절) | 기준값 | 실제 | 결과 |
|---|---|---|---|
| 범주마다 평가 가능한 표본 | 범주당 평가 10건 이상 | 최소 17건 (deployment_upgrade) | 충족 |
| 한 범주가 색인을 독점하지 않음 | 40% 이하 | 31.4% (async_event) | 충족 |
| 해결 근거가 있는 비율 | strong+medium 80% 이상 | 88.8% | 충족 |
| 저장소별 색인 75건 | 저장소당 75건 | OpenMetadata 57, Conductor 73 | 일부 미달 (MVP로 수용) |

### 범주 대신 컴포넌트를 범위 축으로 쓰지 않은 이유

색인 이슈의 57%가 컴포넌트 `general`이다. DataHub, Dify, ThingsBoard, Conductor는 라벨에 컴포넌트를 거의 쓰지 않아, 컴포넌트로 범위를 나누면 대부분 미분류가 된다. 저장소 내부 정밀 검색에는 컴포넌트 태그를 그대로 쓰고, 저장소 간 유사 사례 검색과 평가는 공통 상위 범주로 한다.

### 검토했지만 택하지 않은 안

- **데이터 플랫폼 장애로 축소** (Airbyte, DataHub, OpenMetadata, Conductor): 근거 밀도는 높아지지만 8개 저장소 전제와 인증(Keycloak), 관측(Sentry) 사례를 잃는다.

## 알려진 한계

- `auth` 색인 80건 중 59건이 Keycloak이다. 저장소 간 확장 검색(기획서 9.7절)에서 인증 문제는 Keycloak을 우선한다는 설계와 일치하지만, 다른 저장소의 인증 이슈 평가 표본은 적다.
- 범주 분류는 키워드 규칙 기반이다. 평가셋을 구성할 때 대표 범주 오분류를 표본 검수한다.
- 순서상 이 결정은 상세 수집 전에 했어야 했다. 상세 수집 대상 선별은 컴포넌트 기준이었고 범주를 쓰지 않았으므로, 사후 확정이 선별 결과를 바꾸지는 않는다.
