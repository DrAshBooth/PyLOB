'''
Created on Mar 27, 2013

@author: Ash Booth
'''

from BinTrees import RBTree
from orderlist import OrderList
from order import Order

class OrderTree(object):
    def __init__(self):
        self.priceTree = RBTree()
        self.priceMap = {}  # Map from price -> orderList object
        self.orderMap = {}  # Order ID to Order object
        self.volume = 0     # How much volume on this side?
        self.nOrders = 0   # How many orders?
        self.lobDepth = 0  # How many different prices on lob?
        
    def __len__(self):
        return len(self.orderMap)
    
    def getPrice(self, price):
        return self.priceMap[price]
    
    def getOrder(self, idNum):
        return self.orderMap[idNum]
    
    def createPrice(self, price):
        self.lobDepth += 1
        newList = OrderList()
        self.priceTree.insert(price, newList)
        self.priceMap[price] = newList
        
    def removePrice(self, price):
        self.lobDepth -= 1
        self.priceTree.remove(price)
        del self.priceMap[price]
        
    def priceExists(self, price):
        return price in self.priceMap
    
    def orderExists(self, idNum):
        return idNum in self.orderMap
    
    def insertOrder(self, quote):
        self.nOrders += 1
        if quote['price'] not in self.priceMap:
            self.createPrice(quote['price'])
        order = Order(quote, self.priceMap[quote['price']])
        self.priceMap[order.price].appendOrder(order)
        self.orderMap[order.idNum] = order
        self.volume += order.qty
        
    def updateOrder(self, orderUpdate):
        order = self.orderMap[orderUpdate['idNum']]
        originalVolume = order.qty
        if orderUpdate['price'] != order.price:
            # Price changed
            orderList = self.priceMap[order.price]
            orderList.removeOrder(order)
            if len(orderList) == 0:
                self.removePrice(order.price) 
            self.insertOrder(orderUpdate)
        else:
            # Quantity changed
            order.updateQty(orderUpdate['qty'], orderUpdate['timestamp'])
        self.volume += order.qty-originalVolume
        
    def removeOrderById(self, idNum):
        self.nOrders -= 1
        order = self.orderMap[idNum]
        self.volume -= order.qty
        order.orderList.removeOrder(order)
        if len(order.orderList) == 0:
            self.removePrice(order.price)
        del self.orderMap[idNum]
        
    def maxPrice(self):
        return self.priceTree.max_key()
    
    def minPrice(self):
        return self.priceTree.min_key()
    
    def maxPriceList(self):
        return self.getPrice(self.maxPrice())
    
    def minPriceList(self):
        return self.getPrice(self.minPrice())

