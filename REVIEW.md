# REVIEW.md

| # | Where (function) | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|

| 1 | `compute_late_minutes` | Floors to whole minutes first, then tests `> 10`. A punch-in at 09:40:01 floors to 10 and is treated as on time. Violates R2 ("strictly more than 10:00"). | Shift 09:30, punch in 09:40:01: expected 10, got 0. | Compare raw seconds (`> 600`), then floor to minutes since shift start. |
| 2 | `compute_late_minutes` | Builds shift start with `punch_in.replace(hour, minute)` in whatever timezone the datetime has (naive server-local), not IST. Overnight shifts also anchor to the wrong day. | Server in UTC: an on-time IST punch looks hours late. Overnight 22:00 shift punched at 21:55 IST gets nonsense. | Anchor shift start to the attendance date in IST (`shift_start_dt`). |
| 3 | `compute_work_hours` | Uses float `round()` (not half-up) and does not truncate to whole seconds first. | 8 h 0 min 18 s = 8.005 h: expected 8.01, float round gives 8.0. | Integer seconds, `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Missing the 30-minute minimum (R3). Shift end is built from `date_str`, so overnight shifts end on the wrong day. Mixes an IST-aware `end` with a possibly naive `punch_out`. | 10 minutes after shift end returns 10, expected 0. Overnight punch-out gives a huge negative or 0. Naive punch-out raises `TypeError`. | `shift_end_dt` (next day when overnight), threshold of 30, all datetimes aware. |
| 5 | `create_employee` | Check-then-insert race and no unique index. | Parallel creates with the same `emp_code` return two 201s. | Unique index on `emp_code`; catch `DuplicateKeyError` and return 409. |
| 6 | `create_employee` / `EmployeeIn` | `created_at` is naive local `datetime.now()` and returned as a datetime, not epoch ms. No validation of `emp_code` format, email, shift format, `joined_on`, or `shift_start == shift_end`. | Send `shift_start: "9.30"`; it is stored and breaks later calculations with a 500. | UTC `created_at`, serialised as epoch ms. Pydantic patterns and validators. |
| 7 | `list_employees` | `skip = page * page_size` skips the first page. `total` counts the whole collection, ignoring the filter. No sort. No bounds on `page` / `page_size`. | Page 1 returns what should be page 2. `total` ignores `department`. `page=0` or `page_size=100000` accepted. | `(page - 1) * page_size`, filtered count, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 8 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises `TypeError`. | Punch in with `emp_code: "NOPE"` gives a 500, expected 404. | `get_employee()` raises 404. |
| 9 | `punch_in` | `punched_at` is not validated as epoch ms (seconds, floats, strings slip through). It is converted to naive local time and microseconds are not truncated. | `punched_at: 1783312500` (seconds) is accepted. Stored value keeps sub-second noise. | Strict int, range-checked (ms), converted to aware UTC, truncated to whole seconds. |
| 10 | `punch_in` | `date` is the server-local calendar date, not the IST date, and there is no overnight handling (R1). | A 23:30 IST punch on a UTC server lands on the next date. A 02:00 IST punch on a 22:00-06:00 shift should belong to the previous day. | `attendance_date()` implements R1. |
| 11 | `punch_in` | Check-then-insert race. | Two simultaneous identical requests both return 201. | Unique `(emp_code, date)` index, catch `DuplicateKeyError`, return 409. |
| 12 | `punch_in` | `status` is any string, so `ABSENT` or `"XYZ"` is accepted. | `status: "XYZ"` returns 201. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 13 | `punch_in` response | Leaks an internal `id` (the Mongo `_id` as a string) and returns datetimes instead of epoch ms. | Response contains `id` and ISO datetimes. | `ser_att()` returns the contract fields only, with epoch ms. |
| 14 | `list_attendance` | Loads the entire collection into memory and sorts in Python. No `emp_code` tiebreak. No indexes exist. | Very slow at 100k records. Unstable order for same-date records. | Sort, skip and limit in MongoDB with compound indexes created at startup. |
| 15 | `list_attendance` | `date_from > date_to` and malformed dates are not rejected. `status` is unvalidated. `page=0` produces a negative slice. Leaks `id`; legacy records missing `history` / `half_day` are returned as-is. | `date_from=2026-08-01&date_to=2026-07-01` returns 200 with an empty list instead of 422. | `build_list_spec` validates inputs. `Status` enum, bounded paging, serializer applies defaults. |
| 16 | `/health` | Always returns `ok` even if MongoDB is down. | Stop MongoDB: still 200. | Ping the DB; return 503 on failure. |
| 17 | `MongoClient` | Not `tz_aware`, so PyMongo returns naive datetimes that get mixed with aware ones. | `TypeError: can't subtract offset-naive and offset-aware datetimes`. | `tz_aware=True` plus an `aware()` helper for legacy values. |
| 18 | Startup | No indexes are created anywhere, so every query path is a collection scan. | Explain shows `COLLSCAN`. | `ensure_indexes()` runs at startup via lifespan, idempotently. |

## Examined and decided not defects

- **`load_dotenv()`**: it does not override existing environment variables by default, so real env vars win, as the assignment requires.
- **`date` and `joined_on` stored as strings**: intentional per DATA_MODEL.md. ISO strings sort and range-compare correctly.
- **Reusing the helper functions**: the structure (separate late / hours / overtime helpers) is fine. The logic inside them was the problem, so I rewrote them instead of reusing them as-is.
- **Negative late minutes for early punch-ins**: the starter's `minutes > 10` check already maps these to 0, so that case was not a bug.
