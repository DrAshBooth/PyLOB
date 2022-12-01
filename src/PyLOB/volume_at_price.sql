
select 
    coalesce(sum(available), 0) as volume
from best_quotes 
where 
    instrument=:instrument and 
    side=:side and
    (price is null or price * matching_order<=:price * matching_order)
