
select idNum, qty, fulfilled, price, event_dt, instrument
from best_quotes
where side=:side
-- order by statement should be appended
