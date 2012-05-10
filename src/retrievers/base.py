from __future__ import absolute_import

class RetrieverError(Exception):
    pass

class RetrieverBase(object):
    SUPPORTED_TYPES = ()
    ALWAYS_ENABLED = False
