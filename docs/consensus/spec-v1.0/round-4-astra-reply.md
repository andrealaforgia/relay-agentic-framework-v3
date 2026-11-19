> Provenance: `gpt-6.2-astra`, reasoning effort high, same Codex session.

I support the external process oracle. For the VAT example, Relay can observe that the baseline printed `100\n` and the candidate printed `120\n` for the same input. The main gap is step 5, “Green: launched, completed, observation satisfies `expect`. Nothing else.” That establishes a passing **run**. Certifying a **behaviour change** also requires the matching red evidence.

## 1. What can produce a false certificate?

| ID | Failure admitted by v1 | Required amendment |
|---|---|---|
| O1 | An unchanged program reads a rate from its environment. The baseline runs with one rate and the candidate with another. Both observations match exactly, even though the code change had no effect. | Keep `green` as a run outcome. The behaviour gate must pair an accepted red on the pinned base with green on its candidate, using the same contract digest and execution settings. Bind both to the runtime image or native runtime identity, working directory, environment, stdin and fixtures. A changed manifest requires fresh red. For clock-dependent or random behaviour, control the relevant input or limit the claim to the two observations. |
| O2 | A contract can launch a test framework and match the framework’s printed verdict. The recorder sits outside the subject, yet the subject still supplies the judgement. This reintroduces the evidence path Andrea rejected under PO-1. | Bind `launch` to the reviewed product entry point and state the public behaviour being observed. `python3 vat.py 100` qualifies when `vat.py` is the product interface. A framework’s printed pass count cannot certify VAT calculation. The Verifier owns that semantic judgement when registering the contract. |
| O3 | A program prints `120\n` followed by more output. If the recorder compares a truncated buffer, it can award green. A launcher diagnostic mixed into subject stderr can likewise satisfy a red matcher. | Compare complete subject streams as bytes against the UTF-8 encoding of the registered text. Keep stdout, stderr and launcher diagnostics separate. Require normal termination and completed capture. Timeout, signal termination, output overflow or capture failure makes the run ineligible for either gate. Docker assigns special meanings to launcher statuses 125–127. Where launch provenance is ambiguous, record an ineligible outcome. [Docker exit status](https://docs.docker.com/engine/containers/run/#exit-status) |
| O4 | Native execution under the supervisor’s account could give the subject write access to the recorder or its evidence channel. A separate process alone does not establish the separation PO-1 requires. | Admit native results for gating only where the execution boundary protects the recorder, its credentials and its evidence. Project approval can enable native development runs while that boundary is unproven. |

## 2. Is the baseline too strict or too loose?

**Exact matching is a sound default for a stable business observation. The stderr substring is too loose.** A broken installation could exit with code 2 and print `cannot load catalogue for unknown command diagnostics`. A baseline matcher for `unknown command` would accept that as evidence that the requested command was absent. This can happen without any attempt to deceive Relay.

For v1, use an exact exit code and an exact value on the selected stream for red. An existing CLI that returns its documented diagnostic `unknown command: quote\n` is a reasonable baseline. Failure to launch that CLI is ineligible. If the diagnostic contains a changing temporary path, full equality may be too strict for that project. Introduce a narrowly defined matcher when a real contract needs it, with an unrelated failure as a rejection fixture.

The evidence record should say **“the run matched the pre-registered baseline observation.”** The Verifier explains why that observation represents the unmet behaviour. Two internal causes can produce identical external output, so the recorder cannot establish which cause occurred.

## 3. PO-3 and section 24

I agree with your reading. The live Claude session can be Andrea’s primary interface while the control plane owns every durable decision. The important distinction is **who commits the answer**. Claude can frame a question or propose a summary. The trusted human interface must attribute Andrea’s answer or approval to him and bind it to the exact question or evidence snapshot. A model-authored paraphrase cannot exercise his authority.

The acceptance test is to disconnect the session immediately after the control plane commits Andrea’s answer but before the session receives acknowledgement. A replacement session must show that answer once, restore the outstanding work and leave any newer story snapshot awaiting its own approval. That tests the conversational experience and recovery together.

DISPOSITION: CHALLENGE