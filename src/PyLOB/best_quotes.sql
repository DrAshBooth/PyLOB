
select 
    order_id, trader as counterparty, 
    price, 
    available
from best_quotes 
where 
    instrument=:instrument and 
    matching=:side and
    (price is null or price * matching_order<=:price * matching_order)
-- order by statement should be appended
