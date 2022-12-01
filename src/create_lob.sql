
--PRAGMA foreign_keys=ON;

begin transaction;

create table trader (
    tid integer not null primary key,
    name text,
    currency text,
    -- commission calculations in the currency of the instrument
    commission_per_unit real default(0),
    commission_min real default(0),
    commission_max real default(0),
	commission_max_percnt real default(0),
    allow_self_matching integer default(0),
    foreign key(currency) references instrument(symbol)
) -- strict
;

create table instrument (
    symbol text unique,
    currency text,
    lastprice real
) -- strict
;

insert into instrument (symbol, currency) values ('USD', null);

create table trader_balance (
    trader integer, -- trader
    instrument text,
    amount real default(0),
    primary key(trader, instrument),
    foreign key(trader) references trader(tid),
    foreign key(instrument) references instrument(symbol)
) -- strict
;

create table side (
    side text primary key,
    matching text,
    matching_order integer
) -- strict
;

insert into side (side, matching, matching_order) values 
    ('bid', 'ask', -1),
    ('ask', 'bid', 1)
;

CREATE TRIGGER SIDE_DELETE_LOCK
    BEFORE DELETE ON side
BEGIN
    select RAISE (ABORT, 'side may not be changed');
END;

CREATE TRIGGER SIDE_INSERT_LOCK
    BEFORE INSERT ON side
BEGIN
    select RAISE (ABORT, 'side may not be changed');
END;

CREATE TRIGGER SIDE_UPDATE_LOCK
    BEFORE UPDATE ON side
BEGIN
    select RAISE (ABORT, 'side may not be changed');
END;

create table trade_order (
    order_id integer primary key,
    instrument text,
    order_type text, -- currently limit / market
    side text, -- bid/ask
-------
    --event_dt text default(datetime('now')), -- supplied
    event_dt integer, -- for testing only
-------
    qty integer not null, -- required
    fulfilled integer default(0), -- accumulator of trades by :side _order
    price real, -- trigger price, null for market
    idNum integer, -- external supplied, optional
    trader integer, -- trader
    active integer default(1),
    cancel integer default(0),
    commission real, -- calculate on fulfill or on cancel, else nullify
    foreign key(side) references side(side),
    foreign key(trader) references trader(tid),
    foreign key(instrument) references instrument(symbol)
) -- STRICT
;

create index order_priority on trade_order (side, instrument, price asc, event_dt asc);
create index order_idnum on trade_order (idNum asc);

create view best_quotes as
select 
    order_id, idNum, side.side as side, price, 
    qty, fulfilled, qty-fulfilled as available, 
    event_dt, instrument, trade_order.trader, allow_self_matching, 
    matching, matching_order
from trade_order
inner join trader on trader.tid=trade_order.trader
inner join side on side.side=trade_order.side
where active=1 and cancel=0 and qty>fulfilled
-- please note, that the order by clause may need to be added to your select
order by 
    side asc,
    case when price is null then 0 else 1 end asc, -- null prices are always best
    matching_order * coalesce(price, 0) asc,
    event_dt asc
;

create view order_detail as
select 
    order_id, instrument, instrument.currency, order_type, side, event_dt, 
    qty, fulfilled, price, idNum, trader, active, cancel, 
    -- the commission in case the entire order would be executed
    case when active=1 
    then commission 
    else min(commission_max, max(commission_min, commission_per_unit * qty)) 
    end as commission, 
    instrument.currency as commission_currency
from trade_order
inner join instrument on trade_order.instrument=instrument.symbol
inner join trader on trader.tid=trade_order.trader
;

CREATE TRIGGER order_lock
    BEFORE UPDATE OF order_type, order_id, idNum, instrument, side ON trade_order
BEGIN
    select RAISE (ABORT, 'fields: order_type, order_id, idNum, instrument, side may not be changed');
END;

CREATE TRIGGER order_insert
    AFTER INSERT ON trade_order
BEGIN
    -- ensure trader has balance for instrument and instrument.currency
    insert into trader_balance (trader, instrument, amount) 
    select new.trader, new.instrument, 0 
    on conflict do nothing;
    insert into trader_balance (trader, instrument, amount) 
    select new.trader, instrument.currency, 0 
    from instrument 
    where instrument.symbol=new.instrument 
    on conflict do nothing;
END;

CREATE TRIGGER order_commission
    AFTER UPDATE OF cancel, fulfilled ON trade_order
BEGIN
    update trade_order
    set commission=
        case when new.fulfilled>0
        then min(
            trader.commission_max, max(trader.commission_min, trader.commission_per_unit * new.fulfilled))
        else null
        end
    from (
        select commission_max, commission_min, commission_per_unit
        from trader
        where trader.tid=new.trader
    ) as trader
    where trade_order.order_id=new.order_id and old.cancel=0;
    update trader_balance
    set amount=amount - (
        case when new.fulfilled>0
        then 
            min(
                trader.commission_max, 
                max(trader.commission_min, trader.commission_per_unit * new.fulfilled)
            ) - (
            case when old.fulfilled>0
            then
            min(
                trader.commission_max, 
                max(trader.commission_min, trader.commission_per_unit * old.fulfilled)
            )
            else 0
            end
            )
        else 0
        end
        )
    from (
        select commission_max, commission_min, commission_per_unit, instrument.currency
        from trader
        inner join instrument on instrument.symbol=new.instrument
        where trader.tid=new.trader
    ) as trader
    where trader_balance.trader=new.trader and trader_balance.instrument=trader.currency and old.cancel=0;
END;

create table trade (
    trade_id integer primary key, 
    bid_order integer,
    ask_order integer,
    event_dt text default(datetime('now')), -- supplied
    price real, -- order price or better
    qty integer, -- accumulates to fulfill of orders
    idNum integer, -- external supplied, optional
    foreign key(bid_order) references trade_order(order_id),
    foreign key(ask_order) references trade_order(order_id)
) -- STRICT
;

CREATE TRIGGER trade_lock
    BEFORE UPDATE OF qty, price, bid_order, ask_order ON trade
BEGIN
    select RAISE (ABORT, 'fields: qty, price, bid_order, ask_order may not be changed');
END;

CREATE TRIGGER trade_insert
    AFTER INSERT ON trade
BEGIN
    -- increase order fulfillment
    update trade_order 
    set fulfilled=fulfilled + new.qty
    where idNum in (new.bid_order, new.ask_order);
    -- bid balance increases by qty instrument
    update trader_balance
    set amount=trader_balance.amount + bid_order.amount 
    from (
        select trader, instrument, new.qty as amount
        from trade_order 
        where trade_order.order_id=new.bid_order
    ) as bid_order
    where 
        trader_balance.trader=bid_order.trader and
        trader_balance.instrument=bid_order.instrument;
    -- ask balance increases by qty * price instrument.currency
    update trader_balance
    set amount=trader_balance.amount + ask_order.amount 
    from (
        select trader, instrument.currency as instrument, new.qty * new.price as amount
        from trade_order 
        inner join instrument on instrument.symbol=trade_order.instrument
        where trade_order.order_id=new.ask_order
    ) as ask_order
    where 
        trader_balance.trader=ask_order.trader and
        trader_balance.instrument=ask_order.instrument;
    -- ask balance decreases by qty instrument
    update trader_balance
    set amount=trader_balance.amount - ask_order.amount 
    from (
        select trader, instrument, new.qty as amount
        from trade_order 
        where trade_order.order_id=new.ask_order
    ) as ask_order
    where 
        trader_balance.trader=ask_order.trader and
        trader_balance.instrument=ask_order.instrument;
    -- bid balance decreases by qty * price instrument.currency
    update trader_balance
    set amount=trader_balance.amount - bid_order.amount 
    from (
        select trader, instrument.currency as instrument, new.qty * new.price as amount
        from trade_order 
        inner join instrument on instrument.symbol=trade_order.instrument
        where trade_order.order_id=new.bid_order
    ) as bid_order
    where 
        trader_balance.trader=bid_order.trader and
        trader_balance.instrument=bid_order.instrument;
END;

CREATE TRIGGER trade_delete
    AFTER DELETE ON trade
BEGIN
    -- decrease order fulfillment
    update trade_order 
    set fulfilled=fulfilled - new.qty
    where order_id in (new.bid_order, new.ask_order);
    -- bid balance decreases by qty instrument
    update trader_balance
    set amount=trader_balance.amount - bid_order.qty 
    from (
        select trader, instrument, new.qty 
        from trade_order 
        where trade_order.order_id=new.bid_order
    ) as bid_order
    where 
        trader_balance.trader=bid_order.trader and
        trader_balance.instrument=bid_order.instrument;
    -- ask balance decreases by qty * price instrument.currency
    update trader_balance
    set amount=trader_balance.amount - ask_order.amount 
    from (
        select trader, instrument.currency as instrument, new.qty * new.price as amount
        from trade_order 
        inner join instrument on instrument.symbol=trade_order.instrument
        where trade_order.order_id=new.ask_order
    ) as ask_order
    where 
        trader_balance.trader=ask_order.trader and
        trader_balance.instrument=ask_order.instrument;
    -- ask balance increases by qty instrument
    update trader_balance
    set amount=trader_balance.amount + ask_order.qty 
    from (
        select trader, instrument, new.qty 
        from trade_order 
        where trade_order.order_id=new.ask_order
    ) as bid_order
    where 
        trader_balance.trader=ask_order.trader and
        trader_balance.instrument=ask_order.instrument;
    -- bid balance increases by qty * price instrument.currency
    update trader_balance
    set amount=trader_balance.amount + bid_order.amount 
    from (
        select trader, instrument.currency as instrument, new.qty * new.price as amount
        from trade_order 
        inner join instrument on instrument.symbol=trade_order.instrument
        where trade_order.order_id=new.bid_order
    ) as bid_order
    where 
        trader_balance.trader=bid_order.trader and
        trader_balance.instrument=bid_order.instrument;
END;

create view trade_detail as
select 
    'trade', trade.qty, trade.price, bidorder.instrument,
    case when bidorder.price is null or askorder.price is null or bidorder.price >= askorder.price 
    then '👍' else '👎'
    end as matches,
    'bid' as bid, bidorder.trader, bidorder.idNum, bidorder.qty, bidorder.price, bidorder.fulfilled, 
    'ask' as ask, askorder.trader, askorder.idNum, askorder.qty, askorder.price, askorder.fulfilled 
from trade
inner join trade_order as bidorder on bidorder.order_id=trade.bid_order 
inner join trade_order as askorder on askorder.order_id=trade.ask_order 
;

create table event (
    reqId integer,
    handler text, -- method on lob that will handle. it will handle using the args
    callback text, -- method to invoke on trigger
    unique(reqId) on conflict replace
);

create table event_arg (
    reqId integer,
    arg text,
    val text,
    convertor text,
    unique(reqId, arg) on conflict replace,
    foreign key(reqId) references event(reqId) on delete cascade
);

commit;
