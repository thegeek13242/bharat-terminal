# Power Automate Spec — Taperead demo-request → approval → onboarding

Handoff spec for building the Microsoft/Power Automate side of the Taperead
demo-request pipeline. Written to be implementable **without any other context**
(e.g. by Claude Code for Web). The custom code on both ends (Cloudflare Pages
Functions + the bharat-terminal VM poller) is **already built**; this document
specifies only the flows you build in the M365 tenant and the exact HTTP
contracts that connect them.

---

## 1. End-to-end picture

```
Visitor → landing form (OTP-verified) ──▶ Cloudflare Pages Function
     POST /api/demo/otp   → emails a 6-digit code   ─┐
     POST /api/demo       → verifies code, inserts    │  both call FLOW 1 (email)
                            demo_requests(status=      │
                            'pending'), then:          │
       • POST TEAMS_ALERT_WEBHOOK_URL (flat JSON) ─────┼──▶ FLOW 2 (approval)
       • POST EMAIL_WORKFLOW_URL (ack email) ──────────┘
                                                        FLOW 2:
                                                          • post card to Teams
                                                          • Start & wait approval
                                                          • on Approve/Reject →
                                                            POST /api/demo/decision
                                                                     │
   bharat-terminal VM poller (already built) ◀── polls /api/demo/queue
     for each approved row:
        • create app_users account (invite allowlist)
        • POST EMAIL_WORKFLOW_URL (onboarding email)  ← FLOW 1 again
        • POST /api/demo/provisioned
```

**You build two flows:**
- **FLOW 1 — Send transactional email.** One generic HTTP-triggered flow that
  sends an email. Called for all three email types (OTP code, acknowledgment,
  onboarding). Its trigger URL becomes the `EMAIL_WORKFLOW_URL` secret.
- **FLOW 2 — Demo request approval.** HTTP-triggered by each verified demo
  request. Posts to Teams, runs a Microsoft Approval, and writes the decision
  back. Its trigger URL becomes the `TEAMS_ALERT_WEBHOOK_URL` secret.

You do **not** build a flow for account creation or the onboarding email send —
the VM poller does the provisioning and calls FLOW 1 for the onboarding email.

---

## 2. Parameters you need

| Name | Where it's used | Example |
|---|---|---|
| `LANDING_API_BASE` | base URL of the landing site | `https://taperead.trade` |
| `DECISION_TOKEN` | bearer secret FLOW 2 sends to `/api/demo/decision` | long random string |
| Approver(s) | who receives the approval | `you@taperead.in` |
| Teams channel | where the alert card is posted | e.g. "Demo Requests" |

`DECISION_TOKEN` must be an exact match with the `DECISION_TOKEN` secret set on
the Cloudflare Pages project. Generate one (`openssl rand -hex 32`) and give the
same value to both sides.

Connectors used: **Request** (When an HTTP request is received), **Office 365
Outlook** (Send an email V2), **Approvals** (Start and wait for an approval),
**Microsoft Teams** (Post card in a chat or channel), **HTTP** (POST back to the
decision endpoint).

---

## 3. FLOW 1 — "Taperead: Send transactional email"

Generic mailer. Anything that needs to send email POSTs `{to, subject, html,
text}` to this flow's URL.

### Trigger
**When an HTTP request is received** (method `POST`). Request body JSON schema:

```json
{
  "type": "object",
  "properties": {
    "to":      { "type": "string" },
    "subject": { "type": "string" },
    "html":    { "type": "string" },
    "text":    { "type": "string" }
  },
  "required": ["to", "subject", "html"]
}
```

### Actions
1. **Office 365 Outlook → Send an email (V2)**
   - **To**: `triggerBody()?['to']`
   - **Subject**: `triggerBody()?['subject']`
   - **Body**: `triggerBody()?['html']`  (leave "Is HTML" = Yes, the default for V2)
   - Optionally set a friendly **From (Send as)** if you use a shared mailbox
     (e.g. `hello@taperead.in`) — requires Send-As permission.
2. **Request → Response** (add this so the flow is synchronous and the caller
   knows the mail was accepted):
   - **Status Code**: `200`
   - Body: `{ "ok": true }`

> The **Response** action matters: the OTP call on the landing side blocks on
> this flow and treats a non-2xx (or timeout) as "couldn't send the code". Put
> the Response **after** the Send email action.

### After saving
Copy the trigger's **HTTP POST URL**. This is `EMAIL_WORKFLOW_URL`. It must be
set in **two** places:
- Cloudflare Pages → project **taperead** → Settings → Environment variables
  (encrypted): `EMAIL_WORKFLOW_URL`.
- bharat-terminal VM `.env`: `EMAIL_WORKFLOW_URL` (the poller uses the same flow
  for the onboarding email).

### Test
```bash
curl -X POST '<FLOW1_URL>' -H 'Content-Type: application/json' \
  -d '{"to":"you@yourdomain.com","subject":"Flow1 test","html":"<p>hello</p>","text":"hello"}'
```
Expect the email to arrive and a `200 {"ok":true}` response.

---

## 4. FLOW 2 — "Taperead: Demo request approval"

Triggered once per **verified** demo request. Note: it must return quickly and
then wait for a human, so **do not** add a Response action — let the trigger
auto-return `202 Accepted` and continue asynchronously.

### Trigger
**When an HTTP request is received** (method `POST`). Request body JSON schema:

```json
{
  "type": "object",
  "properties": {
    "event":      { "type": "string" },
    "id":         { "type": "integer" },
    "email":      { "type": "string" },
    "firm":       { "type": ["string", "null"] },
    "referrer":   { "type": ["string", "null"] },
    "ip":         { "type": ["string", "null"] },
    "user_agent": { "type": ["string", "null"] },
    "created_at": { "type": "string" }
  },
  "required": ["id", "email"]
}
```

`id` is the `demo_requests` row id — you pass it back verbatim in the decision
call. `event` is always `"demo_request"` (guard on it if you like).

### Actions

1. **(Optional) Microsoft Teams → Post card in a chat or channel** — team
   visibility. Post to your "Demo Requests" channel. Suggested Adaptive Card
   text (bind the trigger fields):
   ```
   🚀 New demo request  (#@{triggerBody()?['id']})
   Email: @{triggerBody()?['email']}
   Firm:  @{triggerBody()?['firm']}
   Referrer: @{triggerBody()?['referrer']}
   Received: @{triggerBody()?['created_at']}
   ```

2. **Approvals → Start and wait for an approval**
   - **Approval type**: `Approve/Reject – First to respond`
   - **Title**: `Demo request: @{triggerBody()?['email']}`
   - **Assigned to**: your approver(s)
   - **Details** (Markdown):
     ```
     **Email:** @{triggerBody()?['email']}
     **Firm:** @{triggerBody()?['firm']}
     **Referrer:** @{triggerBody()?['referrer']}
     **Request ID:** @{triggerBody()?['id']}
     ```

3. **Condition** — `Outcome` (from the approval) `is equal to` `Approve`.

   - **If yes → HTTP (POST decision = approved)**
     - Method: `POST`
     - URI: `@{concat('<LANDING_API_BASE>', '/api/demo/decision')}`
       (or hardcode `https://taperead.trade/api/demo/decision`)
     - Headers:
       ```
       Content-Type : application/json
       Authorization: Bearer <DECISION_TOKEN>
       ```
     - Body:
       ```json
       {
         "id": @{triggerBody()?['id']},
         "decision": "approved",
         "decided_by": "@{triggerBody()?['email']}"
       }
       ```
       > For `decided_by`, prefer the approver's identity if available, e.g.
       > `@{first(body('Start_and_wait_for_an_approval')?['responses'])?['responder']?['email']}`.

   - **If no → HTTP (POST decision = rejected)** — identical, but
     `"decision": "rejected"`. (Recording rejections is optional but keeps the
     row out of the queue and gives you an audit trail.)

### After saving
Copy the trigger's **HTTP POST URL**. This is `TEAMS_ALERT_WEBHOOK_URL`. Set it
in Cloudflare Pages → project **taperead** → Settings → Environment variables
(encrypted): `TEAMS_ALERT_WEBHOOK_URL`. (The VM does **not** need this one.)

### What happens after approval
FLOW 2's job ends at the decision callback. `POST /api/demo/decision` flips the
row to `status='approved'`. The bharat-terminal VM poller (already deployed
separately) then picks it up: creates the `app_users` account, sends the
onboarding email via FLOW 1, and marks it provisioned. You don't build any of
that here.

---

## 5. The decision endpoint contract (already built — for reference)

`POST {LANDING_API_BASE}/api/demo/decision`

- **Headers**: `Authorization: Bearer <DECISION_TOKEN>`, `Content-Type: application/json`
- **Body**: `{ "id": <int>, "decision": "approved" | "rejected", "decided_by": <string, optional> }`
- **Responses**:
  - `200 {"ok":true}` — recorded (also returned idempotently if the row was
    already decided, so retries are safe)
  - `400 {"error":"invalid_id"|"invalid_decision"|"invalid_json"}`
  - `401 {"error":"unauthorized"}` — bad/missing bearer
  - `404 {"error":"not_found"}` — unknown id

Only a row still in `status='pending'` is changed; a second call for the same id
is a harmless no-op success.

### Manual test (simulate an approval without waiting on the pipeline)
```bash
curl -X POST 'https://taperead.trade/api/demo/decision' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <DECISION_TOKEN>' \
  -d '{"id":1,"decision":"approved","decided_by":"you@taperead.in"}'
```

---

## 6. Security notes

- Both flow trigger URLs contain a SAS signature — **treat them as secrets**.
  Store them only as encrypted CF Pages env vars / VM `.env`, never in the repo.
- FLOW 1 can email any `to` value it's handed. That's fine because only the
  landing Functions and the VM poller know the URL, but if you want defense in
  depth, add a shared-secret header check in the trigger (e.g. require header
  `X-Api-Key: <value>`) — note this then also requires the callers to send that
  header, which is a small follow-up code change on the landing + worker side
  (not currently sent).
- `DECISION_TOKEN` is the only thing standing between the public internet and
  flipping a request to "approved". Use a long random value and rotate if leaked.

---

## 7. Checklist

- [ ] Build FLOW 1, copy URL → set `EMAIL_WORKFLOW_URL` on **CF Pages** + **VM**.
- [ ] Build FLOW 2, copy URL → set `TEAMS_ALERT_WEBHOOK_URL` on **CF Pages**.
- [ ] Generate `DECISION_TOKEN`; set on **CF Pages** and put the same value in
      FLOW 2's HTTP header.
- [ ] (CF Pages secrets also required for the code already built: `OTP_PEPPER`,
      and the D1 migrations `0003`/`0004` applied.)
- [ ] (VM `.env` also needs `PROVISION_TOKEN`, `LANDING_API_BASE`,
      `ONBOARDING_SIGNIN_URL` for the poller.)
- [ ] Test FLOW 1 with curl (section 3).
- [ ] Test the decision endpoint with curl (section 5).
- [ ] End-to-end: submit the real form → get code → verify → approve in
      Teams/Outlook → confirm the onboarding email + `app_users` row appear.
