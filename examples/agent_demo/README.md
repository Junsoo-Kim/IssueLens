# 에이전트 시연용 예제

PR 선제 리뷰 에이전트를 이 레포에서 직접 시연하기 위한 작은 예제입니다. 이 폴더를 바꾸는 PR을 열면
`.github/workflows/issuelens-demo.yml`이 IssueLens 리뷰를 실행해 PR에 코멘트를 남깁니다.

- 레포 Settings → Secrets and variables → Actions에 `OPENAI_API_KEY`(필수), `OPENAI_BASE_URL`(선택)을 넣어야 합니다
- 예: `openai`를 1.x로 올리면서 `openai.ChatCompletion` 호출을 그대로 두는 PR
