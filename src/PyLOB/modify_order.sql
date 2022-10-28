
update trade_order 
set 
    price=:price,
    qty=(case when :qty<fulfilled then fulfilled else :qty end),
    event_dt=(case when :qty>qty then :timestamp else event_dt end)
where idNum=:idNum and cancel=0 and side=:side;
