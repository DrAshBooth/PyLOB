'''
Created on Apr 11, 2013

@author: Ash Booth

For a full walkthrough of PyLOB functionality and usage,
see the wiki @ https://github.com/ab24v07/PyLOB/wiki

'''

if __name__ == '__main__':
    
    from PyLOB import OrderBook
    
    # create a LOB object
    lob = OrderBook()
    
    ########### Limit Orders #############
    
    # create some limit orders
    some_orders = [{'side' : 'ask', 
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
                   {'side' : 'bid', 
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
    
    # Add orders to LOB
    ids = []
    for order in some_orders:
        trades, idNum = lob.processOrder('limit', order, False)
        ids.append(idNum)
    
    # The current book may be viewed using a print
    print lob
    
    # Submitting a limit order that crosses the opposing best price will 
    # result in a trade.
    crossing_limit_order = {'side' : 'bid', 
                    'qty' : 2, 
                    'price' : 102,
                    'tid' : 109}
    trades, idNum = lob.processOrder('limit', crossing_limit_order, False)
    print "Trade occurs as incoming bid limit crosses best ask.."
    print trades
    print lob
    
    # if a limit order crosses but is only partially matched, the remaining 
    # volume will be placed in the book as an outstanding order
    big_crossing_limit_order = {'side' : 'bid', 
                    'qty' : 50, 
                    'price' : 102,
                    'tid' : 110}
    trades, idNum = lob.processOrder('limit', big_crossing_limit_order, False)
    print "Large incoming bid limit crosses best ask. Remaining volume is placed in the book.."
    print lob
    
    ############# Market Orders ##############
    
    # Market orders only require that the user specifies a side (bid
    # or ask), a quantity and their unique tid.
    market_order = {'side' : 'ask', 
                    'qty' : 40, 
                    'tid' : 111}
    trades, idNum = lob.processOrder('market', market_order, False)
    print "A limit order takes the specified volume from the inside of the book, regardless of price"
    print "A market ask for 40 results in.."
    print lob
    
    ############ Cancelling Orders #############
    
    # Order can be cancelled simply by submitting an order idNum and a side
    print "cancelling bid for 5 @ 97.."
    lob.cancelOrder('bid', 8)
    print lob
    
    ########### Modifying Orders #############
    
    # Orders can be modified by submitting a new order with an old idNum
    lob.modifyOrder('bid', 5, {'side' : 'bid', 
                    'qty' : 14, 
                    'price' : 99,
                    'tid' : 100})
    print "book after modify..."

    