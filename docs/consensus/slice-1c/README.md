# Consensus record: slice 1c (green paired with its red)

| Round | Astra's disposition | What it found |
|---|---|---|
| 1 | CHALLENGE, 3 blocking | A tag accepted as a candidate; runtime identity ignoring the container user; attempt refs shared between projects |
| 2 | CHALLENGE, 3 blocking | A green accepted against a superseded red; unresolved image identity; host-dependent execute permissions |
| 3 | CHALLENGE, 1 blocking | Runs finishing after the behaviour moved on left no record |
| 4 | CHALLENGE, 1 blocking | Permission-setting could follow a link out of the checkout |
| 5 | CHALLENGE, 1 blocking | Directories folded together by the filesystem |
| 6 | ACCEPT_WITH_RESERVATIONS | Hard-link check dropped by the rewrite (since restored) |

All reviewer replies are from `gpt-6.2-astra`. Astra ran the unit tests itself in each round; the integration results and the mutation checks are Claude's reports. Agreement between two models is not evidence of correctness.

Carried to slice 2: regression checks against behaviours already on trunk; review dispatch for a behaviour already in `review`, surviving a restart; immutable candidate refs. Carried to slice 5: ownership of attempts, so two concurrent runs cannot both launch.
