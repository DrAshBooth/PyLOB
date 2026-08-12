PyLOB
=====

Fully functioning fast Limit Order Book written in Python

PyLOB, is a fully functioning fast simulation of a limit-order-book financial exchange, developed for modelling. The aim is to allow exploration of automated trading strategies that deal with "Level 2" market data.

It is written in Python, single-threaded and opperates a standard price-time-priority. It supports both market and limit orders, as well as add, cancel and update functionality. The model is based on few simplifying assumptions, chief of which is zero latency: if a trader issues a new quote, that gets processed by the exchange, all other traders can react to it before any other quote is issued.

Installation:
=============
PyLOB is not published on PyPI (the `pylob` name there belongs to an unrelated project). Install it from GitHub; Python 3.11 or newer is required:

    pip install "PyLOB @ git+https://github.com/DrAshBooth/PyLOB.git"

or, with [uv](https://docs.astral.sh/uv/):

    uv add "PyLOB @ git+https://github.com/DrAshBooth/PyLOB.git"

To work on PyLOB itself, clone the repo and run `uv sync`; `./verify` is the definition of done.

Requirements:
=============
To ensure easy distribution and use I've tried to ensure that there are no requirements other than a standard python3 install. The code for the RBTrees was taken directly from the bintrees library and is implemented in pure python. This is to improve portability and ensure easy of use for all. Credit to Julienne Walker ( http://eternallyconfuzzled.com/jsw_home.aspx ) for the great algorithms.

Check the Wiki!
===============
For details on limit order books as well as usage guides and examples, please see the wiki.

The code is open-sourced via the MIT Licence: see the LICENSE file for full text. (copied from http://opensource.org/licenses/mit-license.php)

