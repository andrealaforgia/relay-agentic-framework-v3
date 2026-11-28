# Relay

[![CI](https://github.com/andrealaforgia/relay-agentic-framework-v3/actions/workflows/ci.yml/badge.svg)](https://github.com/andrealaforgia/relay-agentic-framework-v3/actions/workflows/ci.yml)

Version 3 of a distributed, multi-provider agentic software development framework. Agents negotiate and perform work; deterministic code validates evidence and controls every workflow transition.

**Status: early, and usable for small things.** Relay has delivered one very small feature end to end with the real provider, driven by nothing but its command line: a problem stated by a person, a roadmap and a story approved at a terminal, and a change on the trunk that Relay itself observed to work. Nothing larger has been tried. `docs/USAGE.md` says how to use it, what it costs, and what it does not do yet.

What is there:

- **The gates.** A behaviour reaches the Builder only when Relay itself has launched the project's trunk in an isolated container and observed the outcome registered beforehand. A Builder's candidate counts only when Relay itself observes the expected outcome on it. Reviews, integration and a final check of the whole feature are Relay's to run.
- **The durable core.** State, an append-only ledger and a transactional outbox in PostgreSQL; at-least-once delivery over Redis Streams with one durable effect.
- **Processes.** One control plane and a worker for each role (Analyst, Verifier, Builder, QA, Sentinel), each a call of the Claude CLI on a canonical agent definition, confined, capped and recorded.
- **The Product Owner's part.** One inbox of everything waiting, decisions confirmed by a passphrase read from the terminal, `relay watch` and `relay costs`, and `relay session`: a conversation with Relay that can read and prepare, and decide nothing.

There is one provider, no web interface and no API.

## Documents

- `docs/USAGE.md`: how to use Relay, from nothing to a first feature.
- `docs/SPEC.md`: the specification.
- `docs/ARCHITECTURE.md`: assessment of the previous Relay, the agreed architecture and the slice order.
- `docs/DECISIONS.md`: Product Owner decisions.
- `docs/consensus/`: the recorded exchange between Claude and Codex (Astra).

## Running the checks

Needs Docker, `uv` and Git.

```sh
make check
```

This starts PostgreSQL and Redis from `compose.yaml` (bound to loopback, on ports 55432 and 56379), then runs lint, strict type checking and the tests. The tests use real PostgreSQL, Redis, Git and Docker, and pull `python:3.14-alpine` once. No model is called and nothing costs money.

GitHub Actions runs the same `make check` on every push to `main` and on every pull request (`.github/workflows/ci.yml`). No test may run longer than 180 seconds.
