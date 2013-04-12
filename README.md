PyLOB
=====

Fully functioning fast Limit Order Book written in Python

PyLOB, is a fully functioning fast simulation of a limit-order-book financial exchange, developed for modelling. The aim is to allow exploration of automated trading strategies that deal with "Level 2" market data.

It is written in Python, single-threaded and opperates a standard price-time-priority. It supports both market and limit orders, as well as add, cancel and update functionality. The model is based on few simplifying assumptions, chief of which is zero latency: if a trader issues a new quote, that gets processed by the exchange, all other traders can react to it before any other quote is issued.

Requirements:
=============
To ensure easy distribution and use I've tried to ensure that there are no requirements other than a standard python install. The code for the RBTrees was taken directly from the bintrees library and is implemented in pure python. This is to improve portability and ensure easy of use for all. Credit to Julienne Walker ( http://eternallyconfuzzled.com/jsw_home.aspx ) for the great algorithms.

Check the Wiki!
===============
For details on limit order books as well as usage guides and examples, please see the wiki.

The code is open-sourced via the MIT Licence: see the LICENSE file for full text. (copied from http://opensource.org/licenses/mit-license.php)

