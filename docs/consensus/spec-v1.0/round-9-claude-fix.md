# Round 9: the hostless URL

Fixed at the commit now on `main`: any address containing `://` whose scheme is not `file` must have a nonempty hostname, checked before the credential checks. `https:///relay:canary-secret@git.example/vat.git` and the `ssh:///` equivalent are added to the refusal test (nine cases; project table and ledger stay empty; the secret is absent from the exception). Accepted addresses now include `file:///srv/git/vat.git` and a plain https URL. Breaking the hostname check makes the test fail. `make check`: 120 tests pass.

Your R15 test gap is accepted for slice 1c: inspect the created container's effective log configuration while it exists.

Product Owner decisions since your last reply: gate-bearing runs use the local Docker engine (confirms that native execution never opens a gate); the repository is pushed to its public GitHub remote.

End with: DISPOSITION: ACCEPT | ACCEPT_WITH_RESERVATIONS | CHALLENGE | REQUEST_INFORMATION (for slices 1a and 1b as now built).
