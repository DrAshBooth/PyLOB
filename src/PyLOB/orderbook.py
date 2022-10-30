'''
Created on Mar 28, 2013

@author: Ash Booth, Alex Bodnaru
         
'''

import sys
import math
from io import StringIO
import os, inspect

class OrderBook(object):

    valid_types = ('market', 'limit')
    valid_sides = ('ask', 'bid')

    def __init__(self, db, tick_size = 0.0001):
        self.lastTick = None
        self.lastPrice = None
        self.lastTimestamp = 0
        self.tickSize = tick_size
        self.rounder = int(math.log10(1 / self.tickSize))
        self.time = 0
        self.nextQuoteID = 0
        self.db = db
        location = os.path.dirname(inspect.getabsfile(inspect.currentframe()))
        ext = '.sql'
        queries = [fname.rsplit('.', 1)[0] for fname in os.listdir(location) if fname.endswith(ext)]
        for query in queries:
            setattr(self, query, open(os.path.join(location, query + ext), 'r').read())
        self.best_quotes_order_asc = self.best_quotes_order.replace(':direction', 'asc')
        self.best_quotes_order_desc = self.best_quotes_order.replace(':direction', 'desc')
        self.best_quotes_order_map = dict(
            desc=self.best_quotes_order_desc,
            asc=self.best_quotes_order_asc
        )

        
    def clipPrice(self, price):
        """ Clips the price according to the ticksize """
        return round(price, self.rounder)
    
    def updateTime(self):
        self.time+=1
    
    def processOrder(self, quote, fromData, verbose):
        if fromData:
            self.time = quote['timestamp']
        else:
            self.updateTime()
            quote['timestamp'] = self.time
            self.nextQuoteID += 1
            quote['idNum'] = self.nextQuoteID
        
        if quote['qty'] <= 0:
            sys.exit('processOrder() given order of qty <= 0')

        if quote['type'] not in self.valid_types:
            sys.exit("processOrder() given neither 'market' nor 'limit'")
        if quote['side'] not in self.valid_sides:
            sys.exit("processOrder() given neither 'ask' nor 'bid'")

        if quote.get('price'):
            quote['price'] = self.clipPrice(quote['price'])
        else:
            quote['price'] = None

        crsr = self.db.cursor()
        crsr.execute('begin transaction')
        crsr.execute(self.insert_order, quote)
        quote['order_id'] = crsr.lastrowid
        ret = self.processMatchesDB(quote, crsr, verbose)
        crsr.execute('commit')

        return ret


    def processMatchesDB(self, quote, crsr, verbose):
        quote.update(
            lastprice=self.lastPrice,
        )
        qtyToExec = quote['qty']
        sql_matches = self.matches + self.best_quotes_order_asc
        matches = crsr.execute(sql_matches, quote).fetchall()
        trades = list()
        for match in matches:
            if qtyToExec <= 0:
                break
            order_id, counterparty, price, available = match
            bid_order = quote['order_id'] if quote['side'] == 'bid' else order_id
            ask_order = quote['order_id'] if quote['side'] == 'ask' else order_id
            qty = min(available, qtyToExec)
            qtyToExec -= qty
            trade = bid_order, ask_order, self.time, price, qty
            self.lastPrice = price
            trades.append(trade)
            if verbose: print('>>> TRADE \nt=%s $%f n=%d p1=%d p2=%d' % 
                              (self.time, price, qty,
                               counterparty, quote['tid']))
        crsr.executemany(self.insert_trade, trades)
        return trades, quote

    def cancelOrder(self, side, idNum, time = None):
        if time:
            self.time = time
        else:
            self.updateTime()
        
        crsr = self.db.cursor()
        crsr.execute('begin transaction')
        crsr.execute(self.cancel_order, (1, idNum, side))
        crsr.execute('commit')
    
    # return whether comparedPrice has better matching chance than price
    def betterPrice(self, side, price, comparedPrice):
        if price is None and comparedPrice is not None:
            return False
        if price is not None and comparedPrice is None:
            return True
        if side == 'bid':
            return (price < comparedPrice)
        elif side == 'ask':
            return (price > comparedPrice)
        else:
            sys.exit('betterPrice() given neither bid nor ask')
    
    def orderGetSide(self, idNum):
        crsr = self.db.cursor()
        row = crsr.execute(self.find_order, (idNum,)).fetchone()
        return row[0] if row else None

    def modifyOrder(self, idNum, orderUpdate, time=None, verbose=False):
        if time:
            self.time = time
        else:
            self.updateTime()
        side = orderUpdate['side']
        orderUpdate['idNum'] = idNum
        orderUpdate['timestamp'] = self.time

        ret = [], orderUpdate
        crsr = self.db.cursor()
        crsr.execute('begin transaction')
        row = crsr.execute(self.find_order, (idNum,)).fetchone()
        if row:
            side, instrument, price, qty, fulfilled, cancel, order_id, order_type = row
            orderUpdate.update(
                type=order_type,
                order_id=order_id,
                instrument=instrument,
            )
            if orderUpdate.get('price'):
                orderUpdate['price'] = self.clipPrice(orderUpdate['price'])
            crsr.execute(self.modify_order, orderUpdate)
            if self.betterPrice(side, price, orderUpdate['price']):
                ret = self.processMatchesDB(orderUpdate, crsr, verbose)
        crsr.execute('commit')
        return ret
    
    def getVolumeAtPrice(self, instrument, side, price):
        # how much can i buy / sell for this price 
        # should include all matching prices.
        price = self.clipPrice(price)
        crsr = self.db.cursor()
        params = dict(instrument=instrument, side=side, price=price)
        first = crsr.execute(self.volume_at_price, params).fetchone()
        if first:
            volume, = first
            return volume
        return None

    def getPrice(self, instrument, side, direction='asc'):
        crsr = self.db.cursor()
        sql_active_orders = \
            self.active_orders + self.best_quotes_order_map[direction] + self.limit1
        first = crsr.execute(sql_active_orders, dict(instrument=instrument, side=side)).fetchone()
        if first:
            idNum, qty, fulfilled, price, event_dt, instrument = first
            return price
        return None

    def getBestBid(self, instrument):
        return self.getPrice(instrument, 'bid')
    def getWorstBid(self, instrument):
        return self.getPrice(instrument, 'bid', direction='desc')
    def getBestAsk(self, instrument):
        return self.getPrice(instrument, 'ask')
    def getWorstAsk(self, instrument):
        return self.getPrice(instrument, 'ask', direction='desc')
    
    def print(self, instrument):
        crsr = self.db.cursor()
        sql_active_orders = \
            self.active_orders + self.best_quotes_order_asc

        fileStr = StringIO()
        fileStr.write("------ Bids -------\n")
        for bid in crsr.execute(sql_active_orders, dict(instrument=instrument, side='bid')):
            idNum, qty, fulfilled, price, event_dt, instrument = bid
            fileStr.write('%s)%s-%s @ %s t=%s\n' % (idNum, qty, fulfilled, price, event_dt))
        fileStr.write("\n------ Asks -------\n")
        for ask in crsr.execute(sql_active_orders, dict(instrument=instrument, side='ask')):
            idNum, qty, fulfilled, price, event_dt, instrument = ask
            fileStr.write('%s)%s-%s @ %s t=%s\n' % (idNum, qty, fulfilled, price, event_dt))
        fileStr.write("\n------ Trades ------\n")
        for trade in crsr.execute(self.select_trades, dict(instrument=instrument)):
            fileStr.write(repr(trade)+'\n')
        fileStr.write("\n")
        
        fileStr.write('volume bid if i ask 98: '+str(self.getVolumeAtPrice(instrument, 'bid', 98))+"\n")
        fileStr.write('volume ask if i bid 101: '+str(self.getVolumeAtPrice(instrument, 'ask', 101))+"\n")
        fileStr.write('best bid: '+str(self.getBestBid(instrument))+"\n")
        fileStr.write('worst bid: '+str(self.getWorstBid(instrument))+"\n")
        fileStr.write('best ask: '+str(self.getBestAsk(instrument))+"\n")
        fileStr.write('worst ask: '+str(self.getWorstAsk(instrument))+"\n")
        
        value = fileStr.getvalue()
        print(value)
        return value

