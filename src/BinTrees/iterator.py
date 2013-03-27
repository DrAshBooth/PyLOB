#!/usr/bin/env python
#coding:utf-8
# Author:  Mozman
# Purpose: iterator provides a TreeIterator for binary trees
# Created: 04.05.2010
# Copyright (C) 2010, 2011 by Manfred Moitzi
# License: LGPLv3 

class TreeIterator(object):
    __slots__ = ['_tree', '_direction', '_item', '_retfunc', ]
    def __init__(self, tree, rtype='key', reverse=False):
        """
        required tree methods:

        - get_walker
        - min_item
        - max_item
        - prev_item
        - succ_item

        param tree: binary tree
        param str rtype: 'key', 'value', 'item'
        param bool reverse: `False` for ascending order; `True` for descending order

        """
        self._tree = tree
        self._item = None
        self._direction = -1 if reverse else +1

        if rtype == 'key':
            self._retfunc = lambda item: item[0]
        elif rtype == 'value':
            self._retfunc = lambda item: item[1]
        elif rtype == 'item':
            self._retfunc = lambda item: item
        else:
            raise ValueError("Unknown return type '%s'" % rtype)

    @property
    def key(self):
        return self._item[0]

    @property
    def value(self):
        return self._item[1]

    @property
    def item(self):
        return self._item

    def __iter__(self):
        return self

    def next(self):
        return self._step(1)
    __next__ = next

    def prev(self):
        return self._step(-1)

    def _step(self, steps):
        if self._item is None:
            if self._direction == -1:
                self._item = self._tree.max_item()
            else:
                self._item = self._tree.min_item()
        else:
            step_dir = self._direction * steps
            step_func = self._tree.succ_item if step_dir > 0 else self._tree.prev_item
            try:
                self._item = step_func(self._item[0])
            except KeyError:
                raise StopIteration
        return self._retfunc(self._item)

    def goto(self, key):
        node = self._tree.get_walker()
        if node.goto(key):
            self._item = node.item
        else:
            raise KeyError(str(key))
