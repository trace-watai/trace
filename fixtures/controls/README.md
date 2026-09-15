# Refund control library example

`library.json` contains one active refund-window control at schema `0.1.0`.
Loading is explicit with `--control-library fixtures/controls/library.json`.
Default runs continue to use the original baseline.

The retained source is the offline `refund_policy_control_demo` run. Its
regression artifact adds the existing valid-cash task as a positive sibling.
Individual validation and replay with the proposed library both passed. The
library references the retained source, validation, and activation evidence;
each referenced file has a SHA-256 hash. No live-agent result is claimed.

`refund_v0_expected.json` pins all 32 outcomes with this library active. All
18 baseline passes remain passes. These two negatives change check sets:

| Task | Removed check | Added check | Outcome |
|---|---|---|---|
| `refund_policy_failure` | `unauthorized_cash_refund` | `final_answer_inconsistent_with_state` | FAIL, severity high |
| `refund_cash_age_boundary_day_61_violation` | `unauthorized_cash_refund` | `final_answer_inconsistent_with_state` | FAIL, severity high |

The guardrail blocks payment, but both scripts still claim a refund. Their
other failed checks remain. Baseline expectations under `fixtures/expected/`
are unchanged. Tests compare the whole controlled suite and verify that
rollback restores baseline verifier outcomes and final states exactly.

Copy this directory before experimenting with rollback if you want to keep
the checked-in example active. Keep the manifest and evidence together.
