#!/usr/bin/env python
#coding:utf-8
# Author:  mozman
# Purpose: tree walker
# Created: 07.05.2010
# Copyright (C) 2010, 2011 by Manfred Moitzi
# License: LGPLv3

class Walker(object):
    __slots__ = ['_node', '_stack', '_tree']

    def __init__(self, tree):
        self._tree = tree
        self._node = tree.root
        self._stack = []

    def reset(self):
        self._stack = []
        self._node = self._tree.root

    @property
    def key(self):
        return self._node.key

    @property
    def value(self):
        return self._node.value

    @property
    def item(self):
        return (self._node.key, self._node.value)

    @property
    def is_valid(self):
        return self._node is not None

    def goto(self, key):
        self._node = self._tree.root
        while self._node is not None:
            if key == self._node.key:
                return True
            elif key < self._node.key:
                self.go_left()
            else:
                self.go_right()
        return False

    def push(self):
        self._stack.append(self._node)

    def pop(self):
        self._node = self._stack.pop()

    def stack_is_empty(self):
        return (self._stack is None) or (len(self._stack) == 0)

    def goto_leaf(self):
        """ get a leaf node """
        while self._node is not None:
            if self.has_left():
                self.go_left()
            elif self.has_right():
                self.go_right()
            else:
                return

    def has_child(self, direction):
        if direction == 0:
            return self._node.left is not None
        else:
            return self._node.right is not None

    def down(self, direction):
        if direction == 0:
            self._node = self._node.left
        else:
            self._node = self._node.right

    def go_left(self):
        self._node = self._node.left

    def go_right(self):
        self._node = self._node.right

    def has_left(self):
        return self._node.left is not None

    def has_right(self):
        return self._node.right is not None

    def succ_item(self, key):
        """ Get successor (k,v) pair of key, raises KeyError if key is max key
        or key does not exist.
        """
        self._node = self._tree.root
        succ = None
        while self._node is not None:
            if key == self._node.key:
                break
            elif key < self._node.key:
                if (succ is None) or (self._node.key < succ[0]):
                    succ = self.item
                self._node = self._node.left
            else:
                self._node = self._node.right

        if self._node is None: # stay at dead end
            raise KeyError(str(key))
        # found node of key
        if self._node.right is not None:
            # find smallest node of right subtree
            self._node = self._node.right
            while self._node.left is not None:
                self._node = self._node.left
            if succ is None:
                succ = self.item
            elif self._node.key < succ[0]:
                succ = self.item
        elif succ is None: # given key is biggest in tree
            raise KeyError(str(key))
        return succ

    def prev_item(self, key):
        """ Get predecessor (k,v) pair of key, raises KeyError if key is min key
        or key does not exist.
        """
        self._node = self._tree.root
        prev = None
        while self._node is not None:
            if key == self._node.key:
                break
            elif key < self._node.key:
                self._node = self._node.left
            else:
                if (prev is None) or (self._node.key > prev[0]):
                    prev = self.item
                self._node = self._node.right

        if self._node is None: # stay at dead end (None)
            raise KeyError(str(key))
        # found node of key
        if self._node.left is not None:
            # find biggest node of left subtree
            self._node = self._node.left
            while self._node.right is not None:
                self._node = self._node.right
            if prev is None:
                prev = self.item
            elif self._node.key > prev[0]:
                prev = self.item
        elif prev is None: # given key is smallest in tree
            raise KeyError(str(key))
        return prev
