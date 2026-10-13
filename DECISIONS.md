# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` on logs: punch-in race safety, PATCH, monthly summary. `(date desc, emp_code)`: list sort order and month scans. `(status, date desc, emp_code)`: status-filtered lists. `(emp_code, punch_in desc)`: punch-out lookup. On employees: unique `emp_code`, `(department, joined_on)`. Rejected: an index on `late_minutes`; scans are already bounded by the month's date range.
2. **Punch-in race.** Both requests build the record and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one; the other raises `DuplicateKeyError`, which I return as 409. No read-then-write.
3. **Ties.** Ranking uses `$setWindowFields` with `$rank`, then keeps `rank <= limit`. A tie at the cutoff shares one rank, so both rows are returned, and the response may exceed `limit`.
4. **Headcount.** The pipeline starts on `employees` (joined on or before month end) and `$lookup`s logs. Employees with zero logs still produce a document, so they are counted.
5. **100x data.** Pre-aggregate monthly totals per employee (updated on punch/correction) so analytics stop scanning raw logs.
