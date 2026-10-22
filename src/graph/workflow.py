"""retrieve -> generate 그래프 구성과 구조 출력 (요구사항 F2007)."""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.graph.nodes import RAGPipeline
from src.graph.state import RAGState


def build_graph(pipeline: RAGPipeline):
    graph = StateGraph(RAGState)
    graph.add_node("retrieve", pipeline.retrieve)
    graph.add_node("generate", pipeline.generate)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
    return graph.compile()


def graph_diagram(compiled) -> str:
    """요구사항 정의서 4.3절 증빙용 그래프 구조 (Mermaid)."""
    return compiled.get_graph().draw_mermaid()
