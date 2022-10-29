'''
Created on Mar 28, 2013

@author: Ash Booth
         
'''

import sys
import math
from collections import deque
from io import StringIO
import os, inspect

from .ordertree import OrderTree

class OrderBook(object):

    valid_types = ('market', 'limit')
    valid_sides = ('ask', 'bid')

    def __init__(self, db, tick_size = 0.0001):
        self.tape = deque(maxlen=None) # Index [0] is most recent trade
        self.bids = OrderTree()
        self.asks = OrderTree()
        self.lastTick = None
        self.lastPrice = None
        self.lastTimestamp = 0
        self.tickSize = tick_size
        self.time = 0
        self.nextQuoteID = 0
        self.db = db
        location = os.path.dirname(inspect.getabsfile(inspect.currentframe()))
        ext = '.sql'
        queries = [fname.rsplit('.', 1)[0] for fname in os.listdir(location) if fname.endswith(ext)]
        for query in queries:
            setattr(self, query, open(os.path.join(location, query + ext), 'r').read())

        
    def clipPrice(self, price):
        """ Clips the price according to the ticksize """
        return round(price, int(math.log10(1 / self.tickSize)))
    
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
#        print(quote, self.matches)
        sql_matches = self.matches + self.best_quotes_order
        matches = crsr.execute(sql_matches, quote).fetchall()
        trades = list()
        for match in matches:
            if qtyToExec <= 0:
                break
            bid_order, ask_order, counterparty, price, qty = match
            qty = min(qty, qtyToExec)
            qtyToExec -= qty
            trade = bid_order, ask_order, self.time, price, qty
            self.lastPrice = price
            trades.append(trade)
            if verbose: print('>>> TRADE \nt=%s $%f n=%d p1=%d p2=%d' % 
                              (self.time, price, qty,
                               counterparty, quote['tid']))
            #crsr.execute(self.insert_trade, trade)
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
#?                ret = self.processOrder(orderUpdate, True, verbose)
                self.processMatchesDB(orderUpdate, crsr, verbose)
        crsr.execute('commit')
        return ret
    
    def getVolumeAtPrice(self, side, price):
        price = self.clipPrice(price)
        if side =='bid':
            vol = 0
            if self.bids.priceExists(price):
                vol = self.bids.getPrice(price).volume
            return vol
        elif side == 'ask':
            vol = 0
            if self.asks.priceExists(price):
                vol = self.asks.getPrice(price).volume
            return vol
        else:
            sys.exit('getVolumeAtPrice() given neither bid nor ask')
    
    def getBestBid(self):
        return self.bids.maxPrice()
    def getWorstBid(self):
        return self.bids.minPrice()
    def getBestAsk(self):
        return self.asks.minPrice()
    def getWorstAsk(self):
        return self.asks.maxPrice()
    
    def __str__(self):
        crsr = self.db.cursor()
        sql_active_orders = self.active_orders + self.best_quotes_order

        fileStr = StringIO()
        fileStr.write("------ Bids -------\n")
        for bid in crsr.execute(sql_active_orders, dict(side='bid', direction=-1)):
            idNum, qty, fulfilled, price, event_dt = bid
            fileStr.write('%s-%s @ %s t=%s\n' % (qty, fulfilled, price, event_dt))
        fileStr.write("\n------ Asks -------\n")
        for ask in crsr.execute(sql_active_orders, dict(side='ask', direction=1)):
            idNum, qty, fulfilled, price, event_dt = ask
            fileStr.write('%s-%s @ %s t=%s\n' % (qty, fulfilled, price, event_dt))
        fileStr.write("\n------ Trades ------\n")
        for trade in crsr.execute(self.select_trades):
            fileStr.write(repr(trade)+'\n')
        fileStr.write("\n")
        return fileStr.getvalue()

