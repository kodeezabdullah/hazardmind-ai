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

import functools
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


# Per-agent stash of the unqualified sibling modules each agent introduced at
# load time (keyed by agent_name -> {bare_name: module}). Used to re-install the
# right agent's bare modules around every node call — see _load_node.
_AGENT_BARE_MODULES: dict[str, dict] = {}


def _load_node(agent_name: str, func_name: str):
    """Import agents/<agent_name>/node.py in isolation and return a callable.

    node.py (and the modules it imports) reach their siblings with UNQUALIFIED
    names (`from agent import ...`, `from sentinel import ...`, ...). Python
    caches those under their bare name in sys.modules, so if two agents are both
    resident the first agent's `agent`/`sentinel`/... would satisfy the NEXT
    agent's identically-named import and load the wrong code.

    We can't simply delete the bare modules after load, because the pipeline code
    imports some siblings LAZILY at call time (e.g.
    agents/satellite/processor.py does `from sentinel import ...` inside a
    function). By the time the node runs, a deleted `sentinel` + a popped
    sys.path entry make that lazy import raise `ModuleNotFoundError`.

    Instead we: (1) load node.py with the agent dir on sys.path, (2) stash the
    bare-named modules this agent introduced under a per-agent dict and remove
    them from the shared sys.modules (so they don't collide with the next
    agent's load), then (3) return a WRAPPER that, on every call, temporarily
    re-installs this agent's bare modules into sys.modules and prepends its dir
    to sys.path — so both eager and lazy sibling imports resolve to the correct
    agent — and restores the previous state afterwards.
    """
    key = f"hazardmind_{agent_name}_node"
    agent_dir = AGENTS_DIR / agent_name

    if key not in sys.modules:
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
            # Move the unqualified sibling modules this agent introduced out of
            # the shared table into this agent's stash (so the NEXT agent's load
            # sees a clean slate), but KEEP them — they'll be re-installed around
            # each call so lazy imports resolve.
            stash: dict = {}
            for name in set(sys.modules) - before:
                if name == key or name.startswith("hazardmind_"):
                    continue
                if name.startswith("shared") or name.startswith("langgraph"):
                    continue
                mod = sys.modules.get(name)
                mod_file = getattr(mod, "__file__", None) or ""
                if str(agent_dir) in mod_file:
                    stash[name] = sys.modules.pop(name)
            _AGENT_BARE_MODULES[agent_name] = stash

    node_fn = getattr(sys.modules[key], func_name)
    stash = _AGENT_BARE_MODULES.get(agent_name, {})
    dir_str = str(agent_dir)

    @functools.wraps(node_fn)
    async def _wrapped(state):
        # Install this agent's bare sibling modules + dir for the duration of the
        # call, so both eager and LAZY (`from sentinel import ...` inside a
        # function) imports resolve to THIS agent's code. Save/restore whatever
        # was there so concurrent/other agents aren't disturbed.
        saved = {name: sys.modules.get(name) for name in stash}
        for name, mod in stash.items():
            sys.modules[name] = mod
        added_path = dir_str not in sys.path
        if added_path:
            sys.path.insert(0, dir_str)
        try:
            return await node_fn(state)
        finally:
            if added_path:
                try:
                    sys.path.remove(dir_str)
                except ValueError:
                    pass
            # Re-stash any modules the call newly imported lazily, then restore.
            for name in list(stash) + [
                n for n in sys.modules
                if n not in saved
                and not n.startswith(("hazardmind_", "shared", "langgraph"))
                and str(agent_dir) in (getattr(sys.modules.get(n), "__file__", None) or "")
            ]:
                mod = sys.modules.get(name)
                if mod is not None and str(agent_dir) in (getattr(mod, "__file__", None) or ""):
                    stash[name] = mod
                prev = saved.get(name)
                if prev is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = prev

    return _wrapped


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
