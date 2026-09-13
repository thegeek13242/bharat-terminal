# AgentCore access — AWS CloudShell runbook

**Date:** 2026-09-13 · **Region:** `ap-south-1` · **Account:** `480749289185`
**Companion to:** `AGENTCORE_ACCESS_SETUP_2026-09-13.md` (the *why*; this file is the *how*).
**Status:** nothing below has been run. Every block is for **you** to run in CloudShell as the
account owner. I cannot run any of it — the EC2 instance role has no IAM, no AgentCore and no SNS
permissions, which is the whole reason this document exists.

Run it in **CloudShell in ap-south-1** (region selector, top right). CloudShell inherits your console
identity, so it already has the admin rights these commands need.

---

## 0. Corrections to the setup doc — read before running

I verified every action name against the live API surface in this account rather than trusting the
docs I wrote on 2026-09-12. Four things in `AGENTCORE_ACCESS_SETUP_2026-09-13.md` were wrong. The
commands below are correct; the older doc is not.

| Setup doc said | Live API | Consequence |
|---|---|---|
| `bedrock-agentcore:InvokeGateway` | **No such action.** Neither plane has it. Gateways are called over MCP using a workload-identity token (`GetWorkloadAccessToken*`, `GetResourceOauth2Token`) | An agent role built from the old doc could not have called a tool |
| `CreatePolicy` \| `UpdatePolicy` \| `ListPolicies` | Also needs **`CreatePolicyEngine`** — a policy attaches to an engine, and the engine must exist first | Step 5 would have failed at the first call |
| `CreateEvaluator` \| `StartEvaluation` \| `ListEvaluations` | `StartEvaluation` and `ListEvaluations` **do not exist**. Real: `CreateEvaluator`, `CreateOnlineEvaluationConfig`, `ListEvaluators`, data-plane `Evaluate` | Step 6 would have granted two phantom actions and missed the real one |
| Workload identity not mentioned | `CreateWorkloadIdentity` is how a runtime gets an identity to call a gateway | Step 4 would have had no auth path |

Two quota facts also moved since the plan was written:

- **Opus 4.6 has no tokens-per-day quota at all** in this account — only 5 RPM and 3M
  tokens-per-*minute*. The plan's "track Opus 4.6 TPD (2.59M) as a budget guard" rests on a quota
  that does not exist. Opus 4.5 does have a TPD (13.5M). Budget guarding has to be per-minute or
  dollar-based, not TPD.
- **Opus 5, Opus 4.7, Opus 4.8, Sonnet 5 and Fable 5/5.1 inference profiles now exist in
  ap-south-1 but carry zero quota.** So the "Opus 4.6 + 4.5 only" decision still holds — not
  because the newer pools are reserved, but because they are unusable. Worth re-checking before the
  90-day budget is committed; if Opus 5 gets quota it is the better agent model.

Verified inference-profile IDs (exact strings, checked live):

```
global.anthropic.claude-opus-4-6-v1
global.anthropic.claude-opus-4-5-20251101-v1:0
```

Note these are `global.` — Claude 3.x profiles in this region are `apac.`, so the prefix is not
uniform and cannot be guessed.

**Fifth correction, found the hard way on 2026-09-13.** The first draft of this runbook had an em
dash (U+2014) inside `--description`. IAM rejects it:

```
ValidationError: Value at 'description' failed to satisfy constraint:
Member must satisfy regular expression pattern: [\u0009\u000A\u000D\u0020-\u007E\u00A1-\u00FF]*
```

That range is ASCII plus Latin-1; U+2014 is in neither. **Every command block below is now pure
ASCII** — prose keeps its typography, commands do not. If you edit this file, keep it that way:
non-ASCII in a pasted command is also the kind of thing that survives one terminal and breaks in
another, which is far harder to diagnose than the clean error above.

**Sixth correction, and the one with a security edge.** The first draft derived a
`CALLER_PRINCIPAL` from the CloudShell identity and wrote it into the orchestrator's trust policy so
the verification tests could run from CloudShell. That was wrong twice over:

- The founder's CloudShell session is the **account root**, and root can never assume a role — the
  tests could not have run from CloudShell under any circumstances.
- Worse, the fallback branch wrote `arn:aws:iam::480749289185:root` into the trust policy. In an IAM
  trust policy that string does **not** mean "the root user" — it means **"delegate to the entire
  account"**. Any IAM principal in the account could then have assumed the orchestrator role. For a
  role whose whole purpose is to be the boundary between the factory and production, that is the
  opposite of the intent.

Fixed below: the trust policy names **only** the EC2 role, and verification runs on the EC2 box via
the path production actually uses. That path is confirmed working — `bharat-terminal-ec2` assumed
`bharat-factory-orchestrator` successfully on 2026-09-13, which also proves no identity-policy
change to the production instance role is needed.

If you already created the role from the first draft, repair the trust policy with
`aws iam update-assume-role-policy` (§3) before going further.

**Seventh correction — an illegal name, and the cascade it caused.** `sg-factory-lambda` is not a
valid security-group *name*: AWS reserves the `sg-` prefix for security-group **IDs**, so
`CreateSecurityGroup` rejects it with `InvalidParameterValue ... Group names may not be in the
format sg-*`. Renamed to `bharat-factory-lambda` in §6b.

The cascade matters more than the typo. The failed create left `LAMBDA_SG` **empty**, and the very
next command fed that empty value straight into a security-group ingress rule. This time it errored
cleanly (`MissingParameter: Source group ID missing`) — but a call that happens to tolerate an empty
argument would instead have written a rule nobody intended, silently. Every `VAR=$(aws ...)` in this
runbook whose value feeds a later command now carries an explicit non-empty guard: §2's `TOPIC_ARN`
(which feeds three commands) and §6b's `LAMBDA_SG`. **Do not delete those guards, and do not paste
past one that has fired.**

---

## 1. Preflight — variables and identity

Paste this whole block first. Everything afterwards depends on it. If your CloudShell session
times out, re-paste it.

```bash
export AWS_REGION=ap-south-1
export AWS_DEFAULT_REGION=ap-south-1
export ACCT=480749289185
export EC2_ROLE=bharat-terminal-ec2
export VPC_ID=vpc-0ffc2d3f5b0209053
export SUBNET_A=subnet-09b106eedd410b9fd     # ap-south-1a - the EC2 box's subnet
export EC2_SG=sg-0ac6dd46a26006df0
export EC2_PRIVATE_IP=172.31.42.154
export TOOLS_PORT=8003                        # free: 8000/8001/8002/8080/8081/8082 are taken
export ALERT_EMAIL=aviralv35@gmail.com        # change if you want alerts elsewhere
export OPUS46=global.anthropic.claude-opus-4-6-v1
export OPUS45=global.anthropic.claude-opus-4-5-20251101-v1:0

aws sts get-caller-identity
aws --version
```

**Check before continuing:**
- `Account` is `480749289185`.
- The `Arn` is **not** `assumed-role/bharat-terminal-ec2/...`. If it is, you are on the EC2 box, not
  CloudShell, and none of this will work.
- CLI is v2. Confirm the AgentCore surface exists:

```bash
aws bedrock-agentcore-control help >/dev/null 2>&1 && echo "agentcore control plane: OK" || echo "CLI too old - run: pip install --upgrade awscli"
```

**Check whether your CloudShell identity is the account root** — it changes where verification runs:

```bash
aws sts get-caller-identity --query Arn --output text
```

If that ARN ends in `:root` (it did on 2026-09-13), then:

- **Root can never assume an IAM role.** There is no workaround. Every `assume-role` verification in
  this runbook therefore runs **on the EC2 box**, not in CloudShell. CloudShell is still where all
  the *creation* commands run — only the verification moves.
- Separately: AWS recommends not using root for routine work. Creating an IAM admin user for
  yourself is worth doing on its own merits, but nothing here requires it.

### Re-running this runbook, and which errors are harmless

`EntityAlreadyExists` from `create-role` or `create-policy` means **that step is already done**.
Harmless: skip it and carry on. `attach-role-policy` is idempotent and stays silent on a repeat.

**The one trap:** `create-policy` does **not** update an existing policy. If you edit a policy's
JSON and re-run `create-policy`, you get `EntityAlreadyExists` and the **old policy silently stays
in force** — a confident-looking error hiding stale permissions. To actually change a policy:

```bash
aws iam create-policy-version \
  --policy-arn "arn:aws:iam::${ACCT}:policy/POLICY_NAME" \
  --policy-document file:///tmp/POLICY_FILE.json \
  --set-as-default
```

### State check - what exists so far

Run this any time you lose track, especially after a command fails mid-sequence. It reads only, and
prints `MISSING` for anything not yet created:

```bash
echo "=== orchestrator: trust principals (must be the EC2 role ONLY) ==="
aws iam get-role --role-name bharat-factory-orchestrator \
  --query 'Role.AssumeRolePolicyDocument.Statement[].Principal' --output json 2>/dev/null || echo "  MISSING"

echo "=== orchestrator: attached policies ==="
aws iam list-attached-role-policies --role-name bharat-factory-orchestrator \
  --query 'AttachedPolicies[].PolicyName' --output text 2>/dev/null || echo "  MISSING"

echo "=== agent roles ==="
for A in scientist adversary chief reporter; do
  P=$(aws iam list-attached-role-policies --role-name "bharat-factory-agent-$A" \
        --query 'AttachedPolicies[].PolicyName' --output text 2>/dev/null)
  echo "  $A: ${P:-MISSING}"
done

echo "=== policies ==="
for P in BharatFactoryOpusInvoke BharatFactoryAgentCoreControl BharatFactoryAgentRuntimeBase \
         BharatFactoryGateway BharatFactoryPolicyEngine BharatFactoryObservability \
         BharatFactoryCostRead; do
  N=$(aws iam get-policy --policy-arn "arn:aws:iam::${ACCT}:policy/$P" \
        --query 'Policy.PolicyName' --output text 2>/dev/null)
  echo "  ${N:-MISSING ($P)}"
done

echo "=== sns topic / lambda SG / secret ==="
aws sns list-topics --query "Topics[?contains(TopicArn,'factory-alerts')].TopicArn" --output text
aws ec2 describe-security-groups --filters "Name=group-name,Values=bharat-factory-lambda" \
  --query 'SecurityGroups[].GroupId' --output text 2>/dev/null
aws secretsmanager list-secrets \
  --query "SecretList[?Name=='factory/tools-api-token'].Name" --output text 2>/dev/null
```

---

## 2. Step 7 first — the dead-man switch (do this even if AgentCore never happens)

Cheapest, independent of everything else, and it protects the daily report from the silent EC2
freeze that has already happened twice.

```bash
TOPIC_ARN=$(aws sns create-topic --name factory-alerts \
  --query TopicArn --output text)

# GUARD: TOPIC_ARN feeds three later commands (subscribe, publish, put-metric-alarm).
# An empty value would silently mis-target all three.
if [ -z "$TOPIC_ARN" ]; then
  echo "create-topic FAILED - stop here"
else
  echo "topic: $TOPIC_ARN"
  aws sns subscribe --topic-arn "$TOPIC_ARN" \
    --protocol email --notification-endpoint "$ALERT_EMAIL"
fi
```

**Now go to your inbox and click the confirmation link.** Then verify — this must say `true`, not
`PendingConfirmation`:

```bash
# Literal ARN, not $TOPIC_ARN: this step follows an inbox round-trip and is
# usually run in a fresh shell where that variable no longer exists.
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:ap-south-1:480749289185:factory-alerts \
  --query 'Subscriptions[].{Endpoint:Endpoint,Arn:SubscriptionArn}' --output table
```

Prove it actually delivers:

```bash
aws sns publish \
  --topic-arn arn:aws:sns:ap-south-1:480749289185:factory-alerts \
  --subject "factory-alerts test" \
  --message "If you are reading this, the dead-man switch can reach you."
```

**Do not create the CloudWatch alarm yet.** With `TreatMissingData=breaching` it goes to ALARM the
moment it exists, because the report workflow has never pushed a datapoint. Create it on the day the
report goes live:

```bash
# RUN THIS LATER - only after the report has pushed Factory/ReportDelivered at least once
aws cloudwatch put-metric-alarm \
  --alarm-name factory-report-missing \
  --alarm-description "Daily research report did not deliver in 24h" \
  --namespace Factory --metric-name ReportDelivered \
  --statistic Sum --period 86400 --evaluation-periods 1 \
  --threshold 1 --comparison-operator LessThanThreshold \
  --treat-missing-data breaching \
  --alarm-actions "arn:aws:sns:ap-south-1:480749289185:factory-alerts"
```

---

## 3. Step 1 — the orchestrator role

A separate role, not the EC2 instance role. This is the single highest-value boundary in the setup:
it makes "a compromised agent cannot reach production" a property of IAM rather than of anyone's
carefulness.

The trust policy names **exactly one** principal: the EC2 role. That is the only identity that ever
needs to assume it, and it is the path production uses. Do not add your CloudShell identity here —
§0's sixth correction explains why that was a mistake in the first draft.

```bash
cat > /tmp/trust-orchestrator.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow",
  "Principal":{"AWS":"arn:aws:iam::${ACCT}:role/${EC2_ROLE}"},
  "Action":"sts:AssumeRole"}]}
JSON

aws iam create-role \
  --role-name bharat-factory-orchestrator \
  --assume-role-policy-document file:///tmp/trust-orchestrator.json \
  --description "Bharat Terminal research factory orchestrator - assumed by the EC2 role only"
```

**If you already created this role from the first draft**, its trust policy still carries the
account-wide `:root` entry. Repair it in place — same file, one different verb:

```bash
aws iam update-assume-role-policy \
  --role-name bharat-factory-orchestrator \
  --policy-document file:///tmp/trust-orchestrator.json

aws iam get-role --role-name bharat-factory-orchestrator \
  --query 'Role.AssumeRolePolicyDocument.Statement[].Principal' --output json
```

The second command must print the EC2 role ARN and nothing else. If `:root` still appears, the
update did not take and any IAM principal in the account can still assume this role.

**Verify — on the EC2 box, not in CloudShell.** Root cannot assume a role, and this is the better
test regardless: it exercises the path production actually uses.

> Already run and **passing** as of 2026-09-13 — it returned
> `arn:aws:sts::480749289185:assumed-role/bharat-factory-orchestrator/ec2-path-test`.

```bash
# ON THE EC2 BOX - not CloudShell
aws sts assume-role \
  --role-arn "arn:aws:iam::480749289185:role/bharat-factory-orchestrator" \
  --role-session-name preflight --duration-seconds 900 \
  --query 'AssumedRoleUser.Arn' --output text
```

Worth recording: this needed **no** identity-policy change on `bharat-terminal-ec2`. For
same-account assumption, a trust policy naming the principal explicitly is sufficient on its own —
so the production instance role's permissions stay exactly as they were.

Define this helper **on the EC2 box** — later steps use it to run a command *as the orchestrator*
rather than as the instance role, which is the only way the negative tests mean anything. It hard-codes
the account and region because the CloudShell exports from §1 do not exist on the box:

```bash
# ON THE EC2 BOX - not CloudShell
as_orchestrator() {
  read -r AK SK ST < <(aws sts assume-role \
    --role-arn "arn:aws:iam::480749289185:role/bharat-factory-orchestrator" \
    --role-session-name verify --duration-seconds 900 \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
  env AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST" \
      AWS_REGION=ap-south-1 AWS_DEFAULT_REGION=ap-south-1 "$@"
}
```

---

## 4. Step 2 — model access, scoped to the two idle Opus pools

This is the permission that decides whether the factory can ever eat production's capacity. Sonnet
4.6, Sonnet 4.5 and Haiku 4.5 are **absent** from the resource list, so the factory cannot reach
them even if misconfigured. Absence is the enforcement — there is no Deny to forget.

The foundation-model ARNs use `*` for region deliberately: a `global.` inference profile routes to
whichever region has capacity, and Bedrock authorizes against the foundation-model ARN in *that*
region.

```bash
cat > /tmp/pol-opus.json <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
 "Resource":[
  "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-6-v1",
  "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-4-5-20251101-v1:0",
  "arn:aws:bedrock:${AWS_REGION}:${ACCT}:inference-profile/${OPUS46}",
  "arn:aws:bedrock:${AWS_REGION}:${ACCT}:inference-profile/${OPUS45}"]}]}
JSON

aws iam create-policy --policy-name BharatFactoryOpusInvoke \
  --policy-document file:///tmp/pol-opus.json \
  --description "Invoke ONLY the two idle Opus pools. Production pools are unreachable by omission."

aws iam attach-role-policy --role-name bharat-factory-orchestrator \
  --policy-arn "arn:aws:iam::${ACCT}:policy/BharatFactoryOpusInvoke"
```

### Verify — on the EC2 box. Both results matter, and the second matters more

Positive test (expect a response). The model ID is spelled out because `$OPUS46` is a CloudShell
export and is empty on the box:

```bash
# ON THE EC2 BOX - not CloudShell
as_orchestrator aws bedrock-runtime invoke-model \
  --model-id global.anthropic.claude-opus-4-6-v1 \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":4,"messages":[{"role":"user","content":"say ok"}]}' \
  --cli-binary-format raw-in-base64-out /tmp/opus46.json && cat /tmp/opus46.json
```

**Negative test — this is the actual test.** It must FAIL with `AccessDeniedException`. If it
succeeds, the factory can consume production's Sonnet capacity and you must stop and fix the policy:

```bash
# ON THE EC2 BOX - not CloudShell
as_orchestrator aws bedrock-runtime invoke-model \
  --model-id global.anthropic.claude-sonnet-4-6 \
  --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":4,"messages":[{"role":"user","content":"say ok"}]}' \
  --cli-binary-format raw-in-base64-out /tmp/should-fail.json \
  && echo "*** SECURITY FAILURE: Sonnet was reachable - do not proceed ***" \
  || echo "expected AccessDenied - production pool is protected"
```

Also worth measuring here (the setup doc lists it as an open unknown — whether Bedrock prompt
caching applies to Opus 4.5/4.6 in this region). Send a repeated long prefix and read
`cache_read_input_tokens` from the response `usage`.

---

## 5. Step 3 — AgentCore control plane + four agent execution roles

```bash
cat > /tmp/pol-agentcore.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":[
   "bedrock-agentcore:CreateAgentRuntime","bedrock-agentcore:UpdateAgentRuntime",
   "bedrock-agentcore:GetAgentRuntime","bedrock-agentcore:ListAgentRuntimes",
   "bedrock-agentcore:DeleteAgentRuntime","bedrock-agentcore:InvokeAgentRuntime",
   "bedrock-agentcore:CreateAgentRuntimeEndpoint","bedrock-agentcore:GetAgentRuntimeEndpoint",
   "bedrock-agentcore:ListAgentRuntimeEndpoints","bedrock-agentcore:ListAgentRuntimeVersions",
   "bedrock-agentcore:DeleteAgentRuntimeEndpoint","bedrock-agentcore:StopRuntimeSession",
   "bedrock-agentcore:ListSessions",
   "bedrock-agentcore:CreateWorkloadIdentity","bedrock-agentcore:GetWorkloadIdentity",
   "bedrock-agentcore:ListWorkloadIdentities"],
  "Resource":"*"},
 {"Effect":"Allow","Action":"iam:PassRole",
  "Resource":"arn:aws:iam::${ACCT}:role/bharat-factory-agent-*",
  "Condition":{"StringEquals":{"iam:PassedToService":"bedrock-agentcore.amazonaws.com"}}}]}
JSON

aws iam create-policy --policy-name BharatFactoryAgentCoreControl \
  --policy-document file:///tmp/pol-agentcore.json

aws iam attach-role-policy --role-name bharat-factory-orchestrator \
  --policy-arn "arn:aws:iam::${ACCT}:policy/BharatFactoryAgentCoreControl"
```

`Resource:"*"` on the first statement is deliberate and temporary: runtime ARNs contain
AWS-generated IDs that do not exist yet, so there is nothing to scope to. **Tighten this to the real
runtime ARNs right after Step 3's hello-world deploy** — it is on the teardown checklist at the
bottom.

### The four agent roles

Four rather than one is deliberate: Cedar policies (Step 5) key off agent identity, so a shared role
would make "the Scientist cannot call `submit_allocation`" unenforceable.

```bash
cat > /tmp/trust-agent.json <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
 "Action":"sts:AssumeRole",
 "Condition":{
   "StringEquals":{"aws:SourceAccount":"${ACCT}"},
   "ArnLike":{"aws:SourceArn":"arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCT}:*"}}}]}
JSON

cat > /tmp/pol-agent-base.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":[
   "logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],
  "Resource":"arn:aws:logs:${AWS_REGION}:${ACCT}:log-group:/aws/bedrock-agentcore/*"},
 {"Effect":"Allow","Action":[
   "xray:PutTraceSegments","xray:PutTelemetryRecords"],"Resource":"*"},
 {"Effect":"Allow","Action":[
   "bedrock-agentcore:GetWorkloadAccessToken",
   "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
   "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
   "bedrock-agentcore:GetResourceOauth2Token",
   "bedrock-agentcore:GetResourceApiKey"],
  "Resource":"*"}]}
JSON

aws iam create-policy --policy-name BharatFactoryAgentRuntimeBase \
  --policy-document file:///tmp/pol-agent-base.json

for A in scientist adversary chief reporter; do
  aws iam create-role --role-name "bharat-factory-agent-$A" \
    --assume-role-policy-document file:///tmp/trust-agent.json \
    --description "AgentCore execution role - $A"
  aws iam attach-role-policy --role-name "bharat-factory-agent-$A" \
    --policy-arn "arn:aws:iam::${ACCT}:policy/BharatFactoryOpusInvoke"
  aws iam attach-role-policy --role-name "bharat-factory-agent-$A" \
    --policy-arn "arn:aws:iam::${ACCT}:policy/BharatFactoryAgentRuntimeBase"
done
```

**Verify:**

```bash
for A in scientist adversary chief reporter; do
  echo "--- $A"
  aws iam list-attached-role-policies --role-name "bharat-factory-agent-$A" \
    --query 'AttachedPolicies[].PolicyName' --output text
done
```

Each should list both `BharatFactoryOpusInvoke` and `BharatFactoryAgentRuntimeBase`. Then, **on the
EC2 box**, confirm the orchestrator can actually reach the control plane:

```bash
# ON THE EC2 BOX - not CloudShell
as_orchestrator aws bedrock-agentcore-control list-agent-runtimes --output table
```

That must return an empty list — **not** AccessDenied. It is the moment the orchestrator can
genuinely talk to the AgentCore control plane, and it is the finish line for "Steps 1-3 are the
minimum to run a single agent end to end".

---

## 6. Step 4 — Gateway + Lambda tool proxy

> **Blocked on code that does not exist.** `bharat_terminal/factory/tools_api/` has not been built —
> the factory directory today holds `dsl, registry, snapshot, backtest, stats, gates, costs,
> feedback, workflows, report` and no tools API. The **permission grants** below are runnable now;
> the **Lambda and Gateway creation** must wait until there is something for the Lambda to proxy to.

The path is `agent → Gateway (+Cedar) → Lambda in our VPC → tools API on 172.31.42.154`. Agents
never hold database credentials.

### 6a. Permissions (runnable now)

```bash
cat > /tmp/pol-gateway.json <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":[
  "bedrock-agentcore:CreateGateway","bedrock-agentcore:UpdateGateway",
  "bedrock-agentcore:GetGateway","bedrock-agentcore:ListGateways",
  "bedrock-agentcore:DeleteGateway",
  "bedrock-agentcore:CreateGatewayTarget","bedrock-agentcore:UpdateGatewayTarget",
  "bedrock-agentcore:GetGatewayTarget","bedrock-agentcore:ListGatewayTargets",
  "bedrock-agentcore:DeleteGatewayTarget","bedrock-agentcore:SynchronizeGatewayTargets",
  "bedrock-agentcore:CreateOauth2CredentialProvider",
  "bedrock-agentcore:CreateApiKeyCredentialProvider",
  "bedrock-agentcore:GetTokenVault"],
 "Resource":"*"}]}
JSON

aws iam create-policy --policy-name BharatFactoryGateway \
  --policy-document file:///tmp/pol-gateway.json
aws iam attach-role-policy --role-name bharat-factory-orchestrator \
  --policy-arn "arn:aws:iam::${ACCT}:policy/BharatFactoryGateway"
```

### 6b. The security-group rule (runnable now, and worth doing early)

A dedicated SG for the Lambda, and exactly one inbound rule on the EC2 SG sourced from it. No public
exposure, no NAT gateway, no VPC endpoints — the Lambda only ever talks to a private IP.

```bash
LAMBDA_SG=$(aws ec2 create-security-group \
  --group-name bharat-factory-lambda \
  --description "Bharat factory tools-proxy Lambda" \
  --vpc-id "$VPC_ID" --query GroupId --output text)

# GUARD: an empty LAMBDA_SG must never reach a security-group rule.
if [ -z "$LAMBDA_SG" ]; then
  echo "create-security-group FAILED - stop here, do NOT run the ingress command below"
else
  echo "lambda sg: $LAMBDA_SG"
fi
```

Only once that printed an `sg-...` ID, add the single inbound rule to the EC2 security group. This
is the command that actually writes a firewall rule, so it carries its own guard rather than
trusting the one above:

```bash
if [ -z "$LAMBDA_SG" ] || [ "$LAMBDA_SG" = "None" ]; then
  echo "LAMBDA_SG is empty - refusing to modify the EC2 security group"
else
  aws ec2 authorize-security-group-ingress \
    --group-id "$EC2_SG" \
    --ip-permissions "IpProtocol=tcp,FromPort=${TOOLS_PORT},ToPort=${TOOLS_PORT},UserIdGroupPairs=[{GroupId=${LAMBDA_SG},Description=factory tools proxy}]"
fi
```

**Verify the rule is SG-sourced and not CIDR-open** — if this prints any `0.0.0.0/0`, stop:

```bash
aws ec2 describe-security-groups --group-ids "$EC2_SG" \
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`${TOOLS_PORT}\`]" --output json
```

### 6c. Secrets (runnable now — never echo the value)

Generate the token in-shell and write it straight to Secrets Manager. It is never printed, never
pasted, and never reaches a chat window or a log:

```bash
aws secretsmanager create-secret --name factory/tools-api-token \
  --description "Bearer token: Lambda proxy -> factory tools API" \
  --secret-string "$(openssl rand -hex 32)" \
  --query ARN --output text
```

To put the same value on the EC2 side later, read it *on the box* from Secrets Manager rather than
copying it by hand.

### 6d. Lambda + Gateway — deferred

Blocked on `tools_api` existing. When it does, the Lambda needs: VPC config
(`--vpc-config SubnetIds=${SUBNET_A},SecurityGroupIds=${LAMBDA_SG}`),
`secretsmanager:GetSecretValue` on that one secret ARN, and `AWSLambdaVPCAccessExecutionRole`.

Two things to settle at that point, both currently unverified:

- **Whether Gateway can reach a Lambda in our VPC as drawn.** This is the one architectural
  assumption in the whole design I have not been able to test.
- **The subnet is single-AZ** (`ap-south-1a`). A Lambda pinned to one subnet dies with that AZ. I
  could not enumerate sibling subnets — the EC2 role lacks `ec2:DescribeSubnets` — so run this in
  CloudShell and add a second subnet if one exists:

```bash
aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" \
  --query 'Subnets[].{Id:SubnetId,AZ:AvailabilityZone,Cidr:CidrBlock}' --output table
```

---

## 7. Step 5 — Policy engine (Cedar), default-deny

```bash
cat > /tmp/pol-cedar.json <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":[
  "bedrock-agentcore:CreatePolicyEngine","bedrock-agentcore:UpdatePolicyEngine",
  "bedrock-agentcore:GetPolicyEngine","bedrock-agentcore:ListPolicyEngines",
  "bedrock-agentcore:DeletePolicyEngine",
  "bedrock-agentcore:CreatePolicy","bedrock-agentcore:UpdatePolicy",
  "bedrock-agentcore:GetPolicy","bedrock-agentcore:ListPolicies",
  "bedrock-agentcore:DeletePolicy",
  "bedrock-agentcore:StartPolicyGeneration","bedrock-agentcore:GetPolicyGeneration",
  "bedrock-agentcore:ListPolicyGenerations"],
 "Resource":"*"}]}
JSON

aws iam create-policy --policy-name BharatFactoryPolicyEngine \
  --policy-document file:///tmp/pol-cedar.json
aws iam attach-role-policy --role-name bharat-factory-orchestrator \
  --policy-arn "arn:aws:iam::${ACCT}:policy/BharatFactoryPolicyEngine"
```

Note `CreatePolicyEngine` — a policy attaches to an engine and the engine must exist first. The
setup doc missed this; the call order is engine, then policy.

The matrix is in `RESEARCH_FACTORY_SPEC_v2_2026-09-12.md` §4.2. **The property to verify is
negative:** the Scientist calling `submit_allocation` must be *DENIED at the gateway*, not merely
absent from its tool list. Absent-from-the-list is prompt discipline; denied-at-the-gateway is
enforcement. CI holds deny tests for this.

Exact Cedar entity-type names for AgentCore principals are still unknown — read them off the first
created policy.

---

## 8. Step 6 — Observability + Evaluations

```bash
cat > /tmp/pol-obs.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":"cloudwatch:PutMetricData","Resource":"*",
  "Condition":{"StringEquals":{"cloudwatch:namespace":"Factory"}}},
 {"Effect":"Allow","Action":[
   "cloudwatch:PutMetricAlarm","cloudwatch:DescribeAlarms","cloudwatch:DeleteAlarms",
   "cloudwatch:GetMetricStatistics","cloudwatch:GetMetricData"],"Resource":"*"},
 {"Effect":"Allow","Action":[
   "logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents",
   "logs:DescribeLogGroups","logs:DescribeLogStreams","logs:StartQuery","logs:GetQueryResults"],
  "Resource":"arn:aws:logs:${AWS_REGION}:${ACCT}:log-group:/aws/bedrock-agentcore/*"},
 {"Effect":"Allow","Action":["xray:PutTraceSegments","xray:PutTelemetryRecords",
   "xray:GetTraceSummaries","xray:BatchGetTraces"],"Resource":"*"},
 {"Effect":"Allow","Action":[
   "bedrock-agentcore:CreateEvaluator","bedrock-agentcore:UpdateEvaluator",
   "bedrock-agentcore:GetEvaluator","bedrock-agentcore:ListEvaluators",
   "bedrock-agentcore:DeleteEvaluator",
   "bedrock-agentcore:CreateOnlineEvaluationConfig","bedrock-agentcore:UpdateOnlineEvaluationConfig",
   "bedrock-agentcore:GetOnlineEvaluationConfig","bedrock-agentcore:ListOnlineEvaluationConfigs",
   "bedrock-agentcore:DeleteOnlineEvaluationConfig",
   "bedrock-agentcore:Evaluate"],
  "Resource":"*"},
 {"Effect":"Allow","Action":"sns:Publish",
  "Resource":"arn:aws:sns:${AWS_REGION}:${ACCT}:factory-alerts"}]}
JSON

aws iam create-policy --policy-name BharatFactoryObservability \
  --policy-document file:///tmp/pol-obs.json
aws iam attach-role-policy --role-name bharat-factory-orchestrator \
  --policy-arn "arn:aws:iam::${ACCT}:policy/BharatFactoryObservability"
```

The `cloudwatch:namespace` condition means the factory can only write metrics under `Factory/` — it
cannot pollute or overwrite production's metrics.

**CloudWatch Transaction Search** must be enabled in ap-south-1 for Evaluations to read spans.
This is a console toggle: **CloudWatch → Application Signals → Transaction Search → Enable**.
There is an X-Ray CLI path for this, but I could not verify the exact call shape against this
account, so I am not going to guess it into a runbook — use the console.

**Verify:**

```bash
# ON THE EC2 BOX - not CloudShell
as_orchestrator aws cloudwatch put-metric-data \
  --namespace Factory --metric-name SetupProbe --value 1
as_orchestrator aws bedrock-agentcore-control list-evaluators --output table
```

---

## 9. Optional — cost visibility and a budget guard

Not in the original 7 steps. I am adding it because the $3,000 / 90-day figure is a founder decision
with **no enforcement behind it today**, and because the setup doc lists "real per-token Bedrock
price" as an open unknown that `ce:GetCostAndUsage` settles.

```bash
cat > /tmp/pol-cost.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":[
  "ce:GetCostAndUsage","ce:GetCostAndUsageWithResources","ce:GetDimensionValues",
  "pricing:GetProducts","pricing:DescribeServices"],"Resource":"*"}]}
JSON

aws iam create-policy --policy-name BharatFactoryCostRead \
  --policy-document file:///tmp/pol-cost.json
aws iam attach-role-policy --role-name bharat-factory-orchestrator \
  --policy-arn "arn:aws:iam::${ACCT}:policy/BharatFactoryCostRead"
```

A budget that emails you at 50/80/100% of $1,000/month (≈ the $3,000 / 90-day runway):

```bash
cat > /tmp/budget.json <<'JSON'
{"BudgetName":"bharat-factory-90d","BudgetLimit":{"Amount":"1000","Unit":"USD"},
 "TimeUnit":"MONTHLY","BudgetType":"COST"}
JSON
cat > /tmp/budget-notify.json <<JSON
[{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN",
   "Threshold":80,"ThresholdType":"PERCENTAGE"},
  "Subscribers":[{"SubscriptionType":"EMAIL","Address":"${ALERT_EMAIL}"}]}]
JSON

aws budgets create-budget --account-id "$ACCT" \
  --budget file:///tmp/budget.json \
  --notifications-with-subscribers file:///tmp/budget-notify.json
```

Budgets is a **us-east-1 global** service; if the call complains about region, prefix it with
`AWS_REGION=us-east-1`.

---

## 10. What to tighten once things exist

| When | Tighten |
|---|---|
| After the first runtime deploys | Replace `Resource:"*"` in `BharatFactoryAgentCoreControl` with the real runtime ARNs |
| After the gateway exists | Same for `BharatFactoryGateway` |
| After the first Cedar policy | Record the real entity-type names; scope `BharatFactoryPolicyEngine` to the engine ARN |
| After the report is live | Create the `factory-report-missing` alarm (§2) |
| Once verification passes | Consider removing `${CALLER_PRINCIPAL}` from the orchestrator trust policy, leaving only the EC2 role |

## 11. Teardown — if you want it all gone

```bash
for A in scientist adversary chief reporter; do
  for P in BharatFactoryOpusInvoke BharatFactoryAgentRuntimeBase; do
    aws iam detach-role-policy --role-name "bharat-factory-agent-$A" \
      --policy-arn "arn:aws:iam::${ACCT}:policy/$P" 2>/dev/null
  done
  aws iam delete-role --role-name "bharat-factory-agent-$A" 2>/dev/null
done

for P in BharatFactoryOpusInvoke BharatFactoryAgentCoreControl BharatFactoryGateway \
         BharatFactoryPolicyEngine BharatFactoryObservability BharatFactoryCostRead; do
  aws iam detach-role-policy --role-name bharat-factory-orchestrator \
    --policy-arn "arn:aws:iam::${ACCT}:policy/$P" 2>/dev/null
  aws iam delete-policy --policy-arn "arn:aws:iam::${ACCT}:policy/$P" 2>/dev/null
done
aws iam delete-role --role-name bharat-factory-orchestrator

# Re-derive the SG id by name: teardown is usually run in a fresh shell where
# $LAMBDA_SG from section 6b no longer exists, and an empty value would skip both commands.
LAMBDA_SG=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=bharat-factory-lambda" "Name=vpc-id,Values=${VPC_ID}" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
if [ -n "$LAMBDA_SG" ] && [ "$LAMBDA_SG" != "None" ]; then
  aws ec2 revoke-security-group-ingress --group-id "$EC2_SG" \
    --ip-permissions "IpProtocol=tcp,FromPort=${TOOLS_PORT},ToPort=${TOOLS_PORT},UserIdGroupPairs=[{GroupId=${LAMBDA_SG}}]"
  aws ec2 delete-security-group --group-id "$LAMBDA_SG"
else
  echo "no bharat-factory-lambda SG found in ${VPC_ID} - nothing to revoke or delete"
fi
aws secretsmanager delete-secret --secret-id factory/tools-api-token --force-delete-without-recovery
aws sns delete-topic --topic-arn "arn:aws:sns:${AWS_REGION}:${ACCT}:factory-alerts"
```

Nothing above modifies an existing permission, so teardown returns the account to its current state.

---

## 12. Still unverified after all of this

| Unknown | How it gets settled |
|---|---|
| Whether Gateway can reach a Lambda in our VPC as drawn | Deploy the §6d hello-world path. This is the one architectural assumption I could not test |
| Exact Cedar entity-type names for AgentCore principals | Read them off the first created policy |
| Whether Bedrock prompt caching applies to Opus 4.5/4.6 here | Measure `cache_read_input_tokens` on a repeated prefix (§4) |
| Real per-token Bedrock price — the pricing page never rendered 4.x rows | First bill, or `ce:GetCostAndUsage` after §9 |
| Whether Transaction Search has a reliable CLI path in ap-south-1 | Console for now; I would not guess the call |
| Whether Opus 5 / 4.7 / 4.8 ever get quota here | Re-run the §0 quota check before committing the model budget |
