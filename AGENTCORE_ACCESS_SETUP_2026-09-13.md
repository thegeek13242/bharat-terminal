# AgentCore Access & Setup — what to grant, in what order

**Date:** 2026-09-13 · **Status:** ⚠ **PARTLY SUPERSEDED — read the correction block below first.**
Four IAM action names in this document do not exist, and the work is no longer on hold: Steps 1-3
and 6 were executed on 2026-09-13 and verified working.
**Audience:** founder (account owner). Everything here is an action *you* take, or approve me to
take, in account `480749289185`, region **ap-south-1** unless stated.

Why this doc: the factory's deterministic half runs today with zero AWS permissions. The moment we
want LLM agents, we need six things that only an account owner can grant. This lists them in
dependency order, with the exact IAM actions, what each unlocks, what it costs, and how to verify
it worked before moving on.

---

> ## ⚠ CORRECTION 2026-09-13 — four IAM actions below do not exist
>
> **What happened.** I wrote the action names in this document from documentation rather than from
> the live API. Checked against `aws bedrock-agentcore-control help` and `aws bedrock-agentcore
> help` in this account, four of them do not exist. A role built from this file would have carried
> two phantom permissions and been missing the one the tool path actually needs.
>
> | This document says | The live API |
> |---|---|
> | `bedrock-agentcore:InvokeGateway` (Step 3) | **No such action, in either plane.** Gateways are called over MCP with a workload-identity token: `GetWorkloadAccessToken`, `GetWorkloadAccessTokenForJWT`, `GetResourceOauth2Token` |
> | `CreatePolicy \| UpdatePolicy \| ListPolicies` (Step 5) | Incomplete — a policy attaches to an **engine**, so `CreatePolicyEngine` must exist first |
> | `StartEvaluation`, `ListEvaluations` (Step 6) | **Neither exists.** Real: `CreateEvaluator`, `CreateOnlineEvaluationConfig`, `ListEvaluators`, data-plane `Evaluate` |
> | *(never mentioned)* | `CreateWorkloadIdentity` is required for the gateway auth path |
>
> **Three more errors found by running it:**
> - **Step 4's `sg-factory-lambda` is an illegal security-group *name*** — AWS reserves the `sg-`
>   prefix for group IDs (`InvalidParameterValue`). Renamed `bharat-factory-lambda`.
> - **Step 1's verification cannot be run as written.** This account's CloudShell is the **root
>   account**, and root may never assume a role. Verification runs on the EC2 box instead — which
>   exercises the production path (`bharat-terminal-ec2` -> orchestrator), so it is the better test.
> - **§0's "Opus 4.6 = 5 RPM / 2.59M TPD" is wrong.** There is no tokens-per-day quota for Opus 4.6
>   in this account at all — only 5 RPM and 3M tokens-per-*minute*. A TPD-based budget guard would
>   guard nothing. (Opus 4.5 does have a real TPD: 13.5M.)
>
> **Superseded by `AGENTCORE_CLOUDSHELL_RUNBOOK_2026-09-13.md`** — corrected, copy-pasteable, every
> identifier pre-filled. Use that for the commands; this file remains the *why*.
>
> ### What is actually built, and what I could not check
>
> **Verified by behaviour on the EC2 box, 12/12 passing** — the orchestrator role assumes via the
> EC2 path and carries working model, AgentCore-control, gateway, policy-engine and observability
> permissions. The two tests that matter both pass: **Sonnet 4.6 and Haiku 4.5 are DENIED** to the
> factory, and **`PutMetricData` outside the `Factory/` namespace is DENIED**. Production capacity
> is unreachable by IAM, not by convention.
>
> **Trust policy CONFIRMED CLEAN, 2026-09-13.** `aws iam get-role --role-name
> bharat-factory-orchestrator --query 'Role.AssumeRolePolicyDocument.Statement[].Principal'` returns
> exactly one principal — `arn:aws:iam::480749289185:role/bharat-terminal-ec2` — with no `:root`
> entry. This was the last gap in the boundary, and it mattered: every behavioural test above passes
> either way, because the EC2 role is named explicitly, so only a direct read could settle it. The
> factory/production separation is now an IAM property end to end.
>
> **Still NOT verified — this box has no IAM or EC2 read permission:** the four agent execution
> roles, the security-group ingress rule, and the SNS topic and subscription. None of these sits on
> the production-boundary path; they gate Steps 3, 4 and 7 respectively, and each fails loudly rather
> than silently when missing.
>
> ### Two unknowns from §"What I still cannot verify" have been tested
>
> - **Real Bedrock per-token price — still NOT settled, and not settleable from here.**
>   `ce:GetCostAndUsage` is attached and returns successfully, but reports $0 for July,
>   $0.00000004 for August and $0.03 for September — on an account running a t3.xlarge
>   continuously and calling Bedrock daily. That is not low spend; it is **no billing data in this
>   account**, most likely consolidated at a payer account (`organizations:DescribeOrganization` is
>   denied, so the mechanism is unconfirmed). A working permission returning empty data is a worse
>   failure than a clean denial, because "$0.03" reads like a fact.
> - **Prompt caching on Opus 4.5/4.6 — measured, and it does NOT engage.**
>   `cache_read_input_tokens: 0` on a repeated 2,656-token cached prefix, on both passes. Both Opus
>   models also reject on-demand invocation outright, so the global inference profile is the only
>   available route and the cause cannot be isolated here. This matches the `cached_tokens=0`
>   already seen on production hot paths. **Budget the $1,000 model line with no caching discount.**
>
> Everything below is left exactly as written, so the errors and their cause stay legible.

---

## 0. Current state (verified 2026-09-12)

| Probe | Result |
|---|---|
| `aws sts get-caller-identity` | `assumed-role/bharat-terminal-ec2/i-02d5bf4dfc63b0e87` |
| `aws bedrock-agentcore-control list-agent-runtimes --region ap-south-1` | **AccessDenied** — `bedrock-agentcore:ListAgentRuntimes` not allowed |
| `aws pricing get-products` | AccessDenied |
| `aws ce get-cost-and-usage` | AccessDenied |
| `aws service-quotas list-service-quotas --service-code bedrock` | **works** (this is how the Opus pools were confirmed) |
| Bedrock model access | Opus 4.6 = 5 RPM / 2.59M TPD, Opus 4.5 = 5 RPM / 13.5M TPD, both unused by production |

So: the instance role can read quotas and call Bedrock models, and nothing else. Every step below
is additive; none of it changes an existing permission.

---

## Step 1 — Decide the identity the factory runs as

**Recommendation: a new role, not the existing instance role.**

`bharat-terminal-ec2` is attached to the box that runs production. Adding AgentCore and Bedrock
permissions to it widens what a bug in *any* production container can reach. Instead:

- Create `bharat-factory-orchestrator`, assumable by the EC2 instance role
  (`sts:AssumeRole` trust on `bharat-terminal-ec2`).
- The orchestrator process assumes it explicitly; nothing else on the box can.

This costs nothing and is the single highest-value boundary in the whole setup: it is what makes
"a compromised agent cannot reach production" a property of IAM rather than of my carefulness.

**You do:** create the role with the trust policy below, no permissions yet.

```json
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"AWS":"arn:aws:iam::480749289185:role/bharat-terminal-ec2"},
 "Action":"sts:AssumeRole"}]}
```

**Verify:** `aws sts assume-role --role-arn arn:aws:iam::480749289185:role/bharat-factory-orchestrator --role-session-name t` returns credentials.

---

## Step 2 — Model access, scoped to the two idle pools

This is the permission that decides whether the factory can ever consume production's capacity.
**Scope it to the Opus inference-profile ARNs only.** Sonnet 4.6, Sonnet 4.5 and Haiku 4.5 must be
absent from the resource list, so the factory *cannot* touch them even if misconfigured.

Attach to `bharat-factory-orchestrator` and to each agent runtime role:

```json
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
 "Resource":[
  "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-6*",
  "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-5*",
  "arn:aws:bedrock:*:480749289185:inference-profile/global.anthropic.claude-opus-4-6*",
  "arn:aws:bedrock:*:480749289185:inference-profile/global.anthropic.claude-opus-4-5*"]}]}
```

**Verify:** a one-token invoke on Opus 4.6 succeeds, and the same call against
`global.anthropic.claude-sonnet-4-6-*` returns AccessDenied. Both results matter — the second is
the actual test.

**Cost:** model tokens only, ~$5/$25 per M (list). Budget line: $1,000 over 90 days.

---

## Step 3 — AgentCore control plane + runtime

**You do:** attach to `bharat-factory-orchestrator`:

```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":[
   "bedrock-agentcore:CreateAgentRuntime","bedrock-agentcore:UpdateAgentRuntime",
   "bedrock-agentcore:GetAgentRuntime","bedrock-agentcore:ListAgentRuntimes",
   "bedrock-agentcore:InvokeAgentRuntime","bedrock-agentcore:DeleteAgentRuntime"],
  "Resource":"arn:aws:bedrock-agentcore:ap-south-1:480749289185:runtime/*"},
 {"Effect":"Allow","Action":"iam:PassRole","Resource":
   "arn:aws:iam::480749289185:role/bharat-factory-agent-*",
  "Condition":{"StringEquals":{"iam:PassedToService":"bedrock-agentcore.amazonaws.com"}}}]}
```

Plus **one execution role per agent** (`bharat-factory-agent-scientist`, `-adversary`, `-chief`,
`-reporter`), each trusted by `bedrock-agentcore.amazonaws.com`, each carrying only:
- the Step 2 model policy,
- `bedrock-agentcore:InvokeGateway` on our gateway ARN,
- CloudWatch Logs write + X-Ray put for its own log group.

Four roles rather than one is deliberate: Cedar policies (Step 5) key off agent identity, and a
shared role would make "the Scientist cannot call submit_allocation" unenforceable.

**Cost:** Runtime is $0.0895/vCPU-hour + $0.00945/GB-hour, and **I/O wait is free** — an agent
waiting on a model call bills nothing. Estimated <$10 per 90 days at our volume.

**Verify:** deploy a hello-world Strands agent, `InvokeAgentRuntime` returns its response, and the
session appears in CloudWatch.

---

## Step 4 — Gateway + the Lambda tool proxy

Agents must not hold database credentials. The path is:

```
agent → AgentCore Gateway (+Cedar) → Lambda (in our VPC) → research-tools API on the EC2 private IP
```

**You do:**
1. Create Lambda `bharat-factory-tools-proxy` in the VPC, in the same subnet as the EC2 box, with
   a security group `sg-factory-lambda`.
2. Add **one inbound rule** to the EC2 security group: TCP on the tools-API port, source
   `sg-factory-lambda` only. No public exposure, no NAT gateway, no VPC endpoints needed — the
   Lambda only talks to a private IP.
3. Store the tools-API bearer token in Secrets Manager (`factory/tools-api-token`); grant the
   Lambda role `secretsmanager:GetSecretValue` on that one secret.
4. Grant the orchestrator `bedrock-agentcore:CreateGateway|CreateGatewayTarget|UpdateGateway` on
   `gateway/*`.

**Cost:** Gateway $0.005 per 1k invocations; Lambda within free tier at this volume. Under $5 per
90 days.

**Verify:** from the agent, a `research___search_memory` call returns ledger rows; the EC2 tools
port remains unreachable from anywhere except the Lambda SG (test from the host itself).

---

## Step 5 — Policy (Cedar), default-deny

**You do:** grant `bedrock-agentcore:CreatePolicy|UpdatePolicy|ListPolicies` and attach the policy
engine to the gateway.

The matrix is in `RESEARCH_FACTORY_SPEC_v2_2026-09-12.md` §4.2. The property to verify is negative,
not positive: **the Scientist calling `submit_allocation` must be DENIED at the gateway**, not
merely absent from its tool list. CI holds deny tests for this.

**Cost:** $0.000025 per request — cents.

---

## Step 6 — Observability + Evaluations

**You do:** grant `cloudwatch:PutMetricData` (namespace `Factory`), `logs:*` on
`/aws/bedrock-agentcore/*`, `xray:PutTraceSegments`, and
`bedrock-agentcore:CreateEvaluator|StartEvaluation|ListEvaluations`. Enable **CloudWatch
Transaction Search** in ap-south-1 (a console toggle; Evaluations reads spans from it).

**Cost:** Evaluations built-in $0.0024/1k input + $0.012/1k output tokens; custom $1.50/1k. Budget
line $200 over 90 days at ~30 sampled traces/day. CloudWatch spans/logs ~$100.

---

## Step 7 — The report dead-man switch (independent of everything above)

This one is worth doing **even if AgentCore never happens**, because it protects the daily report
from a silent EC2 freeze — which has happened twice.

**You do:**
1. Create SNS topic `factory-alerts`; subscribe your email; confirm the subscription.
2. Grant the orchestrator `sns:Publish` on that topic and `cloudwatch:PutMetricAlarm`.
3. The report workflow pushes `Factory/ReportDelivered=1` on success; the alarm fires on
   `TreatMissingData=breaching` over 24h.

**Cost:** alarm $0.10/month, SNS email free.

---

## Order, and what each step unlocks

| Step | Unlocks | Blocked without it |
|---|---|---|
| 1 Role | everything else | — |
| 2 Model scope | agents can think | all LLM work |
| 3 Runtime | agents run off the production host | agents would run in-process on a loaded t3.xlarge |
| 4 Gateway + Lambda | agents get tools without credentials | agents would need DB access directly |
| 5 Policy | permissions enforced outside agent code | "cannot promote" rests on prompt discipline |
| 6 Observability | traces, online evals | no agent quality measurement |
| 7 SNS + alarm | report failure is detectable | a frozen host silently stops the report |

Steps 1-3 are the minimum to run a single agent end to end. Step 7 is independent and cheap — I'd
do it first regardless.

---

## What I still cannot verify, and how we find out

| Unknown | How to settle it | When |
|---|---|---|
| Whether Gateway can reach a Lambda in our VPC as drawn | deploy the hello-world path in Step 4 | Day 1 of the spike |
| Exact Cedar entity type names for AgentCore principals | read them off the first created policy | Step 5 |
| Whether Bedrock prompt caching applies to Opus 4.5/4.6 here | measure `cache_read_input_tokens` on a repeated prefix | Step 2 |
| Real per-token Bedrock price (the pricing page did not render 4.x rows) | first AWS bill, or grant `ce:GetCostAndUsage` | Week 1 |
| Whether us-east-1 is a separate RPM pool | 3-call burst from us-east-1 off-market, watching prod throttles | optional |

## Total cost of the AgentCore layer itself

Runtime + Gateway + Policy + Lambda + alarms ≈ **$65 per 90 days**. The spend that matters is model
tokens ($1,000) and Evaluations ($200) — the infrastructure is close to free. That is worth knowing
before deciding: the decision is about the model budget, not about AgentCore.
