# Slice 3a-ii, step 4: the build contract

Provenance: written by Claude (Fable 5.2) on 2026-11-21. **Astra has not reviewed this document.** Round 36 was the third and last design round under D-8; Astra said its four findings "can go directly into the build and its acceptance tests" with no further design round. The contract is design v3 (`round-36-claude-3a-ii-step-4-design-v3.md`) with these four amendments.

1. **S3-17.1, the identity of a managed trunk.** One project per repository identity and full ref. A local path and a `file://` URL to the same place are one identity: both are resolved to the absolute path. Relay cannot tell that two remote addresses name one repository, so the guarantee is for repositories enrolled under one address; D-9 says so. Test: path and file URL on `main` refused, on another branch accepted.

2. **S3-17.2, who wakes a successor.** The call that ends a batch as `discarded` because what it recorded has changed takes up the completion it has just owed, after its transaction commits, a bounded number of times. A completion deferred behind an outstanding push is woken by that push's settlement; one deferred by an unreadable repository waits for the next pass. Test: A's checks held on `H`; B integrates `H2` and settles, its callback finding nothing owed; A is released and, with nothing else called, the only summary names `H2`.

3. **S3-17.3, ending a request.** A completion request is ended in one transaction with whatever it still has: a running generation is ended with it and a wanted assignment retired; receipts are kept; nothing is owed after it. `overtaken` when the prerequisites are lost, `abandoned` when the feature is cancelled, including a cancel between admission and take-up, when there is no batch. Test: the stored request, batch and assignment after each, and after a restart; no summary.

4. **S3-17.4, a repeated take-up.** The assignment's durable disposition is looked up first: consumed, the generation bound to it is returned; retired unused, that is the answer; only a wanted one reads the repository. Test: both replays with the repository unreachable.
