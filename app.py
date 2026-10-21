"""IssueLens Streamlit 진입점.

    streamlit run app.py
"""
import streamlit as st

st.set_page_config(page_title="IssueLens", layout="wide")
st.title("IssueLens")
st.caption("운영 이슈 이력 기반 장애 진단 및 해결 지원 RAG 챗봇")
st.info("색인이 아직 없습니다. 수집과 색인 스크립트를 먼저 실행해 주세요.")
