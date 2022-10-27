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
    back_compatibility = False
    
    def __init__(self, tick_size = 0.0001, **kw):
        self.tape = deque(maxlen=None) # Index [0] is most recent trade
        self.bids = OrderTree()
        self.asks = OrderTree()
        self.lastTick = None
        self.lastBidPrice = None
        self.lastAskPrice = None
        self.lastTimestamp = 0
        self.tickSize = tick_size
        self.time = 0
        self.nextQuoteID = 0

        
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

        if quote.get('price'):
            if quote['side'] == 'ask':
                self.lastAskPrice = quote['price']
            elif quote['side'] == 'bid':
                self.lastBidPrice = quote['price']
            else:
                sys.exit("processOrder() given neither 'ask' nor 'bid'")
            quote['price'] = self.clipPrice(quote['price'])
        else:
            quote['price'] = None

        if orderType=='market':
            trades = self.processMarketOrder(quote, verbose)
        elif orderType=='limit':
            trades, orderInBook = self.processLimitOrder(quote, verbose)
        else:
            sys.exit("processOrder() given neither 'market' nor 'limit'")
        return trades, orderInBook


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
            if not self.back_compatibility and qtyToTrade > 0:
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
            if not self.back_compatibility and qtyToTrade > 0:
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
                orderUpdate['type'] = order.order_type
                if not self.back_compatibility and \
                        self.betterPrice(side, order.price, orderUpdate['price']):
                    self.bids.removeOrderById(idNum)
                    ret = self.processOrder(orderUpdate, True, verbose)
                else:
                    self.bids.updateOrder(orderUpdate)
        elif side == 'ask':
            if not self.back_compatibility and \
                    self.asks.orderExists(orderUpdate['idNum']):
                order = self.asks.getOrder(idNum)
                orderUpdate['type'] = order.order_type
                if self.betterPrice(side, order.price, orderUpdate['price']):
                    self.asks.removeOrderById(idNum)
                    ret = self.processOrder(orderUpdate, True, verbose)
                else:
                    self.asks.updateOrder(orderUpdate)
        else:
            sys.exit('modifyOrder() given neither bid nor ask')
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
        fileStr = StringIO()
        fileStr.write("------ Bids -------\n")
        if self.bids != None and len(self.bids) > 0:
            for k, v in self.bids.priceTree.items(reverse=True):
                fileStr.write('%s' % v)
        fileStr.write("\n------ Asks -------\n")
        if self.asks != None and len(self.asks) > 0:
            for k, v in self.asks.priceTree.items():
                fileStr.write('%s' % v)
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
        fileStr.write("\n")
        return fileStr.getvalue()

