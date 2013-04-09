'''
Created on Mar 20, 2013

@author: Ash Booth
'''

class Order(object):
    def __init__(self, quote, orderList):
        self. useFloat = True    # Use floats to approximate prices instead of Decimals
        self.timestamp = int(quote['timestamp'])
        self.qty = int(quote['qty'])
        self.price = quote['price']
        self.idNum = quote['idNum']
        self.tid = quote['tid']
        self.nextOrder = None
        self.prevOrder = None
        self.orderList = orderList
        
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
        return "%s\t@\t%.4f\tt=%d" % (self.qty, self.price, self.timestamp)
    
#class Trade(Order):
#    def __init__(self, quote):
#        super(Trade, self).__init__(quote)
#
#class Ask(Order):
#    def __init__(self, quote):
#        super(Ask, self).__init__(quote)
#        self.isBid = False
#
#class Bid(Order):
#    def __init__(self, quote):
#        super(Bid, self).__init__(quote)
#        self.isBid = True
