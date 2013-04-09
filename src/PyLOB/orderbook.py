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

class OrderBook(object):
    def __init__(self):
        self.tape = deque(maxlen=None) # Index [0] is most recent trade
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
    
    def processOrder(self, time, order_type, quote, verbose):
        if quote['qty'] <= 0:
            sys.exit('processLimitOrder() given order of qty <= 0')
        # Assign idNum
        quote['idNum'] = self.nextQuoteID
        self.nextQuoteID += 1
        # Check order type
        if order_type=='market':
            trades = self.processMarketOrder(time, quote, verbose)
        elif order_type=='limit':
            # Clip the price according to the tick size
            quote['price'] = self.clipPrice(quote['price'])
            trades = self.processLimitOrder(time, quote, verbose)
        else:
            sys.exit("processOrder() given neither 'market' nor 'limit'")
        return trades
    
    def processMarketOrder(self, time, quote, verbose):
        trades = []
        qty_to_trade = quote['qty']
        side = quote['side']
        if side == 'bid':
            while qty_to_trade > 0 and self.asks: 
                best_price_asks = self.asks.minPriceList()
                while len(best_price_asks)>0 and qty_to_trade > 0:
                    head_order = best_price_asks.getHeadOrder()
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
                        # lift the ask
                        self.asks.removeOrderById(head_order.idNum)
                        # incoming done with
                        qty_to_trade = 0
                    else:
                        traded_qty = head_order.qty
                        # lift the ask
                        self.asks.removeOrderById(head_order.idNum)
                        # we need to keep eating into volume at this price
                        qty_to_trade -= traded_qty
                    if verbose: print('>>> TRADE \nt=%d $%f n=%d p1=%d p2=%d' % (time, traded_price, traded_qty, counterparty, quote['tid']))
                    transaction_record = {'timestamp': time,
                                               'price': traded_price,
                                               'qty': traded_qty, 
                                               'party1': [counterparty, 'ask'],
                                               'party2': [quote['tid'], 'bid'],
                                               'qty': traded_qty}
                    self.tape.append(transaction_record)
                    trades.append(transaction_record)
        elif side == 'ask':
            while qty_to_trade > 0 and self.bids: 
                best_price_bids = self.bids.maxPriceList()
                while len(best_price_bids)>0 and qty_to_trade > 0:
                    head_order = best_price_bids.getHeadOrder()
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
                        # hit the bid
                        self.bids.removeOrderById(head_order.idNum)
                        # incoming done with
                        qty_to_trade = 0
                    else:
                        traded_qty = head_order.qty
                        # hit the bid
                        self.bids.removeOrderById(head_order.idNum)
                        # we need to keep eating into volume at this price
                        qty_to_trade -= traded_qty
                    if verbose: print('>>> TRADE \nt=%d $%f n=%d p1=%d p2=%d' % (time, traded_price, traded_qty, counterparty, quote['tid']))
                    transaction_record = {'timestamp': time,
                                               'price': traded_price,
                                               'qty': traded_qty, 
                                               'party1': [counterparty, 'bid'],
                                               'party2': [quote['tid'], 'ask'],
                                               'qty': traded_qty}
                    trades.append(transaction_record)
                    self.tape.append(transaction_record)
        else:
            sys.exit('processMarketOrder() received neither "bid" nor "ask"')
        return trades
    
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
# creates some bids
some_orders = [{'timestamp' : 1, 
                'side' : 'bid', 
                'qty' : 5, 
                'price' : 99,
                'tid' : 100,
                'idNum' : 1},
               {'timestamp' : 2, 
                'side' : 'bid', 
                'qty' : 5, 
                'price' : 98,
                'tid' : 101,
                'idNum' : 2},
               {'timestamp' : 3, 
                'side' : 'bid', 
                'qty' : 5, 
                'price' : 99,
                'tid' : 102,
                'idNum' : 3},
               {'timestamp' : 4, 
                'side' : 'bid', 
                'qty' : 5, 
                'price' : 97,
                'tid' : 103,
                'idNum' : 4},
               ]
for order in some_orders:
    the_lob.bids.insertOrder(order)

print "book before MO..."
print the_lob
market_order = {'timestamp' : 4, 
                'side' : 'ask', 
                'qty' : 11, 
                'tid' : 999}
trades = the_lob.processOrder(4, 'market', market_order, True)
print "\nbook after MO..."
print the_lob
print "\nResultant trades..."
import pprint
if trades:
    pprint.pprint(trades)
else:
    print "no trades"
    


    
    
    
    
    