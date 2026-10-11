# DECISIONS.md

1. **Indexes.** I added a unique index on `employees.emp_code` (create + punch-in safety) and a unique compound on `attendance_logs (emp_code, date)` so duplicate punch-ins fail at insert. Listing uses `(date desc, emp_code asc)`; analytics filters use `(date, status)` and employee `(department, emp_code)` / `joined_on`. I skipped a text index on `name` because no endpoint searches by name.

2. **Punch-in race.** Both requests hit `insert_one` with the same key. MongoDB’s unique index makes one insert succeed (201) and the other raise `DuplicateKeyError`, which we map to 409. There is no prior `find_one` gate.

3. **Ties.** Late leaderboard uses competition ranking: equal `total_late_minutes` share a rank and the next rank is skipped. After ranking, every row with `rank <= limit` is returned, so a tie at the cutoff can yield more than `limit` rows. Sort order is total late minutes descending, then `emp_code` ascending.

4. **Headcount.** Department summary loads all employees with `joined_on` on or before the last day of the month, groups them by department in Python, and sets `headcount` to that list size. Attendance aggregates only add present/late metrics; employees with zero logs still appear via the employee side.

5. **One thing you would change** for 100× data: pre-aggregate monthly and daily department metrics in separate collections (updated on punch/correct) so analytics reads O(departments) instead of scanning ~100k log rows every request.
