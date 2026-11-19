# Relay

## Distributed, Collaborative, Multi-Provider Agentic Software Development Framework

**Specification version:** 1.0  
**Date:** 19 November 2026  
**Status:** Implementation brief  
**Reference agent repository:** https://github.com/andrealaforgia/claude-code-agents

## 1. Mission

Build Relay, a distributed, event-driven framework in which Claude Code and OpenAI Codex agents collaborate to discover problems, decompose them into user stories and behaviours, write failing acceptance tests, implement code, review changes and validate quality and security.

Claude Code is the default Product Owner interface. Codex may act as an independent counterpart to Claude at chosen workflow stages, even when running on another computer. The framework must also work with Claude alone, without Codex installed.

Use Redis Streams to transport commands and events between workers. Keep authoritative workflow state and the durable event ledger in a transactional database. Expose the roadmap, agent conversations, event history, token consumption, estimated cost and optional live log tails in a dashboard.

The default development mode uses outside-in ATDD, short feedback loops, small changes, continuous integration and trunk-based development. Pause for Product Owner approval after every completed story unless bounded autonomous execution has been explicitly enabled.

**Core rule:** Agents negotiate and perform work. Deterministic application code validates evidence and controls workflow transitions.

## 2. Design principles

1. **Real collaboration:** Agents challenge one another with explicit arguments and evidence, rather than rubber-stamping outputs.
2. **Behaviour-led progress:** Progress is demonstrated through observable behaviour, not code volume or completed tickets.
3. **Verified red before green:** Each behaviour begins with an acceptance test that fails for the intended reason against a recorded baseline.
4. **Separation of responsibilities:** The Verifier owns the acceptance contract, the Builder owns implementation, QA owns test-design findings, and Sentinel owns security findings.
5. **Short feedback loops:** Integrate small, reviewed changes without defaulting to long-lived branches or mandatory pull requests.
6. **Product Owner authority:** Unresolved product decisions are returned to the human. Default to approval at each story boundary.
7. **Provider independence:** All provider and model choices are configuration, not embedded workflow logic.
8. **Recovery:** At-least-once transport, idempotency, leases, durable checkpoints and replayable projections must prevent silent progress loss.
9. **Evidence over confidence:** A model's assertion of success does not substitute for actual test results or an authorised transition.

## 3. Architecture

```text
                    Product Owner
                          |
                Claude Code entry point
                          |
                   Relay Control Plane
       workflow FSM | scheduler | policies | budgets
                          |
                Redis Streams transport
                    /             \
             Claude workers     Codex workers
           analyst/verifier/    peer analysts/
            builder/QA/etc.       reviewers
                    \             /
                Git + test execution
                          |
           PostgreSQL ledger and projections
                          |
              CLI + Web Dashboard
```

### 3.1 Control plane

The control plane manages project and run state, scheduling, dependencies, routing, approval gates, consensus outcomes, budgets, concurrency, cancellation, leases and integration permissions. Only this component may authorise canonical workflow transitions. It should not ask an LLM whether a formal transition is permitted.

### 3.2 Distributed worker runtime

Workers are long-running processes that register provider, models, installed agent definitions, supported tools, execution environment, repository access and concurrency capacity. Workers may be on different hosts and need only broker/control-plane connectivity, not a shared filesystem.

Every assignment has an immutable task ID, lease/fencing token, attempt number, correlation IDs and context references. A worker reports results, usage, logs, artefact references and test evidence.

### 3.3 Provider adapters

Define an interface for capability discovery, agent invocation, structured output, session continuation where possible, cancellation, usage and log collection. Implement Claude Code and Codex adapters independently. Use official supported SDK or CLI interfaces as appropriate and confirm actual installed capabilities.

Do not assume provider authentication, pricing, permissions, reasoning settings or session semantics are interchangeable. API keys and subscription/CLI sessions may have different restrictions and accounting.

### 3.4 Stack recommendation

Use Python with typed domain models and async I/O for the orchestrator, workers, APIs and CLI. Use Redis Streams for transport, PostgreSQL for durable workflow state and event ledger, Git for source exchange, OpenTelemetry for traces and metrics, and a lightweight web dashboard with SSE or WebSockets. Provide Docker Compose and deployable worker images. Inspect the old Relay implementation before replacing any existing components.

## 4. Existing agent integration

Re-use the canonical agent definitions in https://github.com/andrealaforgia/claude-code-agents rather than rewriting their instructions.

| Relay responsibility | Existing definition |
| --- | --- |
| Problem Interpreter | `alf-problem-analyzer` |
| Story Analyst | `alf-user-story-writer` |
| Verifier | `alf-atdd-developer`, restricted to acceptance-test ownership |
| Builder | `alf-atdd-developer`, restricted to production implementation |
| Refactoring specialist | `alf-clean-coder` |
| QA | `alf-test-design-reviewer` |
| Sentinel | `alf-security-assessor` |

Optional specialist roles include API design, dependency, domain modelling and observability assessors where present. Treat `alf-problem-analyst` as an optional alias of the canonical `alf-problem-analyzer`, not as an invented file.

Capture the agent definition source path, repository revision and content hash for every invocation. Support global, local and configurable agent installation paths. The Claude and Codex adapters may render the same agent role through different invocation mechanisms without changing its purpose.

## 5. Roles

### 5.1 Problem Interpreter

The initial Claude agent receives the Product Owner's problem. It explores intent, stakeholders, measurable outcomes, current behaviour, assumptions, failure cases, constraints, non-goals, existing systems and integration boundaries. It must ask useful clarifying questions rather than prematurely inventing a solution. For technical work, ask about language, frameworks, architecture, data, infrastructure, deployment and operational constraints as applicable.

It produces a versioned **Problem Analysis** containing the problem statement, context, constraints, choices, open questions, preliminary decomposition, options and rationale. It can revisit the analysis as implementation yields new information.

### 5.2 Problem Counterpart

Where Codex is available, its highest configured reasoning profile independently critiques the Problem Analysis. It identifies omissions, weak assumptions, questionable decomposition, unnecessary complexity and alternative approaches. Claude and Codex exchange structured challenges and revisions. Material product ambiguities are escalated to the Product Owner rather than settled through fabricated agreement.

### 5.3 Story Analyst

The Analyst turns the agreed understanding into a roadmap of small, vertically sliced user stories describing valuable outcomes. Prefer one to three acceptance criteria per story, treating this as a guideline consistent with the old index-card model, not an arbitrary validation constraint. Avoid replacing user stories with implementation checklists.

If enabled, a Codex Analyst independently reviews boundaries, gaps, dependencies and value. The roadmap remains revisable. Present it to the Product Owner for initial approval by default.

### 5.4 Verifier

The Verifier selects a story and decomposes its acceptance criteria into ordered, observable behaviours. A Codex counterpart challenges gaps or ambiguous expectations. For the first behaviour, the Verifier writes an acceptance test and runs it against an identified baseline. An unrelated infrastructure error, invalid test or syntax failure does not count as the required red state.

The Verifier records test identifier, command, environment, revision, exit code, expected failure explanation and output. A Codex Verifier validates the proposed test where available. The Verifier then issues an implementation request and retains ownership of that acceptance contract.

### 5.5 Builder

The Builder makes the smallest production change that passes the verified failing acceptance test. Use TDD inside the implementation, including smaller red-green-refactor cycles, and keep changes focused. The Builder may not silently delete, skip, weaken or rewrite the approved acceptance test. Changes to it require the Verifier's authorisation.

When enabled, a Codex Builder counterpart reviews implementation, maintainability, design, correctness and unnecessary complexity. The Builder integrates feedback, runs relevant tests and produces a traceable changeset.

### 5.6 QA

QA reacts independently to each relevant changeset and assesses test design, meaningful assertions, test isolation, flakiness, coverage of behaviour, regression risks and maintainability. It returns version-bound findings, not arbitrary coverage demands. QA can challenge the Builder and require remediation of blocking defects.

### 5.7 Sentinel

Sentinel reacts independently to each changeset and examines security vulnerabilities, secrets, permissions, dependency risks, authentication, authorisation, injection and unsafe tool execution. Combine LLM assessment with configured deterministic scanners. Findings include severity, concrete evidence, affected code and remediation. High and critical unresolved findings block by default. Do not claim Sentinel can prove absence of vulnerabilities.

## 6. Workflow

### A. Discovery

1. The Product Owner opens a project and submits a problem to Claude's Problem Interpreter.
2. The Interpreter asks questions, captures decisions and prepares Problem Analysis v1.
3. An optional Codex counterpart critiques the same version independently.
4. Claude and Codex exchange evidence-based challenges and revisions.
5. When a product decision is missing, Relay asks the Product Owner and suspends dependent work.
6. Accept an explicitly versioned analysis once blocking disputes are resolved.

### B. Story planning

1. The Analyst proposes small valuable stories and acceptance criteria.
2. An optional Codex Analyst reviews decomposition and missing cases.
3. Negotiate revisions, record decisions and publish the story roadmap.
4. Obtain initial Product Owner approval by default.
5. Release the first eligible story.

### C. Behaviour and red test

1. The Verifier decomposes the story into testable behaviours.
2. An optional Codex Verifier reviews the behaviours.
3. The Verifier writes the first acceptance test.
4. Execute the test against the recorded baseline.
5. Validate that the failure matches the intended unmet behaviour.
6. An optional Codex Verifier reviews the acceptance test and red evidence.
7. Release the behaviour to the Builder only when the red state is valid.

### D. Build and review

1. The Builder implements minimum production code and uses TDD.
2. Run the acceptance test and relevant focused tests.
3. An optional Codex Builder independently reviews the implementation.
4. Submit an immutable Git changeset for parallel QA and Sentinel review.
5. Route blocking findings back to the Builder.
6. Re-run affected reviews whenever a new changeset makes older approvals stale.
7. Verify green acceptance and regression checks against the exact integrated revision.
8. The Verifier reports behaviour completion to the Analyst.
9. Repeat for remaining behaviours.

### E. Story approval

1. The Analyst verifies that the behaviours demonstrate the intended story outcome.
2. Relay produces a story review with implemented behaviours, changes, tests, risks, decisions and costs.
3. Pause by default and request Product Owner approval.
4. Permit `approve`, `request_changes`, `reject`, `pause` and `cancel`.
5. Release the next story only after approval or where explicit autonomous policy permits it.

### F. Feature completion

Run configured feature-wide checks, capture evidence and present the final implementation summary, accepted stories, open limitations and costs. Do not equate completed agent work with demonstrated product value.

## 7. Consensus protocol

Use immutable, versioned proposals and structured responses. The four dispositions are `ACCEPT`, `ACCEPT_WITH_RESERVATIONS`, `CHALLENGE`, and `REQUEST_INFORMATION`.

Each challenge includes a precise claim, evidence, affected artefact version, proposed resolution and whether it is blocking. Resolve findings individually rather than resending entire documents blindly.

Consensus requires acceptance of the **same artefact version**, resolution of all blocking findings and no outstanding required Product Owner decisions. Agreement between models is not evidence of correctness.

Configure a default maximum of four negotiation rounds. Reaching the cap triggers escalation, additional specialist review or explicit non-blocking decision policy, **not automatic agreement**. Product decisions belong to the Product Owner. Security policy determines whether disputed critical risks block work.

## 8. Model profiles, routing and calibration

Use logical aliases such as `advanced_reasoning`, `intermediate_review`, `coding`, and `lightweight`. Support user aliases such as `fable`, `astra`, `opus`, and `sonnet`, but never assume the aliases correspond to supported provider model IDs. Resolve them through validated provider-specific configuration.

| Stage | Claude profile | Optional Codex profile |
| --- | --- | --- |
| Problem discovery and decomposition | Advanced reasoning | Advanced reasoning |
| Story analysis | Intermediate or advanced | Intermediate or advanced |
| Behaviour discovery | Intermediate | Intermediate |
| Acceptance-test design | Intermediate | Intermediate |
| Implementation | Coding | Coding |
| Code review | Intermediate | Intermediate |
| QA | Intermediate | Optional review |
| Sentinel | Intermediate or advanced | Optional review |

Allow overrides per project, role, task and invocation. Profiles should expose model identifier, reasoning effort if supported, timeout, context limits, invocation budget and cost policy.

Escalate reasoning capabilities for hard problems, repeated failures, persistent disagreements or significant security decisions. Evaluate routing policies against benchmark tasks with tracked evidence, quality findings, interventions, latency and cost. Do not optimise only for cheap tokens or self-reported confidence.

## 9. Provider capability and fallback policies

Support Claude-only and Claude-plus-Codex configurations, and permit Codex-only configurations where roles and interfaces are explicitly supported. Cross-provider review must be independently toggled by phase.

For each counterpart role, configure one of:

- `required`: block if the counterpart is unavailable.
- `best_effort`: record unavailability and use an authorised alternative, such as a separate Claude review session.
- `disabled`: never invoke the counterpart.

Do not silently downgrade a required review. Distinguish Codex intentionally disabled from Codex unexpectedly offline. Store credentials securely and keep API-backed and authenticated CLI execution separate where necessary.

## 10. Redis Streams messaging protocol

A versioned envelope should contain:

```json
{
  "schema_version": 1,
  "message_id": "uuid",
  "message_kind": "command|event|agent_response",
  "event_type": "behaviour.test_red_verified",
  "project_id": "project-id",
  "run_id": "run-id",
  "story_id": "story-id-or-null",
  "behaviour_id": "behaviour-id-or-null",
  "conversation_id": "conversation-id-or-null",
  "correlation_id": "uuid",
  "causation_id": "uuid-or-null",
  "sender": "relay/verifier/worker-id",
  "recipient": "role-or-worker-id",
  "timestamp": "2026-11-19T22:00:00Z",
  "attempt": 1,
  "idempotency_key": "stable-key",
  "artefact_ref": "artefact-id-or-null",
  "payload": {}
}
```

Use separate logical streams or stream families for routing and observation. Consumer groups distribute work across interchangeable eligible workers and **do not broadcast messages** to every peer. Implement explicit role-directed commands and independent domain-event subscriptions.

Messages reference durable artefacts rather than embedding large transcripts or entire source trees. Validate schema versions, payload size and sender permissions. Preserve correlation and causation across asynchronous hops.

## 11. Durability, delivery and recovery

Redis offers at-least-once delivery, so handlers must be idempotent. PostgreSQL is authoritative for workflow state, event ledger, approvals, artefact metadata, leases and execution results.

Use a transactional outbox so that committed state transitions and intended outgoing messages cannot diverge on crash. Acknowledge queue messages only once work results are durable. Recover pending messages through inspection and claiming.

Worker leases and fencing tokens prevent old workers from publishing authoritative results after losing ownership. On recovery, inspect durable outputs before invoking a provider again. Treat uncertain external calls explicitly, since they might have completed and incurred cost before the response was lost.

Provide dead-letter handling, retries with limits and operator visibility. Event replay rebuilds projections only. It must not call models, re-run tests, modify Git or repeat external effects.

## 12. Workflow state machines

Define domain-level finite-state machines with explicit preconditions and reasons for rejection. Suggested lifecycle names:

**Feature:** `discovering`, `analysis_review`, `planning`, `implementing`, `final_verification`, `completed`, `paused`, `failed`, `cancelled`.

**Story:** `proposed`, `ready`, `in_progress`, `verifying`, `awaiting_approval`, `accepted`, `rejected`, `blocked`.

**Behaviour:** `proposed`, `test_design`, `validating_red`, `implementing`, `review`, `integrated`, `completed`, `blocked`.

A story cannot complete while behaviours are incomplete. A behaviour cannot complete without valid red/green evidence, relevant regression checks and current required reviews. Approval must reference the precise story version and evidence snapshot.

## 13. Git, workspaces and integration

Each worker uses an isolated local checkout or Git worktree based on an identified revision. Different machines synchronise through an authorised Git remote or versioned artefact mechanism. Never assume shared filesystem access.

Every changeset references the base and resulting revision, files touched, story and behaviour. Use an integration owner/controller per project. Prefer short-lived workspaces, small patches and prompt integration to a trunk-based mainline.

Support optimistic concurrency and stale-base detection. Conflicting modifications create explicit reconciliation tasks. Do not allow the Builder to hold a global repository lock during a model invocation or human pause. Respect protected-branch and CI controls, without making GitHub pull requests mandatory.

## 14. Acceptance-test ownership and evidence

The Verifier owns the acceptance contract. The Builder may propose changes but cannot alter an approved test without authorisation. Store test command, test IDs, baseline and implementation commits, environment identity, exit code and relevant output for red and green states.

An unrelated failure cannot establish a valid red. A model's claim that a test passed cannot establish green. Prefer tests through meaningful system interfaces. Run focused tests quickly, then configured broader regression checks before integration.

## 15. Parallel QA and Sentinel

For each changeset, schedule QA and Sentinel concurrently when capacity permits. Bind findings and approvals to immutable revisions or patch hashes. Findings must have stable IDs, severity, evidence, affected files, recommended remediation and resolution status.

The Builder may challenge a finding with evidence but cannot unilaterally close its own disputed blocking finding. A new changeset may invalidate prior reviews. Recheck all required gates against the revision being integrated.

## 16. Product Owner questions and decisions

All agents route questions through a central human inbox. Each question should state why the answer matters, possible options and trade-offs, and what work is blocked. Group related questions when useful. Persist answers as durable project decisions and share them with relevant agents.

Changes in product direction can invalidate downstream artefacts. Record these dependencies and re-open only affected work. The Product Owner can amend analysis, reorder or split stories and request new behaviours.

## 17. Story approvals and autonomy

Default `approval_mode: per_story`. At the approval gate, pause dependent implementation and present results, decisions, risks, checks and costs.

Autonomous execution is an explicit opt-in and must support limits for cost, stories, time, retries and findings. It does not bypass mandatory security checks, unresolved business questions or explicit human-only decisions. Changes to autonomy mode must be auditable. Pausing or cancelling must have clearly defined effects on in-flight work.

## 18. Dashboard

Provide:

- **Roadmap:** Features, stories, behaviours, status, dependencies, acceptance criteria and implementation evidence.
- **Event ledger:** Append-only chronological and filterable events by run, story, behaviour, agent, provider and type.
- **Agent conversations:** Versioned proposals, critiques, replies, decisions and unresolved disagreements. Do not claim access to private model reasoning.
- **Token and cost views:** Usage by run, story, role, provider and model, distinguishing observed tokens, estimated prices and actual billing where available.
- **Optional live logs:** On-demand worker and agent tailing, searching and safe export, with secrets redacted.
- **Operations:** Worker registry, queue health, leases, errors, retry controls, pause/resume/cancel and blocked decisions.

Inspect the previous Relay repository to preserve its existing roadmap, ledger, token/cost and log-tail behaviour where practical. Do not invent compatibility details until its code is inspected.

## 19. Observability

Use OpenTelemetry across the control plane, transport, workers, providers, Git and test runner. Propagate trace/correlation context through messages. Track queue delays, execution time, retries, model errors, provider usage, negotiation rounds, human waiting, failures and stalled tasks. Avoid high-cardinality metric labels and confidential trace payloads.

## 20. Configuration example

The following is illustrative. Validate exact provider model identifiers against installed capabilities.

```yaml
relay:
  project: example-project
  approval_mode: per_story
  max_active_stories: 1

transport:
  kind: redis_streams
  url_env: RELAY_REDIS_URL

state:
  kind: postgresql
  url_env: RELAY_DATABASE_URL

providers:
  claude:
    enabled: true
    adapter: claude_code
  codex:
    enabled: auto
    adapter: codex

models:
  profiles:
    advanced_reasoning:
      claude: ${CLAUDE_ADVANCED_MODEL}
      codex: ${CODEX_ADVANCED_MODEL}
    intermediate_review:
      claude: ${CLAUDE_REVIEW_MODEL}
      codex: ${CODEX_REVIEW_MODEL}
    coding:
      claude: ${CLAUDE_CODING_MODEL}
      codex: ${CODEX_CODING_MODEL}

collaboration:
  default_policy: best_effort
  max_rounds: 4
  problem_analysis: required
  story_analysis: best_effort
  behaviour_analysis: best_effort
  test_review: best_effort
  code_review: best_effort

roles:
  interpreter: alf-problem-analyzer
  analyst: alf-user-story-writer
  verifier: alf-atdd-developer
  builder: alf-atdd-developer
  qa: alf-test-design-reviewer
  sentinel: alf-security-assessor

quality:
  qa_enabled: true
  sentinel_enabled: true
  block_on_severity: high

limits:
  max_concurrent_provider_calls: 4
  project_budget_gbp: 100

ui:
  dashboard_enabled: true
  live_logs_default: false
```

The application must support per-project/role/task profile overrides and layered configuration precedence. `required` cross-provider review must only be configured when the needed worker exists, or the system must clearly pause with an actionable error. Secrets must be external to tracked files.

## 21. Security

Treat repository files, prompts, messages, test outputs, external content and tools as potentially untrusted. Apply role-scoped permissions. Give analysts read access by default, Verifiers controlled test-write access, Builders controlled production-write access, and QA/Sentinel read-only review access.

Run untrusted code and test commands in sandboxed environments without unrestricted access to home directories, SSH keys or provider credentials. Validate and authenticate broker traffic, use scoped Redis/database accounts and TLS where relevant, and enforce explicit approvals for privileged actions. Maintain audit events for permission/configuration changes.

## 22. Cost management

Track input/output/cache usage when reported, invocation status, elapsed time, provider/model and pricing-table version. Separate exact observed token usage from estimated costs and real invoices. Record unknown values as unknown.

Budgets may apply per run, feature and story. Enforce invocation, token, cost, retry and negotiation limits. Budget exhaustion pauses scheduling and asks the Product Owner to increase the allowance, change routing or stop. Cost estimates must not be treated as precise hard guarantees.

## 23. Concurrency

Default to one active implementation story per project. Allow parallel independent QA/Sentinel checks, peer review and non-conflicting read-only analysis. Never run multiple Builders against the same mutable workspace. Advertise machine capacity and concurrency limits in worker registration. Discard stale results from superseded attempts.

## 24. Context management

Store versioned Problem Analyses, decisions, stories, behaviours, acceptance tests, evidence, review findings, commits and approvals as durable artefacts. Assemble the smallest sufficient context for each invocation and distinguish Product Owner decisions from model suggestions. Do not require provider-native session resumability for workflow recovery.

## 25. CLI

Proposed commands:

```text
relay init
relay up
relay worker start
relay doctor
relay feature start
relay status
relay roadmap
relay ledger
relay logs
relay costs
relay answer
relay approve
relay pause
relay resume
relay cancel
```

Support JSON output, interactive Product Owner questions and story approval without requiring the web UI. CLI commands must perform real workflow operations, not merely change presentation state.

## 26. Required failure scenarios

Test at least: broker disconnection, worker crash, provider timeout/rate limit, malformed model response, abandoned pending message, duplicate delivery, outbox crash window, expired lease returning a late result, Git conflict, QA/Sentinel rejection, red test invalidity, test passing locally but failing in CI, changed human decision, story rejection, provider becoming unavailable, budget exhaustion, negotiation deadlock and restart while awaiting approval. Every case requires an explicit recovery policy and visible ledger records.

## 27. Suggested code boundaries

```text
relay/
  domain/           # entities, policies, state machines
  application/      # use cases, workflow, scheduler
  transport/        # Redis streams, envelopes, delivery
  providers/        # Claude and Codex adapters
  agents/           # roles, definition loader, context assembly
  workers/          # worker runtime, leases, execution
  git/              # worktrees, revisions, integration
  testing/          # commands and executable evidence
  quality/          # QA, security, finding lifecycle
  persistence/      # ledger, outbox, projections
  observability/    # logs, traces, metrics
  api/              # external control plane API
  cli/              # command line
  dashboard/        # web UI
```

Keep domain logic independent of infrastructure and SDKs. Do not invent abstractions without concrete use cases.

## 28. Testing Relay itself

Develop Relay with TDD/ATDD. Use fast deterministic domain tests, provider/transport contract tests, fake Claude and Codex adapters, real Redis/PostgreSQL integration tests and end-to-end scenarios. Fake providers must script disagreement, delays, malformed output, crashes, retries and usage reports. Run genuine provider smoke tests separately because they may cost money and are non-deterministic.

## 29. End-to-end acceptance scenarios

### Scenario 1: Claude-only operation

Given only Claude is configured, when a Product Owner submits a problem, Relay can complete analysis, story design, verified red tests, implementation, QA, Sentinel and story approval without calling Codex.

### Scenario 2: Cross-provider challenge

Given both providers are available, when a Claude analysis contains a questionable assumption, Codex can challenge it and Relay records the exchange and resulting decision.

### Scenario 3: Human clarification

Given a blocking product ambiguity, Relay asks the Product Owner, suspends dependent work and persists the answer as shared context.

### Scenario 4: Verified red

Given a new behaviour, the Builder remains blocked until the Verifier demonstrates a test failing for the intended reason against a recorded baseline.

### Scenario 5: Green and refactor

Given a verified red test, the Builder supplies production changes, passes the test and relevant regression checks, and retains an unmodified or explicitly authorised acceptance contract.

### Scenario 6: Parallel reviews

Given an implementation changeset, QA and Sentinel independently review the same immutable revision and unresolved blocking findings prevent completion.

### Scenario 7: Disagreement

Given a blocking Codex challenge, Relay does not mark consensus until the challenge is resolved or explicitly escalated.

### Scenario 8: Story gate

Given all behaviours are verified, Relay pauses before the next story unless the Product Owner has explicitly enabled autonomous mode.

### Scenario 9: Bounded autonomy

Given bounded autonomous mode, Relay may continue between stories but still blocks on unresolved decisions, mandatory security findings and limits.

### Scenario 10: Remote-worker crash

Given a worker on another machine loses connectivity, Relay recovers the unacknowledged task and rejects stale results from the expired lease.

### Scenario 11: Duplicate command

Given Redis redelivers a command, an existing durable result prevents duplicate completion or side effects.

### Scenario 12: Usage transparency

Given multiple model invocations, the dashboard separates observed tokens, estimated costs and unavailable billing information by model, provider, role and story.

### Scenario 13: Live log tail

Given a worker is active, the Product Owner may enable a live, redacted stream of authorised log output.

### Scenario 14: Restart at human approval

Given Relay is awaiting approval, restarting all processes restores the same gate and evidence without re-executing implementation.

## 30. Incremental implementation milestones

1. **Foundation:** Domain, state machines, durable ledger, outbox, Redis transport, fake workers and CLI with crash recovery.
2. **Claude integration:** Claude adapter, existing `alf-` agent loading, Product Owner discovery conversation, model routing.
3. **Distributed Codex:** Codex adapter, remote-worker registration, peer negotiation, cross-machine demonstration.
4. **Stories and behaviours:** Roadmap, Analyst, Verifier, behaviour decomposition and executable red tests.
5. **Builder and Git:** Implementation cycles, controlled workspaces, review and integration.
6. **QA and Sentinel:** Concurrent checks, findings, remediation and quality gates.
7. **Approval and budgets:** Human gate, rejection, autonomy, cost controls.
8. **Dashboard:** Roadmap, event ledger, conversations, usage/cost, optional log tail.
9. **Resilience and hardening:** Recovery, leases, deduplication, cancellation, observability, security and fault injection.
10. **Packaging:** Installation, Docker Compose, distributed workers, examples, documentation and migration guidance.

Each milestone must provide a demonstrable vertical slice with executable acceptance tests. Do not implement the whole platform as one batch.

## 31. Required repository investigation

Before implementation, inspect the existing Relay repository if available and identify its language, architecture, dashboard, ledger, event data, usage accounting and log tail implementation. Keep useful components where practical.

Inspect `github.com/andrealaforgia/claude-code-agents` including agent definitions, skills, tooling and installation scripts. Verify Claude Code and Codex interfaces and installed versions before choosing integration details. Produce a concise architectural assessment and migration plan. If the prior Relay source is unavailable, state so rather than inventing its structure.

## 32. Definition of Done

Relay v1 is complete when the Product Owner can submit a problem to Claude, answer questions, see independently challenged analysis where Codex is available, approve a roadmap, and have stories implemented through true ATDD with an independently verified red state per behaviour. QA and Sentinel react to changes and enforce quality gates. Workers run across machines. The system recovers from failures with durable state, event history and no silent duplicate work. The dashboard exposes roadmap, agent collaboration, ledger, tokens, costs and optional logs. It pauses after every story by default. The same system remains functional with Claude alone.

## 33. Instruction to Claude Code

Implement this specification using outside-in ATDD and TDD. First inspect the existing code and agent repository. Identify gaps and propose the smallest viable architecture and an ordered vertical-slice roadmap. Discuss material Product Owner decisions rather than inventing them. Reuse canonical `alf-` agents and skills. Prefer explicit, testable code and official provider integrations over an unnecessary agent-framework dependency. Keep changes small, integrated, observable and recoverable. Test failures and concurrency, not only the happy path.

Do not equate model agreement with correctness, or code generation with delivery. Produce executable evidence for each implemented capability.

**The aim is an engineering collaboration system that strengthens shared understanding, feedback, testing and continuous improvement, not a software factory that moves tickets between artificial roles.**
