'''
Created on Apr 11, 2013

@author: Ash Booth

For a full walkthrough of PyLOB functionality and usage,
see the wiki @ https://github.com/ab24v07/PyLOB/wiki

'''

from PyLOB import OrderBook

import sqlite3

def main():
    sqlite3.enable_callback_tracebacks(True)
    lob_connection = sqlite3.connect("lob.db")
    lob_connection.isolation_level = None
    #lob_connection.set_trace_callback(print)
    #lob_connection = None
    instrument = 'FAKE'
    one_trader = """
    insert into trader (tid, name) values (%(tid)d, '%(name)s') 
    on conflict do nothing;"""
    insert_traders = [one_trader.strip() % dict(tid=n, name=str(n)) for n in range(100, 112)]
    if lob_connection:
        lob_connection.executescript("""
            begin transaction;
            PRAGMA foreign_keys=1;
            %(insert_traders)s
            update trader 
                set 
                    commission_min=2.5,
                    commission_max_percnt=1,
                    commission_per_unit=0.01
            ;
            insert into instrument (symbol, currency) 
            values ('%(instrument)s', 'USD')
            on conflict do nothing;
            commit;
        """ % dict(
        	instrument=instrument, 
        	insert_traders='\n'.join(insert_traders),
        )
        )
    
    # Create a LOB object
    lob = OrderBook(db=lob_connection)
    
    ########### Limit Orders #############
    
    # Create some limit orders
    someOrders = [{'type' : 'limit', 
                   'side' : 'ask', 
                    'instrument': instrument,
                    'qty' : 5, 
                    'price' : 101,
                    'tid' : 100},
                   {'type' : 'limit', 
                    'side' : 'ask', 
                    'instrument': instrument,
                    'qty' : 5, 
                    'price' : 103,
                    'tid' : 101},
                   {'type' : 'limit', 
                    'side' : 'ask', 
                    'instrument': instrument,
                    'qty' : 5, 
                    'price' : 101,
                    'tid' : 102},
                   {'type' : 'limit', 
                    'side' : 'ask', 
                    'instrument': instrument,
                    'qty' : 5, 
                    'price' : 101,
                    'tid' : 103},
                   {'type' : 'limit', 
                    'side' : 'bid', 
                    'instrument': instrument,
                    'qty' : 5, 
                    'price' : 99,
                    'tid' : 100},
                   {'type' : 'limit', 
                    'side' : 'bid', 
                    'instrument': instrument,
                    'qty' : 5, 
                    'price' : 98,
                    'tid' : 101},
                   {'type' : 'limit', 
                    'side' : 'bid', 
                    'instrument': instrument,
                    'qty' : 5, 
                    'price' : 99,
                    'tid' : 102},
                   {'type' : 'limit', 
                    'side' : 'bid', 
                    'instrument': instrument,
                    'qty' : 5, 
                    'price' : 97,
                    'tid' : 103},
                   ]
    
    # Add orders to LOB
    for order in someOrders:
        trades, idNum = lob.processOrder(order, False, False)
        #print(trades, idNum)
    
    # The current book may be viewed using a print
    lob.print(instrument)
    
    # Submitting a limit order that crosses the opposing best price will 
    # result in a trade.
    crossingLimitOrder = {'type' : 'limit', 
                          'side' : 'bid', 
                          'instrument': instrument,
                          'qty' : 2, 
                          'price' : 102,
                          'tid' : 109}
    trades, orderInBook = lob.processOrder(crossingLimitOrder, False, False)
    print("Trade occurs as incoming bid limit crosses best ask..", crossingLimitOrder)
    lob.print(instrument)
    
    # If a limit order crosses but is only partially matched, the remaining 
    # volume will be placed in the book as an outstanding order
    bigCrossingLimitOrder = {'type' : 'limit', 
                             'side' : 'bid', 
                             'instrument': instrument,
                             'qty' : 50, 
                             'price' : 102,
                             'tid' : 110}
    trades, orderInBook = lob.processOrder(bigCrossingLimitOrder, False, False)
    print("Large incoming bid limit crosses best ask.\
           Remaining volume is placed in the book..", bigCrossingLimitOrder)
    lob.print(instrument)
    
    ############# Market Orders ##############
    
    # Market orders only require that the user specifies a side (bid
    # or ask), a quantity and their unique tid.
    marketOrder = {'type' : 'market', 
                   'side' : 'ask', 
                   'instrument': instrument,
                   'qty' : 40, 
                   'tid' : 111}
    trades, idNum = lob.processOrder(marketOrder, False, False)
    print("A market order takes the specified volume from the\
            inside of the book, regardless of price") 
    print("A market ask for 40 results in..", marketOrder)
    lob.print(instrument)
    
    ############ Cancelling Orders #############
    
    # Order can be cancelled simply by submitting an order idNum and a side
    print("cancelling bid for 5 @ 97..")
    lob.cancelOrder('bid', 8)
    lob.print(instrument)
    
    ########### Modifying Orders #############
    
    # Orders can be modified by submitting a new order with an old idNum
    modifyOrder5 = {'side' : 'bid', 
                    'qty' : 14, 
                    'price' : 99,
                    'tid' : 100}
    lob.modifyOrder(5, modifyOrder5)
    print("book after increase amount. will be put as end of queue")
    
    lob.print(instrument)
    
    modifyOrder5 = {'side' : 'bid', 
                    'qty' : 14, 
                    'price' : 103.2,
                    'tid' : 100}
    lob.modifyOrder(5, modifyOrder5)
    print("book after improve bid price. will process the order")
    
    lob.print(instrument)

    ############# Outstanding Market Orders ##############
    # this loops forever in the compatibility mode.
    # though after my patches it works ok, i didn't find the bug.
    # my next version will use a db, for more extensive activity.
    marketOrder = {'type' : 'market', 
                   'side' : 'ask', 
                   'instrument': instrument,
                   'qty' : 40, 
                   'tid' : 111}
    trades, idNum = lob.processOrder(marketOrder, False, False)
    print("A market ask for 40 should take all the bids and keep the remainder in the book", marketOrder)
    lob.print(instrument)
    
    if lob_connection:
        lob.db.close()

if __name__ == '__main__':
    main()

