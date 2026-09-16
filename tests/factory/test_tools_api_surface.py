"""The tool surface must not drift between its three statements.

The surface is declared in three independent places on purpose:

  1. `tools_api/app.py`        - what the HTTP service will serve
  2. `infra/agentcore/lambda_tools_proxy.py` - what the Gateway proxy forwards
  3. the design doc / spec 4.2 Cedar matrix   - what policy permits

Duplication is the point. If the Lambda simply forwarded whatever the app
exposed, adding a route would silently publish a new tool through the Gateway
with no Cedar rule covering it. These tests turn that drift into a failed
build instead of an open door.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
LAMBDA_SRC = ROOT / "infra" / "agentcore" / "lambda_tools_proxy.py"

# Read tools + write tools, from the modules themselves rather than a literal.
EXPECTED_READS = 16
EXPECTED_WRITES = 5


def _lambda_allowlist() -> set:
    """Parse ALLOWED out of the Lambda without importing it (no boto3 here)."""
    tree = ast.parse(LAMBDA_SRC.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "ALLOWED":
            return set(ast.literal_eval(node.value))
    raise AssertionError("ALLOWED not found in the lambda proxy")


def _module_tools() -> set:
    from bharat_terminal.factory.tools_api import reads, writes

    r = {n for n in dir(reads) if n.startswith(("get_", "search_", "list_"))
         and not n.startswith("get_feature")}
    w = {n for n in dir(writes) if n.startswith(("submit_", "report_"))}
    return r | w


def test_the_lambda_allowlist_matches_the_implemented_tools():
    """A tool the Lambda forwards must exist; a tool that exists must be listed."""
    allow = _lambda_allowlist()
    impl = _module_tools()
    assert not (allow - impl), f"lambda forwards tools that do not exist: {sorted(allow - impl)}"
    assert not (impl - allow), f"implemented tools missing from the lambda allowlist: {sorted(impl - allow)}"


def test_the_surface_is_twenty_one_tools():
    from bharat_terminal.factory.tools_api import reads, writes

    r = {n for n in dir(reads) if n.startswith(("get_", "search_", "list_"))}
    w = {n for n in dir(writes) if n.startswith(("submit_", "report_"))}
    assert len(r) == EXPECTED_READS, sorted(r)
    assert len(w) == EXPECTED_WRITES, sorted(w)
    assert len(_lambda_allowlist()) == EXPECTED_READS + EXPECTED_WRITES


def test_no_promotion_tool_is_reachable_through_the_gateway():
    """Absence is the enforcement. Assert it at the proxy boundary too.

    Matched on the leading VERB, not on substrings. An earlier version of this
    test grepped for "gate" and flagged `get_gate_decision` -- a read tool --
    which is the same over-matching bug that let a memo saying "we ran 15
    tests" cite runs 1 and 5. A noun in a name says nothing about what the
    tool does; the verb does.
    """
    allow = _lambda_allowlist()
    mutating = ("promote", "approve", "unseal", "delete", "drop", "set_",
                "update_", "place_", "enrol", "enroll", "kill_", "lift_")
    offenders = [t for t in allow if t.startswith(mutating)]
    assert not offenders, f"mutating tools exposed through the gateway: {offenders}"

    # The write surface is exactly five, and every one is a submit/report.
    writes_exposed = {t for t in allow if not t.startswith(("get_", "search_", "list_"))}
    assert writes_exposed == {
        "submit_hypotheses", "submit_attack_plan", "submit_allocation",
        "submit_report_narrative", "report_data_gap",
    }, sorted(writes_exposed)


def test_the_lambda_reads_the_tool_name_from_client_context_not_the_body():
    """The body is agent-controlled; the client context is not.

    Routing on the body would let an agent name its own tool and step around
    whatever Cedar had just decided.
    """
    src = LAMBDA_SRC.read_text()
    assert "bedrockAgentCoreToolName" in src
    assert "client_context" in src
    # the handler must not pull a tool name out of the event payload
    assert 'event.get("tool' not in src and "event['tool" not in src


def test_the_lambda_never_forwards_an_unknown_tool():
    src = LAMBDA_SRC.read_text()
    assert "not in ALLOWED" in src
    assert "403" in src


def test_every_forwarded_tool_is_callable():
    """Each allow-listed name resolves to a real coroutine or function.

    Iterated inside the test, not via @parametrize: a module-level call would
    break COLLECTION if the infra/ mount were missing, which hides every other
    test in the file behind one unrelated error.
    """
    from bharat_terminal.factory.tools_api import reads, writes

    missing = [t for t in sorted(_lambda_allowlist())
               if not callable(getattr(reads, t, None) or getattr(writes, t, None))]
    assert not missing, f"allow-listed but not implemented: {missing}"
