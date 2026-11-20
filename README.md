# Relay

Version 3 of a distributed, multi-provider agentic software development framework. Agents negotiate and perform work; deterministic code validates evidence and controls every workflow transition.

**Status: early. Relay cannot yet be used to build software.** Three slices exist, all without any model in the loop:

- The red gate: a behaviour reaches the Builder only when Relay itself has launched the project's trunk in an isolated container and observed the outcome registered beforehand.
- The green gate: a Builder's candidate counts only when Relay itself observes the expected outcome on it, in the same runtime as the red, and the candidate was built on the commit the red was observed on.
- The durable core: state, an append-only ledger and a transactional outbox in PostgreSQL; at-least-once delivery over Redis Streams with one durable effect.

There is no CLI, no control-plane API, no worker, no provider adapter and no dashboard yet. Usage instructions will be written when there is something to use.

## Documents

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
