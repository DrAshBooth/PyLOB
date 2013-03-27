'''
Created on Mar 20, 2013

@author: Ash Booth
'''

class Order(object):
    def __init__(self, quote, orderList):
        self. useFloat = True    # Use floats to approximate prices instead of Decimals
        self.timestamp = int(quote['timestamp'])
        self.qty = int(quote['qty'])
        self.price = self.convertPrice(quote['price'])
        self.idNum = quote['idNum']
        self.tid = quote['tid']
        self.nextOrder = None
        self.prevOrder = None
        self.orderList = orderList
        
    def convertPrice(self, price):
        '''
        Converts price to an integer representing a mil. 
        1 mil = 0.0001
        Smallest representable size is 0.0001
        '''
        if self.useFloat:
            return int(float(price) * float(10000))
        else:
            # Exact representation
            from decimal import Decimal
            return int(Decimal(price) * Decimal(10000))
        
    def nextOrder(self):
        return self.nextOrder
    def prevOrder(self):
        return self.prevOrder
    
    def updateQty(self, newQty, newTimestamp):
        if newQty > self.qty and self.orderList.tailOrder != self:
            # Move order to end of the tier (loses time priority)            
            self.orderList.moveTail(self)
        self.orderList.volume -= self.qty - newQty
        self.timestamp = newTimestamp
        self.qty = newQty

    def __str__(self):
        return "%s\t@\t%.4f" % (self.qty, self.price / float(10000))
    
class Trade(Order):
    def __init__(self, quote):
        super(Trade, self).__init__(quote)

class Ask(Order):
    def __init__(self, quote):
        super(Ask, self).__init__(quote)
        self.isBid = False

class Bid(Order):
    def __init__(self, quote):
        super(Bid, self).__init__(quote)
        self.isBid = True
