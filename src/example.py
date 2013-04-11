'''
Created on Apr 11, 2013

@author: ASh Booth
'''

if __name__ == '__main__':
    
    from PyLOB import OrderBook
    
    ####################################
    ########### For testing ############
    ####################################
    
    lob = OrderBook()
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
        lob.processOrder('limit', order, True)
    print "book before Modify..."
    print lob
    print "volume at x = %d" % lob.getVolumeAtPrice('bid', 99.000004)
    lob.modifyOrder('bid', 6, {'side' : 'bid', 
                    'qty' : 14, 
                    'price' : 99,
                    'tid' : 103})
    print "book after modify..."
    print lob
    print "volume at x = %d" % lob.getVolumeAtPrice('bid', 99.000004)
    
    print "\nbest prices: "
    print "bid: %f\t\t%f :Ask" % (lob.getBestBid(),lob.getBestAsk())
    