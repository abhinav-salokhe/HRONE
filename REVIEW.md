# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Used `punch_in.replace(hour=…)` on the punch instant instead of shift start on the attendance date in IST; treated lateness as `minutes > 10` instead of strictly more than 10 minutes after `shift_start` (R2). | Punch at 09:40:00 should be 0 late; at 09:40:01 should be 10. Overnight shifts give nonsense late values. | Compute shift start from `record_date` + `shift_start` in IST; late only when elapsed seconds > 600, else floor minutes from shift start. |
| 2 | `compute_overtime` | No 30-minute minimum (R3); shift end built from `date_str` without overnight next-day end. | 5 minutes after shift end counted as overtime; night shift overtime uses wrong end time. | `shift_end_instant` adds one day for overnight shifts; return 0 unless overtime ≥ 30 minutes. |
| 3 | `health` | Always returned 200 without checking MongoDB. | `/health` stays green when DB is down; grader expects 503. | Run `admin.command('ping')` and return 503 on failure. |
| 4 | `create_employee` | Duplicate check was find-then-insert (race); no `shift_start != shift_end` validation; `created_at` returned as naive datetime, not epoch ms. | Concurrent creates can both succeed; equal shift times accepted; API schema rejects responses. | Unique index on `emp_code` + catch `DuplicateKeyError`; Pydantic validation; serialize `created_at` as epoch milliseconds. |
| 5 | `list_employees` | `skip = page * page_size` (off-by-one); `total` ignored department filter; no sort by `emp_code`; epoch ms missing. | Page 1 skips the first page of rows; `total` wrong when filtering; order non-deterministic. | `(page - 1) * page_size`, count with same filter, sort ascending, map `created_at` to ms. |
| 6 | `punch_in` | No 404 for unknown employee; attendance `date` from `ts.date()` (UTC) not R1/IST; check-then-insert race; extra `id` field in response; timestamps not truncated to seconds. | Wrong calendar day near midnight; double punch-in under concurrency; response shape mismatch. | Load employee or 404; `attendance_date_from_punch`; unique `(emp_code, date)` index; `attendance_to_api` without `_id`/`id`; truncate seconds. |
| 7 | `list_attendance` | Loaded entire collection into memory; sort missing `emp_code` tie-break; no `date_from > date_to` check; returned Mongo `id` and raw datetimes. | Slow or OOM on large data; unstable ordering; 422 case missing; contract violations. | Indexed query with sort/skip/limit; validate date range; full API serialization. |

Also note anything you looked at and decided was **not** a defect, and why.

- **`compute_work_hours` rounding:** The starter used Python `round()`, which is banker's rounding on some builds. The spec requires half-up to two decimals (R4), so this was replaced with explicit decimal half-up rather than treated as correct.
- **Default `shift_start` / `shift_end` on create:** Matching the OpenAPI defaults is intentional; not a bug.
- **Missing endpoints (punch-out, PATCH, analytics, explain):** Incomplete scope in the starter, not logic bugs in the provided handlers.
