
select idNum, qty, fulfilled, price, event_dt 
from trade_order 
where side=:side and cancel=0 and qty>fulfilled 
order by (price * :direction) asc, event_dt asc;
