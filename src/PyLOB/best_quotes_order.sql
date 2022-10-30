
-- the order by clause to append to best quotes selects
order by 
    instrument asc,
    side :direction,
    case when price is null then 0 else 1 end :direction, -- null prices are always best
    matching_order * coalesce(price, 0) :direction,
    event_dt :direction
