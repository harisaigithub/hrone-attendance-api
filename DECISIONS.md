# DECISIONS.md

1. **Indexes.** On attendance logs: a unique `(emp_code, date)` index gives one record per day and serves punch-in, corrections and monthly lookups; `(date desc, emp_code)` serves the default list order and month scans; `(status, date desc, emp_code)` serves status filters; `(emp_code, punch_in desc)` finds the open record at punch-out. On employees: unique `emp_code`, and `(department, joined_on)` for the summary and trend. I rejected an index on `late_minutes`, because the month's date range already narrows the scan enough.

2. **Punch-in race.** Both requests call `insert_one` with no check first. The unique `(emp_code, date)` index lets MongoDB accept one of them; the other raises a duplicate-key error and I return 409.

3. **Ties.** I rank with `$rank`, then keep rank <= `limit`. Tied employees share a rank, so both are returned and the list can be longer than `limit`. Cutting one tied person would be arbitrary.

4. **Headcount.** The summary starts from `employees` and joins logs with `$lookup`. An employee with no logs still produces a row, so they are counted.

5. **100x data.** I would store monthly totals per employee, updated at each punch or correction, so analytics stop scanning raw logs.
