
select 
    order_id, trader as counterparty, 
    coalesce(price, :price, :lastprice) as price, 
    available
from best_quotes 
where 
    instrument=:instrument and 
    matching=:side and
    (allow_self_matching=1 or trader<>:tid) and 
    coalesce(:price, price, :lastprice) is not null and 
    (:price is null or price is null or price * matching_order<=:price * matching_order)
-- order by statement should be appended
