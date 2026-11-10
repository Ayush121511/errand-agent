# Bugs log

Every real bug hit while building goes here as soon as it is found: symptom, root cause, fix, and the
regression test that now guards it. The three most instructive ones are written up for submission.

| # | Found in | Symptom | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | Block 1, mock `get_booking_status` | After a timeout-after-success, the source-of-truth lookup returned `status: "confirmed"` instead of `status: "booked"`. An agent checking `status == "booked"` would wrongly decide nothing was booked and could retry into a duplicate. | `{"status": "booked", **booking_view}`: the booking view carries its own `status` field (confirmed/cancelled/...), and dict unpacking let it overwrite the lookup result. | Move the booking's own status to `booking_status` and set the lookup `status` last. | `tests/test_mocks.py::test_timeout_after_success_is_visible_only_via_status`, `::test_state_survives_restart` |
