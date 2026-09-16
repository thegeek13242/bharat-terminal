"""AgentCore Gateway -> research-tools proxy. Runs as a Lambda inside the VPC.

    agent -> Gateway (Cedar evaluated there) -> THIS -> research-tools on the
             EC2 private IP -> Postgres as factory_api

WHY A PROXY EXISTS AT ALL
Agents must never hold a database credential or the tools-API bearer token.
This Lambda holds the token (from Secrets Manager, cached per container) and
is the only thing with a network path to the tools service: the EC2 security
group allows the tools port from this Lambda's security group and from nothing
else. Compromising an agent therefore yields no credential and no route.

TOOL NAME ARRIVES OUT OF BAND
The Gateway passes it in `context.client_context.custom["bedrockAgentCoreToolName"]`
formatted `Target___tool` -- NOT in the event body. That matters: the body is
agent-controlled, the client context is not, so routing on the body would let
an agent choose its own tool name and bypass whatever Cedar just decided.

THIS FUNCTION DOES NO ROUTING OF ITS OWN
It splits on `___`, looks the suffix up in a closed allowlist, and refuses
anything absent from it. A tool name it does not recognise is a 403 that never
reaches the tools service. The allowlist is duplicated here deliberately: a
second, independent statement of the tool surface means adding a route to the
FastAPI app does not silently expose it through the Gateway.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict

TOOLS_BASE = os.environ.get("TOOLS_BASE_URL", "http://172.31.42.154:8003")
SECRET_ID = os.environ.get("TOOLS_TOKEN_SECRET", "factory/tools-api-token")
TIMEOUT_S = float(os.environ.get("TOOLS_TIMEOUT_S", "30"))

# Independent restatement of the surface. Keep in step with tools_api/app.py
# on purpose -- CI asserts the two agree, so a drift is a failed build rather
# than an accidentally exposed tool.
ALLOWED = {
    "search_memory", "get_failure", "get_hypothesis", "get_research_question",
    "get_family_state", "get_dataset_catalog", "get_run_results",
    "get_gate_decision", "get_holdout_status", "get_divergence_report",
    "get_frontier", "get_budget", "get_report_facts", "get_paper_feedback",
    "list_features", "get_battery_catalog",
    "submit_hypotheses", "submit_attack_plan", "submit_allocation",
    "submit_report_narrative", "report_data_gap",
}

_token: str | None = None


def _bearer() -> str:
    """Fetch once per container. boto3 is only imported here so the module is
    importable (and testable) without an AWS session."""
    global _token
    if _token is None:
        import boto3

        sm = boto3.client("secretsmanager")
        _token = sm.get_secret_value(SecretId=SECRET_ID)["SecretString"]
    return _token


def _tool_name(context: Any) -> str | None:
    """Read the tool name from the Gateway's client context, never the body."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    raw = custom.get("bedrockAgentCoreToolName")
    if not raw:
        return None
    return raw.split("___")[-1]


def _reply(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    tool = _tool_name(context)
    if tool is None:
        return _reply(400, {"error": "no bedrockAgentCoreToolName in client context"})
    if tool not in ALLOWED:
        # Never forwarded. An unknown name dies here, not at the tools service.
        return _reply(403, {"error": f"tool not allowed: {tool}"})

    payload = json.dumps(event if isinstance(event, dict) else {}).encode()
    req = urllib.request.Request(
        f"{TOOLS_BASE}/tools/{tool}",
        data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_bearer()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return _reply(resp.status, json.loads(resp.read() or b"{}"))
    except urllib.error.HTTPError as e:
        # Pass the status through so a 422 stays a 422 the agent can repair.
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:  # noqa: BLE001
            body = {"error": e.reason}
        return _reply(e.code, body)
    except urllib.error.URLError as e:
        # The reachability failure mode worth naming explicitly: if the Gateway
        # can invoke this Lambda but this Lambda cannot reach the private IP,
        # the security-group rule or the subnet is wrong -- not the agent.
        return _reply(504, {"error": f"tools service unreachable: {e.reason}",
                            "hint": "check sg-factory-lambda -> EC2 SG rule on the tools port"})
