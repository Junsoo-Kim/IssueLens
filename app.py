"""IssueLens Streamlit 진입점.

    streamlit run app.py

ISSUELENS_ROOT를 주면 그 폴더의 configs와 색인을 쓴다 (화면 캡처 스크립트가 색인 없음 화면을 띄울 때 사용).
"""
import os

from src.ui.main import run

run(os.environ.get("ISSUELENS_ROOT") or None)
