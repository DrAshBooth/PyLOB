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
from ordertree import OrderTree
from random import shuffle

class OrderBook(object):
    def __init__(self):
        self.tape = deque(maxlen=None) # Index [0] is most recent trade
        self.bids = OrderTree()
        self.asks = OrderTree()
        self.lastTick = None
        self.lastTimestamp = 0
        self.tickSize = 0.0001
        self.time = 0
        self.nextQuoteID = 0
        
    def clipPrice(self, price):
        return round(price,int(math.log10(1/self.tickSize)))
    
    def updateTime(self):
        self.time+=1
    
    def processOrder(self, order_type, quote, verbose):
        idNum = None
        self.updateTime()
        if quote['qty'] <= 0:
            sys.exit('processLimitOrder() given order of qty <= 0')
        self.nextQuoteID += 1
        # Add timestamp
        quote['timestamp'] = self.time
        # Check order type
        if order_type=='market':
            trades = self.processMarketOrder(quote, verbose)
        elif order_type=='limit':
            # Clip the price according to the tick size
            quote['price'] = self.clipPrice(quote['price'])
            trades, idNum = self.processLimitOrder(quote, verbose)
        else:
            sys.exit("processOrder() given neither 'market' nor 'limit'")
        return trades, idNum
    
    def processOrderList(self, side, orderlist, qty_still_to_trade, quote, verbose):
        '''
        Takes an order list (stack of orders at one price) and 
        an incoming order and matches appropriate trades given the orders 
        quantity.
        '''
        trades = []
        qty_to_trade = qty_still_to_trade
        while len(orderlist)>0 and qty_to_trade > 0:
            head_order = orderlist.getHeadOrder()
            traded_price = head_order.price
            counterparty = head_order.tid
            if qty_to_trade < head_order.qty:
                traded_qty = qty_to_trade
                # amend book order
                new_book_qty = head_order.qty - qty_to_trade
                head_order.updateQty(new_book_qty, head_order.timestamp)
                # incoming done with
                qty_to_trade = 0
            elif qty_to_trade == head_order.qty:
                traded_qty = qty_to_trade
                if side=='bid':
                    # hit the bid
                    self.bids.removeOrderById(head_order.idNum)
                else:
                    # lift the ask
                    self.asks.removeOrderById(head_order.idNum)
                # incoming done with
                qty_to_trade = 0
            else:
                traded_qty = head_order.qty
                if side=='bid':
                    # hit the bid
                    self.bids.removeOrderById(head_order.idNum)
                else:
                    # lift the ask
                    self.asks.removeOrderById(head_order.idNum)
                # we need to keep eating into volume at this price
                qty_to_trade -= traded_qty
            if verbose: print('>>> TRADE \nt=%d $%f n=%d p1=%d p2=%d' % (self.time, traded_price, traded_qty, counterparty, quote['tid']))
            if side=='bid':
                transaction_record = {'timestamp': self.time,
                                   'price': traded_price,
                                   'qty': traded_qty, 
                                   'party1': [counterparty, 'bid'],
                                   'party2': [quote['tid'], 'ask'],
                                   'qty': traded_qty}
            else:
                transaction_record = {'timestamp': self.time,
                                   'price': traded_price,
                                   'qty': traded_qty, 
                                   'party1': [counterparty, 'ask'],
                                   'party2': [quote['tid'], 'bid'],
                                   'qty': traded_qty}
            self.tape.append(transaction_record)
            trades.append(transaction_record)
        return qty_to_trade, trades
    
    def processMarketOrder(self, quote, verbose):
        trades = []
        qty_to_trade = quote['qty']
        side = quote['side']
        if side == 'bid':
            while qty_to_trade > 0 and self.asks: 
                best_price_asks = self.asks.minPriceList()
                qty_to_trade, new_trades = self.processOrderList('ask', best_price_asks, qty_to_trade, quote, verbose)
                trades += new_trades
        elif side == 'ask':
            while qty_to_trade > 0 and self.bids: 
                best_price_bids = self.bids.maxPriceList()
                qty_to_trade, new_trades = self.processOrderList('bid', best_price_bids, qty_to_trade, quote, verbose)
                trades += new_trades
        else:
            sys.exit('processMarketOrder() received neither "bid" nor "ask"')
        return trades
    
    def processLimitOrder(self, quote, verbose):
        idNum = None
        trades = []
        qty_to_trade = quote['qty']
        side = quote['side']
        price = quote['price']
        if side == 'bid':
            while self.asks and price > self.asks.minPrice() and qty_to_trade > 0:
                best_price_asks = self.asks.minPriceList()
                qty_to_trade, new_trades = self.processOrderList('ask', best_price_asks, qty_to_trade, quote, verbose)
                trades += new_trades
            # If volume remains, add to book
            if qty_to_trade > 0:
                # Assign idNum
                idNum = self.nextQuoteID
                quote['idNum'] = idNum
                quote['qty'] = qty_to_trade
                self.bids.insertOrder(quote)
        elif side == 'ask':
            while self.bids and price < self.bids.maxPrice() and qty_to_trade > 0:
                best_price_bids = self.bids.maxPriceList()
                qty_to_trade, new_trades = self.processOrderList('bid', best_price_bids, qty_to_trade, quote, verbose)
                trades+=new_trades
            # If volume remains, add to book
            if qty_to_trade > 0:
                # Assign idNum
                idNum = self.nextQuoteID
                quote['idNum'] = idNum
                quote['qty'] = qty_to_trade
                self.asks.insertOrder(quote)
        else:
            sys.exit('processLimitOrder() given neither bid nor ask')
        return trades, idNum
    

    def cancelOrder(self, side, idNum):
        self.updateTime()
        if side=='bid':
            if self.bids.orderExists(idNum):
                self.asks.removeOrderById(idNum)
        elif side=='ask':
            if self.asks.orderExists(idNum):
                self.asks.removeOrderById(idNum)
        else:
            sys.exit('cancelOrder() given neither bid nor ask')
        
    
    def modifyOrder(self, side, idNum):
        self.updateTime()
    
    def getVolAtPrice(self):
        pass
    
    def getBestBid(self):
        pass
    
    def getBestAsk(self):
        pass
        
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
        if self.tape != None and len(self.tape) > 0:
            num = 0
            for entry in self.tape:
                if num < 5:
                    file_str.write(str(entry['qty']) + " @ " \
                        + str(entry['price']) \
                        + " (" + str(entry['timestamp']) + ")\n")
                    num += 1
                else:
                    break
        file_str.write("\n")
        return file_str.getvalue()
    
####################################
########### For testing ############
####################################
the_lob = OrderBook()
# create some limit orders
some_asks = [{'side' : 'ask', 
                'qty' : 5, 
                'price' : 101,
                'tid' : 100},
               {'side' : 'ask', 
                'qty' : 5, 
                'price' : 103,
                'tid' : 101},
               {'side' : 'ask', 
                'qty' : 5, 
                'price' : 101,
                'tid' : 102},
               {'side' : 'ask', 
                'qty' : 5, 
                'price' : 101,
                'tid' : 103},
               ]
some_bids = [{'side' : 'bid', 
                'qty' : 5, 
                'price' : 99,
                'tid' : 100},
               {'side' : 'bid', 
                'qty' : 5, 
                'price' : 98,
                'tid' : 101},
               {'side' : 'bid', 
                'qty' : 5, 
                'price' : 99,
                'tid' : 102},
               {'side' : 'bid', 
                'qty' : 5, 
                'price' : 97,
                'tid' : 103},
               ]
some_orders =  some_asks+some_bids
for order in some_orders:
    the_lob.processOrder('limit', order, True)
print "book before Cancel..."
print the_lob
the_lob.cancelOrder('ask', 3)
print "book after Cancel..."
print the_lob
#market_order = {'timestamp' : 4, 
#                'side' : 'bid', 
#                'qty' : 11, 
#                'tid' : 999}
#trades = the_lob.processOrder(4, 'market', market_order, True)
#print "\nbook after MO..."
#print the_lob
#print "\nResultant trades..."
#import pprint
#if trades:
#    pprint.pprint(trades)
#else:
#    print "no trades"
    


    
    
    
    
    