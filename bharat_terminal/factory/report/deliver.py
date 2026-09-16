"""Delivery — inert by default.

The report must survive two failure domains (spec §12): the Power Automate flow
that sends the mail, and the host that runs the workflow. Neither is wired up
while AWS is on hold, so this module *builds* the delivery payload and the
dead-man metric, and refuses to transmit unless handed an explicit transport.

That is not a placeholder. Sending mail is an outward-facing action, and the
CloudWatch alarm needs IAM that does not exist yet; both are founder decisions.
What can be built and tested now is everything up to the wire.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from bharat_terminal.factory.report.facts import ReportFacts
from bharat_terminal.factory.report.render import render_text, subject

# Power Automate FLOW 1 accepts {to, subject, html, text} and responds AFTER the
# Outlook send step, so HTTP 200 + {"ok": true} means accepted for delivery.
# See POWER_AUTOMATE_SPEC.md §3.
FLOW_ENV_VAR = "EMAIL_WORKFLOW_URL"

METRIC_NAMESPACE = "Factory"
METRIC_NAME = "ReportDelivered"

MAX_ATTEMPTS = 3


@dataclass
class DeliveryPayload:
    to: str
    subject: str
    text: str
    html: Optional[str] = None

    def as_flow_body(self) -> Dict[str, Any]:
        return {"to": self.to, "subject": self.subject,
                "html": self.html or f"<pre>{self.text}</pre>", "text": self.text}


@dataclass
class DeliveryResult:
    sent: bool
    attempts: int = 0
    status_code: Optional[int] = None
    error: Optional[str] = None
    metric_pushed: bool = False
    inert_reason: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"sent": self.sent, "attempts": self.attempts,
                "status_code": self.status_code, "error": self.error,
                "metric_pushed": self.metric_pushed,
                "inert_reason": self.inert_reason}


def build_payload(facts: ReportFacts, to: str,
                  narrative: Optional[str] = None) -> DeliveryPayload:
    return DeliveryPayload(to=to, subject=subject(facts),
                           text=render_text(facts, narrative))


def deliver(
    payload: DeliveryPayload,
    transport: Optional[Callable[[Dict[str, Any]], Any]] = None,
    metric_sink: Optional[Callable[[str, str, float], Any]] = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> DeliveryResult:
    """Send the report, if and only if a transport was supplied.

    `transport` receives the flow body and returns an object with
    `.status_code` and `.json()`, i.e. an httpx/requests response. Retries are
    bounded; a 2xx without {"ok": true} is NOT treated as delivered, because the
    flow answers after its send step and anything else means it did not run.
    """
    if transport is None:
        return DeliveryResult(sent=False, inert_reason="no transport configured")

    last_error: Optional[str] = None
    status: Optional[int] = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = transport(payload.as_flow_body())
            status = getattr(resp, "status_code", None)
            body: Dict[str, Any] = {}
            try:
                body = resp.json() or {}
            except Exception:
                body = {}
            if status == 200 and body.get("ok") is True:
                pushed = False
                if metric_sink is not None:
                    try:
                        metric_sink(METRIC_NAMESPACE, METRIC_NAME, 1.0)
                        pushed = True
                    except Exception as e:      # metric failure != delivery failure
                        last_error = f"metric push failed: {e}"
                return DeliveryResult(sent=True, attempts=attempt, status_code=status,
                                      error=last_error, metric_pushed=pushed)
            last_error = f"unexpected response: status={status} body={body}"
        except Exception as e:
            last_error = str(e)
    return DeliveryResult(sent=False, attempts=max_attempts, status_code=status,
                          error=last_error)


def dead_man_alarm_spec(period_hours: int = 24) -> Dict[str, Any]:
    """The CloudWatch alarm that fires when no report arrives.

    Returned as a spec rather than created, because it needs IAM we do not have.
    `TreatMissingData=breaching` is the whole point: silence is the alarm
    condition, so a frozen host cannot suppress the report unnoticed.
    """
    return {
        "AlarmName": "factory-report-not-delivered",
        "Namespace": METRIC_NAMESPACE,
        "MetricName": METRIC_NAME,
        "Statistic": "Sum",
        "Period": period_hours * 3600,
        "EvaluationPeriods": 1,
        "Threshold": 1.0,
        "ComparisonOperator": "LessThanThreshold",
        "TreatMissingData": "breaching",
        "AlarmDescription": (
            "No daily research report was delivered in the last "
            f"{period_hours}h. Missing data is breaching by design."),
    }
