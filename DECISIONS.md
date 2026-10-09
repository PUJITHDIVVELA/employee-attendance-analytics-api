# DECISIONS.md

1. **Indexes.**
   - **`employees` Collection**:
     - `[("emp_code", ASCENDING)]` (Unique): Ensures customer employee codes are globally unique and enforces atomic duplicate creation prevention (HTTP 409).
     - `[("department", ASCENDING)]`: Accelerates employee listing by department and aggregation joins/lookups.
   - **`attendance_logs` Collection**:
     - `[("emp_code", ASCENDING), ("date", ASCENDING)]` (Unique): Enforces the core business rule of at most one attendance record per employee per attendance date, preventing double punch-ins under concurrency.
     - `[("date", DESCENDING), ("emp_code", ASCENDING)]`: Serves the mandatory sorting order of `GET /attendance`.
     - `[("emp_code", ASCENDING), ("date", DESCENDING)]`: Optimizes employee monthly analytics and lookup operations.
     - `[("status", ASCENDING), ("date", ASCENDING)]`: Speeds up status-filtered queries and late leaderboard aggregations.
   - *Considered & Rejected*: A compound index on `("history.at", ASCENDING)` was rejected because audit history is stored as an embedded document array and queried embedded within single attendance records, making an embedded array index redundant.

2. **Punch-in race.**
   - When two identical punch-in requests for the same employee arrive at the exact same instant, both requests convert the input timestamp into an IST attendance date.
   - Both build an `attendance_logs` document with `(emp_code, date)`.
   - Rather than executing a vulnerable read-before-write check (`find_one`), both issue an `insert_one` call to MongoDB.
   - MongoDB's unique index on `(emp_code, date)` guarantees that only one write succeeds. The winning request receives a `201 Created` status code.
   - The concurrent duplicate request triggers a `pymongo.errors.DuplicateKeyError`, which is caught by the API exception handler and translated into an HTTP `409 Conflict` response.

3. **Ties.**
   - For the late-comers leaderboard (`GET /analytics/leaderboard/late`), ranking follows standard competition ranking (1, 2, 2, 4).
   - If two employees share identical total `late_minutes`, they are assigned the same `rank`. The next employee's rank skips accordingly.
   - The query parameter `limit` acts as a rank threshold rather than a strict row count cut-off: all employees with `rank <= limit` are returned. Thus, if a tie occurs at or before the limit, the returned list may contain more items than `limit`.
   - Output rows are deterministically ordered by `total_late_minutes DESC`, then `emp_code ASC`.

4. **Headcount.**
   - Department summary headcount includes all active employees whose `joined_on` date is on or before the end of the target month (R9), regardless of whether they have attendance logs in `attendance_logs`.
   - This is implemented in MongoDB using a `$lookup` pipeline starting from the `employees` collection (filtered by `joined_on <= last_day_of_month`), performing a left outer join against `attendance_logs` with `$unwind` setting `preserveNullAndEmptyArrays: True`.
   - Employees with zero attendance logs are preserved with zero counts and still contribute 1 to the department `headcount`.

5. **One thing you would change for 100x data.**
   - At 100x data scale (e.g. 10 million attendance records), real-time aggregation queries (such as department summary and daily trends using `$lookup` and `$densify`) would experience higher latency.
   - I would introduce incremental pre-aggregation / nightly summary collection materialization (e.g., maintaining daily department summary rollups and caching monthly stats), combined with read-replicas for analytical endpoints to isolate reporting queries from live punch transaction processing.
