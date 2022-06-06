'''
Created on Apr 11, 2013

@author: Ash Booth

For a full walkthrough of PyLOB functionality and usage,
see the wiki @ https://github.com/ab24v07/PyLOB/wiki

'''

if __name__ == '__main__':
    
    from PyLOB import OrderBook
    
    # Create a LOB object
    lob = OrderBook()
    
    ########### Limit Orders #############
    
    # Create some limit orders
    someOrders = [{'type' : 'limit', 
                   'side' : 'ask', 
                    'qty' : 5, 
                    'price' : 101,
                    'tid' : 100},
                   {'type' : 'limit', 
                    'side' : 'ask', 
                    'qty' : 5, 
                    'price' : 103,
                    'tid' : 101},
                   {'type' : 'limit', 
                    'side' : 'ask', 
                    'qty' : 5, 
                    'price' : 101,
                    'tid' : 102},
                   {'type' : 'limit', 
                    'side' : 'ask', 
                    'qty' : 5, 
                    'price' : 101,
                    'tid' : 103},
                   {'type' : 'limit', 
                    'side' : 'bid', 
                    'qty' : 5, 
                    'price' : 99,
                    'tid' : 100},
                   {'type' : 'limit', 
                    'side' : 'bid', 
                    'qty' : 5, 
                    'price' : 98,
                    'tid' : 101},
                   {'type' : 'limit', 
                    'side' : 'bid', 
                    'qty' : 5, 
                    'price' : 99,
                    'tid' : 102},
                   {'type' : 'limit', 
                    'side' : 'bid', 
                    'qty' : 5, 
                    'price' : 97,
                    'tid' : 103},
                   ]
    
    # Add orders to LOB
    for order in someOrders:
        trades, idNum = lob.processOrder(order, False, False)
    
    # The current book may be viewed using a print
    print lob
    
    # Submitting a limit order that crosses the opposing best price will 
    # result in a trade.
    crossingLimitOrder = {'type' : 'limit', 
                          'side' : 'bid', 
                          'qty' : 2, 
                          'price' : 102,
                          'tid' : 109}
    trades, orderInBook = lob.processOrder(crossingLimitOrder, False, False)
    print "Trade occurs as incoming bid limit crosses best ask.."
    print trades
    print lob
    
    # If a limit order crosses but is only partially matched, the remaining 
    # volume will be placed in the book as an outstanding order
    bigCrossingLimitOrder = {'type' : 'limit', 
                             'side' : 'bid', 
                             'qty' : 50, 
                             'price' : 102,
                             'tid' : 110}
    trades, orderInBook = lob.processOrder(bigCrossingLimitOrder, False, False)
    print "Large incoming bid limit crosses best ask.\
           Remaining volume is placed in the book.."
    print lob
    
    ############# Market Orders ##############
    
    # Market orders only require that the user specifies a side (bid
    # or ask), a quantity and their unique tid.
    marketOrder = {'type' : 'market', 
                   'side' : 'ask', 
                   'qty' : 40, 
                   'tid' : 111}
    trades, idNum = lob.processOrder(marketOrder, False, False)
    print "A limit order takes the specified volume from the\
            inside of the book, regardless of price" 
    print "A market ask for 40 results in.."
    print lob
    
    ############ Cancelling Orders #############
    
    # Order can be cancelled simply by submitting an order idNum and a side
    print "cancelling bid for 5 @ 97.."
    lob.cancelOrder('bid', 8)
    print lob
    
    ########### Modifying Orders #############
    
    # Orders can be modified by submitting a new order with an old idNum
    lob.modifyOrder(5, {'side' : 'bid', 
                    'qty' : 14, 
                    'price' : 99,
                    'tid' : 100})
    print "book after modify..."
    print lob
    