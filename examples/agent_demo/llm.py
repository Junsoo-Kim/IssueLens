"""PR 리뷰 에이전트 시연용 예제 앱 (실행하지 않는다). 이 폴더를 바꾸는 PR에서 IssueLens 리뷰가 돈다."""
import openai


def summarize(text: str) -> str:
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"Summarize: {text}"}],
    )
    return response["choices"][0]["message"]["content"]
