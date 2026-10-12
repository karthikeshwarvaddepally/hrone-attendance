# Design decisions

1. **Indexes.** Unique `employee_code` and `employee_date` indexes enforce employee identity and one attendance record per day. `date_employee` supports date-range analytics and attendance ordering. `status_date_employee` supports status filtering; `employee_punch` locates the latest eligible punch-in. `department_code` supports employee listing. `joined_on` and `department_joined` support headcount queries. I rejected an index on `half_day`: its low selectivity would add write cost without helping these queries.

2. **Punch-in race.** Both requests may read the employee and compute the same attendance date. They then insert directly. MongoDB's unique employee/date index allows one insert. That request returns 201; the other gets DuplicateKeyError, translated to 409. No separate existence check establishes uniqueness.

3. **Ties.** `$rank` assigns competition ranks using total late minutes only. Filtering rank after ranking retains every tied employee at the cutoff. Final ordering uses employee code to make ties deterministic.

4. **Headcount.** Department summary starts with eligible employees, then looks up attendance. Employees with no matching logs still contribute one to headcount and zero to totals.

5. **At 100x scale.** I would benchmark these pipelines and introduce incrementally maintained daily department summaries, with corrections updating affected summaries, before optimizing the trend's repeated indexed lookups.
