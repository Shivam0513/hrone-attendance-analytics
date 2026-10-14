# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` to prevent duplicate employee codes. A unique compound index on `attendance_logs (emp_code, date)` prevents duplicate daily attendance records and supports employee/date lookups. A compound index on `attendance_logs (date descending, emp_code ascending)` supports date-based attendance queries and sorting. I would avoid adding an index on `status` alone because its usefulness depends on query selectivity and the actual workload.

2. **Punch-in race.** Both requests may initially check or attempt to insert the same employee/date record. The unique compound index is the final safeguard: MongoDB accepts one insert and rejects the other with a duplicate-key error. The application catches that error and returns HTTP 409 for the losing request.

3. **Ties.** The leaderboard uses competition ranking. Employees tied on late minutes receive the same rank. Filtering by rank rather than simply limiting documents means all employees tied at the cutoff are returned, so the result can contain more entries than `limit`.

4. **Headcount.** The department summary starts from employees eligible by the month-end date, then looks up their attendance records. Preserving employees with empty lookup results ensures employees with zero logs are still included.

5. **Scaling.** For 100 times more data, I would measure query plans and latency, add indexes only for demonstrated bottlenecks, and review aggregation efficiency. I would also benchmark realistic workloads before changing the data model. 