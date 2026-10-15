1. **Indexes.** The API creates unique indexes on `employees.emp_code` and the composite `attendance_logs` key `(emp_code, date)`, plus supporting indexes on `department`, `emp_code`, and the attendance date. These keep employee lookups, daily attendance uniqueness, and monthly or department analytics efficient. I did not keep a broad compound index on every analytics field because it would add write overhead without fixing the most common path.

2. **Punch-in race.** Two identical punch-in requests hit the same employee/date simultaneously. The endpoint attempts an insert on the unique `(emp_code, date)` index. One request succeeds; the other raises a duplicate-key conflict and returns the contract’s 409 response. That preserves a single daily attendance record instead of allowing partial state.

3. **Ties.** The leaderboard uses competition ranking: employees with the same `total_late_minutes` receive the same rank, and the next rank skips as needed. If the `limit` cuts a tied group, the endpoint includes all employees tied at that rank rather than truncating only part of the group.

4. **Headcount.** Department summaries aggregate employee rows first, then use `joined_on <= month_end` to include only employees present in the reporting period. Employees who join mid-month still count in headcount, while zero-log employees remain in the department rollup with zero attendance values.

5. **If it had to handle 100x more data**, I would move the heavy analytics to MongoDB views or materialized summaries, keep indexes narrower to actual query patterns, and add paging and time-bounded aggregation windows for the largest monthly reports.
