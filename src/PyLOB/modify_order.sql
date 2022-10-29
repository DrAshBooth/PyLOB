
update trade_order 
set 
    price=case when order_type='market' -- don't switch null price
        then null 
        else case when :price is null 
            then price 
            else :price 
        end
    end,
    qty=(case when :qty<fulfilled then fulfilled else :qty end),
    event_dt=(case when :qty>qty then :timestamp else event_dt end)
where idNum=:idNum and cancel=0 and side=:side;
