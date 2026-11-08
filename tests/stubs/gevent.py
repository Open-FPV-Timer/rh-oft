"""Test stub of the two gevent calls rh_oft uses. Tests drive update() directly."""

import time


def sleep(seconds=0):
    time.sleep(0)


class _Greenlet:
    def __init__(self, fn):
        self.fn = fn

    def kill(self, block=True, timeout=None):
        pass


def spawn(fn, *args, **kwargs):
    return _Greenlet(fn)
