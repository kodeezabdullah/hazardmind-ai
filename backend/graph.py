"""LangGraph pipeline: satellite -> hazard -> impact -> report.

Each agent directory (agents/<name>/) is a self-contained module tree that
still runs standalone as its own process/venv (agent.py, intelligence.py,
node.py, hf_app.py exist under multiple agent directories with the SAME bare
module name). Importing all four normally in one backend process would clobber
sys.modules["agent"]/["intelligence"]/etc. between agents. _load_node isolates
each agent's import under a unique sys.modules key and only prepends that
agent's own directory to sys.path for the duration of its own import, so each
agent's unqualified sibling imports (`from agent import ...`,
`from intelligence import ...`) resolve within its own directory and never leak
into another agent's identically-named module.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    # Needed for `shared.pipeline_state` (this file's own import below, plus
    # every node.py's identical import) to resolve — shared/ is a repo-root
    # sibling of backend/ and agents/, not on sys.path by default.
    sys.path.insert(0, str(REPO_ROOT))

from langgraph.graph import END, StateGraph

from shared.pipeline_state import PipelineState

AGENTS_DIR = REPO_ROOT / "agents"


def _load_node(agent_name: str, func_name: str):
    """Import agents/<agent_name>/node.py in isolation and return func_name.

    Cached in sys.modules under a collision-proof key so repeated calls (e.g.
    build_pipeline_graph() called more than once) don't re-import.

    node.py (and the modules it imports) reach their siblings with UNQUALIFIED
    names (`from agent import ...`, `from pipeline import ...`,
    `from intelligence import ...`). Python caches those under their bare name in
    sys.modules, so without cleanup the first agent's `agent`/`intelligence`/...
    would satisfy the NEXT agent's identically-named import and load the wrong
    code (e.g. hazard/node.py's `from agent import analyze_hazard` resolving to
    satellite/agent.py). We therefore snapshot sys.modules before the load and
    drop every bare-named module the load introduced once the node module itself
    is safely cached under its unique key, leaving the next agent a clean slate.
    """
    key = f"hazardmind_{agent_name}_node"
    if key not in sys.modules:
        agent_dir = AGENTS_DIR / agent_name
        before = set(sys.modules)
        sys.path.insert(0, str(agent_dir))
        try:
            spec = importlib.util.spec_from_file_location(key, agent_dir / "node.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
        finally:
            sys.path.remove(str(agent_dir))
            # Purge unqualified sibling modules this agent introduced (anything
            # not already present and not our own uniquely-keyed node module or
            # a package on sys.path). Keeps each agent's `agent`/`intelligence`/
            # `pipeline`/... from leaking into the next agent's bare imports.
            for name in set(sys.modules) - before:
                if name == key or name.startswith("hazardmind_"):
                    continue
                if name.startswith("shared") or name.startswith("langgraph"):
                    continue
                mod = sys.modules.get(name)
                mod_file = getattr(mod, "__file__", None) or ""
                if str(agent_dir) in mod_file:
                    del sys.modules[name]
    return getattr(sys.modules[key], func_name)


def _route_after(step: str, next_step: str):
    """Advance to next_step unless this node marked the pipeline failed.

    Each node returns status: "failed" (rather than raising) on a stage
    failure, per its own module docstring. Without this gate the graph would
    plow ahead into the next stage on bad/missing upstream data.
    """

    def _router(state: PipelineState) -> str:
        if state.get("status") == "failed":
            return END
        return next_step

    return _router


def build_pipeline_graph():
    """Compile the satellite -> hazard -> impact -> report StateGraph."""
    satellite_node = _load_node("satellite", "satellite_node")
    hazard_node = _load_node("hazard", "hazard_node")
    impact_node = _load_node("impact", "impact_node")
    report_node = _load_node("report", "report_node")

    graph = StateGraph(PipelineState)
    graph.add_node("satellite", satellite_node)
    graph.add_node("hazard", hazard_node)
    graph.add_node("impact", impact_node)
    graph.add_node("report", report_node)

    graph.set_entry_point("satellite")
    graph.add_conditional_edges("satellite", _route_after("satellite", "hazard"))
    graph.add_conditional_edges("hazard", _route_after("hazard", "impact"))
    graph.add_conditional_edges("impact", _route_after("impact", "report"))
    graph.add_edge("report", END)

    return graph.compile()
