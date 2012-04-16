from __future__ import absolute_import
import xml.sax

import kaa
from kaa.saxutils import ElementParser

from ..utils import download

@kaa.coroutine()
def parse_xml(what, nest=[]):
    results = []
    def handle(element):
        info = {}
        attrs = dict((a, getattr(element, a)) for a in element.attributes)
        if element.content:
            results.append((element.tagname, attrs, element.content))
        else:
            for child in element:
                if child.tagname in nest:
                    handle(child)
                elif child.content:
                    info[child.tagname] = child.content
            results.append((element.tagname, attrs, info))
    e = ElementParser()
    e.handle = handle
    parser = xml.sax.make_parser()
    parser.setContentHandler(e)

    if isinstance(what, basestring) and what.startswith('http'):
        status, data = yield download(what, retry=4)
        if status != 200:
            raise ValueError('download failed with http status %d' % status)
        parser.feed(data)
        parser.close()
    else:
        parser.parse(what)

    yield results


class ProviderError(Exception):
    pass


class ProviderBase(object):
    def __init__(self, db):
        self.db = db

    def search(self, name):
        raise NotImplementedError

    def get_series(self, id):
        raise NotImplementedError
        
    def get_changed_series_ids(self):
        """
        Return a list of provider ids of series that have changed since
        last invocation.
        """
        raise NotImplementedError

    def get_last_updated(self):
        """
        Return a timestamp since last synced with server.
        """
        raise NotImplementedError


class ProviderSearchResultBase(object):
    def __init__(self, db, **kwargs):
        self._db = db
        [setattr(self, k, v) for k, v in kwargs.items()]

    @property
    def id(self):
        return self.provider.NAME + ':' + self.pid

