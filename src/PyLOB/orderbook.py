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
    def __init__(self, db=None, tick_size = 0.0001):
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
        orderType = quote['type']
        orderInBook = None
        if fromData:
            self.time = quote['timestamp']
        else:
            self.updateTime()
            quote['timestamp'] = self.time
            self.nextQuoteID += 1
            quote['idNum'] = self.nextQuoteID
        
        if quote['qty'] <= 0:
            sys.exit('processOrder() given order of qty <= 0')

        if quote['side'] not in ('ask', 'bid'):
            sys.exit("processOrder() given neither 'ask' nor 'bid'")

        if quote.get('price'):
            quote['price'] = self.clipPrice(quote['price'])
        else:
            quote['price'] = None

        if self.db and not quote.get('no_db'):
            crsr = self.db.cursor()
            crsr.execute('begin transaction')
            crsr.execute(self.insert_order, quote)
            quote['order_id'] = crsr.lastrowid
            self.processMatchesDB(quote, crsr, verbose)
            crsr.execute('commit')

        if orderType=='market':
            trades = self.processMarketOrder(quote, verbose)
        elif orderType=='limit':
            trades, orderInBook = self.processLimitOrder(quote, verbose)
        else:
            sys.exit("processOrder() given neither 'market' nor 'limit'")
        return trades, orderInBook


    def processMatchesDB(self, quote, crsr, verbose):
        quote.update(
            lastprice=self.lastPrice,
        )
        qtyToExec = quote['qty']
#        print(quote, self.matches)
        matches = crsr.execute(self.matches, quote).fetchall()
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

    def processOrderList(self, side, orderlist, 
                         qtyStillToTrade, quote, verbose):
        '''
        Takes an order list (stack of orders at one price) and 
        an incoming order and matches appropriate trades given 
        the orders quantity.
        '''
        trades = []
        qtyToTrade = qtyStillToTrade
        while len(orderlist) > 0 and qtyToTrade > 0:
            headOrder = orderlist.getHeadOrder()
            tradedPrice = headOrder.price
            counterparty = headOrder.tid
            # Prevent self trading
            if counterparty == quote['tid']:
                continue
            if qtyToTrade < headOrder.qty:
                tradedQty = qtyToTrade
                # Amend book order
                newBookQty = headOrder.qty - qtyToTrade
                headOrder.updateQty(newBookQty, headOrder.timestamp)
                # Incoming done with
                qtyToTrade = 0
            elif qtyToTrade == headOrder.qty:
                tradedQty = qtyToTrade
                if side=='bid':
                    # Hit the bid
                    self.bids.removeOrderById(headOrder.idNum)
                else:
                    # Lift the ask
                    self.asks.removeOrderById(headOrder.idNum)
                # Incoming done with
                qtyToTrade = 0
            else:
                tradedQty = headOrder.qty
                if side=='bid':
                    # Hit the bid
                    self.bids.removeOrderById(headOrder.idNum)
                else:
                    # Lift the ask
                    self.asks.removeOrderById(headOrder.idNum)
                # We need to keep eating into volume at this price
                qtyToTrade -= tradedQty
            if verbose: print('>>> TRADE \nt=%s $%f n=%d p1=%d p2=%d' % 
                              (self.time, tradedPrice, tradedQty, 
                               counterparty, quote['tid']))
            
            self.lastPrice = tradedPrice
            transactionRecord = {'timestamp': self.time,
                                 'price': tradedPrice,
                                 'qty': tradedQty,
                                 'time': self.time}
            if side == 'bid':
                transactionRecord['party1'] = [counterparty, 
                                               'bid', 
                                               headOrder.idNum]
                transactionRecord['party2'] = [quote['tid'], 
                                               'ask',
                                               None]
            else:
                transactionRecord['party1'] = [counterparty, 
                                               'ask', 
                                               headOrder.idNum]
                transactionRecord['party2'] = [quote['tid'], 
                                               'bid',
                                               None]
            self.tape.append(transactionRecord)
            trades.append(transactionRecord)
        return qtyToTrade, trades
    
    def processMarketOrder(self, quote, verbose):
        trades = []
        qtyToTrade = quote['qty']
        side = quote['side']
        if side == 'bid':
            while qtyToTrade > 0 and self.asks: 
                bestPriceAsks = self.asks.minPriceList()
                qtyToTrade, newTrades = self.processOrderList('ask', 
                                                                 bestPriceAsks, 
                                                                 qtyToTrade, 
                                                                 quote, verbose)
                trades += newTrades
            # If volume remains, add to book
            if qtyToTrade > 0:
                quote['qty'] = qtyToTrade
                self.bids.insertOrder(quote)
                orderInBook = quote
        elif side == 'ask':
            while qtyToTrade > 0 and self.bids: 
                bestPriceBids = self.bids.maxPriceList()
                qtyToTrade, newTrades = self.processOrderList('bid', 
                                                                 bestPriceBids, 
                                                                 qtyToTrade, 
                                                                 quote, verbose)
                trades += newTrades
            # If volume remains, add to book
            if qtyToTrade > 0:
                quote['qty'] = qtyToTrade
                self.asks.insertOrder(quote)
                orderInBook = quote
        else:
            sys.exit('processMarketOrder() received neither "bid" nor "ask"')
        return trades
    
    def processLimitOrder(self, quote, verbose):
        orderInBook = None
        trades = []
        qtyToTrade = quote['qty']
        side = quote['side']
        price = quote['price']
        if side == 'bid':
            while (self.asks and 
                   price >= self.asks.minPrice() and 
                   qtyToTrade > 0):
                bestPriceAsks = self.asks.minPriceList()
                qtyToTrade, newTrades = self.processOrderList('ask', 
                                                              bestPriceAsks, 
                                                              qtyToTrade, 
                                                              quote, verbose)
                trades += newTrades
            # If volume remains, add to book
            if qtyToTrade > 0:
                quote['qty'] = qtyToTrade
                self.bids.insertOrder(quote)
                orderInBook = quote
        elif side == 'ask':
            while (self.bids and 
                   price <= self.bids.maxPrice() and 
                   qtyToTrade > 0):
                bestPriceBids = self.bids.maxPriceList()
                qtyToTrade, newTrades = self.processOrderList('bid', 
                                                              bestPriceBids, 
                                                              qtyToTrade, 
                                                              quote, verbose)
                trades += newTrades
            # If volume remains, add to book
            if qtyToTrade > 0:
                quote['qty'] = qtyToTrade
                self.asks.insertOrder(quote)
                orderInBook = quote
        else:
            sys.exit('processLimitOrder() given neither bid nor ask')
        return trades, orderInBook

    def cancelOrder(self, side, idNum, time = None):
        if time:
            self.time = time
        else:
            self.updateTime()
        if side == 'bid':
            if self.bids.orderExists(idNum):
                self.bids.removeOrderById(idNum)
        elif side == 'ask':
            if self.asks.orderExists(idNum):
                self.asks.removeOrderById(idNum)
        else:
            sys.exit('cancelOrder() given neither bid nor ask')
        if self.db:
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
        if self.db:
            crsr = self.db.cursor()
            row = crsr.execute(self.find_order, (idNum,)).fetchone()
            return row[0] if row else None
        if self.bids.orderExists(idNum):
            return 'bid'
        if self.asks.orderExists(idNum):
            return 'ask'
        return None

    def modifyOrder(self, idNum, orderUpdate, time=None, verbose=False):
        if time:
            self.time = time
        else:
            self.updateTime()
        side = orderUpdate['side']
        orderUpdate['idNum'] = idNum
        orderUpdate['timestamp'] = self.time

        ret = [], orderUpdate
        if side == 'bid':
            if self.bids.orderExists(orderUpdate['idNum']):
                order = self.bids.getOrder(idNum)
                if self.betterPrice(side, order.price, orderUpdate['price']):
                    self.bids.removeOrderById(idNum)
                    orderUpdate['type'] = order.order_type
                    orderUpdate['no_db'] = True
                    ret = self.processOrder(orderUpdate, True, verbose)
                else:
                    self.bids.updateOrder(orderUpdate)
        elif side == 'ask':
            if self.asks.orderExists(orderUpdate['idNum']):
                order = self.asks.getOrder(idNum)
                if self.betterPrice(side, order.price, orderUpdate['price']):
                    self.asks.removeOrderById(idNum)
                    orderUpdate['type'] = order.order_type
                    orderUpdate['no_db'] = True
                    ret = self.processOrder(orderUpdate, True, verbose)
                else:
                    self.asks.updateOrder(orderUpdate)
        else:
            sys.exit('modifyOrder() given neither bid nor ask')
        if self.db:
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
                if orderUpdate['price']:
                    orderUpdate['price'] = self.clipPrice(orderUpdate['price'])
                crsr.execute(self.modify_order, orderUpdate)
                if self.betterPrice(side, price, orderUpdate['price']):
                    ret = self.processOrder(orderUpdate, True, verbose)
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
    
    def tapeDump(self, fname, fmode, tmode):
            dumpfile = open(fname, fmode)
            for tapeitem in self.tape:
                dumpfile.write('%s, %s, %s\n' % (tapeitem['time'], 
                                                 tapeitem['price'], 
                                                 tapeitem['qty']))
            dumpfile.close()
            if tmode == 'wipe':
                    self.tape = []
        
    def __str__(self):
        if self.db:
            crsr = self.db.cursor()

        fileStr = StringIO()
        fileStr.write("------ Bids -------\n")
        if self.bids != None and len(self.bids) > 0:
            for k, v in self.bids.priceTree.items(reverse=True):
                fileStr.write('%s' % v)
            if self.db:
                for bid in crsr.execute(self.active_orders, dict(side='bid', direction=-1)):
                    idNum, qty, fulfilled, price, event_dt = bid
                    fileStr.write('%s-%s @ %s t=%s\n' % (qty, fulfilled, price, event_dt))
        fileStr.write("\n------ Asks -------\n")
        if self.asks != None and len(self.asks) > 0:
            for k, v in self.asks.priceTree.items():
                fileStr.write('%s' % v)
            if self.db:
                for ask in crsr.execute(self.active_orders, dict(side='ask', direction=1)):
                    idNum, qty, fulfilled, price, event_dt = ask
                    fileStr.write('%s-%s @ %s t=%s\n' % (qty, fulfilled, price, event_dt))
        fileStr.write("\n------ Trades ------\n")
        if self.tape != None and len(self.tape) > 0:
            num = 0
            for entry in self.tape:
                if num < 5 or True:
                    fileStr.write(str(entry['qty']) + " @ " + 
                                  str(entry['price']) + 
                                  " (" + str(entry['timestamp']) + ")\n")
                    num += 1
                else:
                    break
            if self.db:
                for trade in crsr.execute(self.select_trades):
                    fileStr.write(repr(trade)+'\n')
        fileStr.write("\n")
        return fileStr.getvalue()

