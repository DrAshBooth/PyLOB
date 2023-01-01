
update trade_order 
set cancel=:cancel
where idNum=:idNum and side=:side
