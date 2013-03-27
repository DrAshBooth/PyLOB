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
        self.volume = 0
        self.priceMap = {} # Map from price -> orderList object
        self.orderMap = {} # Order ID to Order object
    def __len__(self):
        return len(self.orderMap)
    def getPrice(self, price):
        return self.priceMap[price]
    def getOrder(self, idNum):
        return self.orderMap[idNum]
    def createPrice(self, price):
        newList = OrderList()
        self.priceTree.insert(price, newList)
        self.priceMap[price] = newList
    def removePrice(self, price):
        self.priceTree.remove(price)
        del self.priceMap[price]
    def priceExists(self, price):
        return price in self.priceMap
    def orderExists(self, idNum):
        return idNum in self.orderMap
    def insertOrder(self, quote): ####
        if quote['price'] not in self.priceMap:
            self.createPrice(quote['price'])
        order = Order(quote, self.priceMap[quote['price']])
        self.priceMap[order.price].appendOrder(order)
        self.orderMap[order.idNum] = order
        self.volume += order.qty
    def updateOrder(self, order_update):
        order = self.orderMap[order_update['idNum']]
        originalVolume = order.qty
        if order_update['price'] != order.price:
            # Price changed
            orderList = self.priceMap[order.price]
            orderList.removeOrder(order)
            if len(orderList) == 0:
                self.removePrice(order.price) 
            self.insertOrder(order_update)
        else:
            # Quantity changed
            order.updateQty(order_update['qty'], order_update['price'])
        self.volume += order.qty - originalVolume
    def removeOrderById(self, idNum):
        order = self.orderMap[idNum]
        self.volume -= order.qty
        order.orderList.removeOrder(order)
        if len(order.orderList) == 0:
            self.removePrice(order.price)
        del self.orderMap[idNum]
    def max(self):
        return min(self.priceTree)
    def min(self):
        return max(self.priceTree)
