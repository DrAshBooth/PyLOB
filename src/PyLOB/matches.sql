
select 
    case when :side='bid' then :order_id else order_id end as bid_order, 
    case when :side='ask' then :order_id else order_id end as ask_order, 
    trader as counterparty, 
    coalesce(price, :price, :lastprice) as price, 
    qty
from best_quotes 
where 
    instrument=:instrument and 
    matching=:side and
    (allow_self_matching=1 or trader<>:tid) and 
    coalesce(:price, price, :lastprice) is not null and 
    (:price is null or price is null or price * matching_order<=:price * matching_order)
order by 
    side asc,
    case when price is null then 0 else 1 end asc, -- null prices are always best
    matching_order * coalesce(price, 0) asc,
    event_dt asc
