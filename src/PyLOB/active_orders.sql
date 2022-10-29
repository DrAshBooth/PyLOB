
select idNum, qty, fulfilled, price, event_dt 
from best_quotes
where side=:side
-- order should be inserted
