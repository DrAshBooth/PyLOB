'''
Created on Mar 28, 2013

@author: Ash Booth

Quotes must be a dict of form:

Limit Order:
quote = {'timestamp' : 008, 
         'side' : 'bid', 
         'qty' : 6, 
         'price' : 108.2, 
         'tid' : xxx}
         
Market Order:
quote = {'timestamp' : 008, 
         'side' : 'ask', 
         'qty' : 6, 
         'tid' : xxx}

'''


import sys
import math
from collections import deque
from order import Bid, Ask, Trade
from ordertree import OrderTree

class OrderBook(object):
    def __init__(self):
        self.trades = deque(maxlen=None) # Index [0] is most recent trade
        self.bids = OrderTree()
        self.asks = OrderTree()
        self.lastTick = None
        self.lastTimestamp = 0
        self.tickSize = 0.0001
        self.nextQuoteID = 0
        
    def clipPrice(self, price):
        return round(price,int(math.log10(1/self.tickSize)))
    
    def addOrder(self, quote):
        pass
    
    def processOrder(self, time, order_type, quote):
        if quote['qty'] <= 0:
            sys.exit('processLimitOrder() given order of qty <= 0')
        # Assign idNum
        quote['idNum'] = self.nextQuoteID
        self.nextQuoteID += 1
        # Clip the price according to the tick size
        quote['price'] = self.clipPrice(quote['price'])
        # Check order type
        if order_type=='market':
            self.processMarketOrder(time, quote)
        elif order_type=='limit':
            self.processLimitOrder(time, quote)
        else:
            sys.exit("processOrder() given neither 'market' nor 'limit'")
    
    def processMarketOrder(self, time, quote):
        qty_to_trade = quote['qty']
        side = quote['side']
        if side == 'bid':
            while qty_to_trade > 0 and self.asks:
                best_price_asks = self.asks.minPriceList()
                while len(best_price_asks)>0 and qty_to_trade > 0:
                    head_order = best_price_asks.getHeadOrder()
                    if qty_to_trade < head_order.qty:
                        # amend book order
                        new_book_qty = head_order.qty - qty_to_trade
                        head_order.updateQty(self, new_book_qty, head_order.timestamp)
                        # incoming done with
                        pass
                    elif qty_to_trade == head_order.qty:
                        pass
                    else:
                        # we need to keep eating into volume at this price
                        pass
        elif side == 'ask':
            while qty_to_trade > 0 and self.bids:
                pass
        else:
            sys.exit('processMarketOrder() received neither "bid" nor "ask"')
    
    def bidLimitClears(self, quote):
        trades = []
        qtyWanted = quote['qty']
        qtyBought = 0
        bestAskList = self.asks.getPrice(quote['price'])
        for order in bestAskList:
            if qtyWanted < order.qty:
                # modify book order and clear incoming order
                # add trades to list
                qtyBought = qtyWanted
                break
                pass
            elif qtyWanted == order.qty:
                # lift top of book and clear incoming trade
                # add trades to list
                qtyBought = qtyWanted
                break
            else: 
                # lift top of book
                # add that trade to list
                # continue loop
                pass
        # sort out clearing
    
    def processLimitOrder(self, time, quote):
        trades = []
        qty = quote['qty']
        o_type = quote['type']
        price = quote['price']
        if o_type == 'bid':
            while (len(self.asks) != 0) and (price > self.asks.min()):
                self.bidLimitClears(quote)
            # just stick it in the book
        elif o_type == 'ask':
            pass
        else:
            # we should never get here
            sys.exit('processLimitOrder() given neither bid nor ask')
        # Does it clear?
            # No
                # Add order to relevant book
            # Yes
        pass
    
    
    
    def cancelOrder(self, info):
        pass
    
    def modifyOrder(self, time, quote):
        pass
        
        
        

    def processBidAsk(self, tick):
        '''
        Generic method to process bid or ask.
        '''
        tree = self.asks
        if tick.isBid:
            tree = self.bids
        if tick.qty == 0:
            # Quantity is zero -> remove the entry
            tree.removeOrderById(tick.idNum)
        else:
            if tree.orderExists(tick.idNum):
                tree.updateOrder(tick)
            else:
                # New order
                tree.insertTick(tick)
                
    def bid(self, tick):
        columns = ['event', 'symbol', 'exchange', 'idNum', 'qty', 'price', 'timestamp']
        data = self.parseCsv(columns, tick)
        bid = Bid(data)
        if bid.timestamp > self.lastTimestamp:
            self.lastTimestamp = bid.timestamp
        self.lastTick = bid
        self.processBidAsk(bid)
        
    def ask(self, tick):
        columns = ['event', 'symbol', 'exchange', 'idNum', 'qty', 'price', 'timestamp']
        data = self.parseCsv(columns, tick)
        ask = Ask(data)
        if ask.timestamp > self.lastTimestamp:
            self.lastTimestamp = ask.timestamp
        self.lastTick = ask
        self.processBidAsk(ask)
        
    def trade(self, tick):
        columns = ['event', 'symbol', 'exchange', 'qty', 'price', 'timestamp']
        data = self.parseCsv(columns, tick)
        data['idNum'] = 0
        trade = Trade(data)
        if trade.timestamp > self.lastTimestamp:
            self.lastTimestamp = trade.timestamp
        self.lastTick = trade
        self.trades.appendleft(trade)
        
    def __str__(self):
        # Efficient string concat
        from cStringIO import StringIO
        file_str = StringIO()
        file_str.write("------ Bids -------\n")
        if self.bids != None and len(self.bids) > 0:
            for k, v in self.bids.priceTree.items(reverse=True):
                file_str.write('%s' % v)
        file_str.write("\n------ Asks -------\n")
        if self.asks != None and len(self.asks) > 0:
            for k, v in self.asks.priceTree.items():
                file_str.write('%s' % v)
        file_str.write("\n------ Trades ------\n")
        if self.trades != None and len(self.trades) > 0:
            num = 0
            for entry in self.trades:
                if num < 5:
                    file_str.write(str(entry.qty) + " @ " \
                        + str(entry.price / 10000) \
                        + " (" + str(entry.timestamp) + ")\n")
                    num += 1
                else:
                    break
        file_str.write("\n")
        return file_str.getvalue()