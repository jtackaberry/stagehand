import xml.sax
import asyncio

from ..toolbox.net import download
from ..utils import ElementParser

@asyncio.coroutine
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

    if isinstance(what, str) and what.startswith('http'):
        status, data = yield from download(what, retry=4)
        if status != 200:
            raise ValueError('download failed with http status %d' % status)
        parser.feed(data)
        parser.close()
    else:
        parser.parse(what)

    return results


class ProviderError(Exception):
    pass


class ProviderBase:
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


class ProviderSearchResultBase:
    def __init__(self, provider, attrs):
        self.provider = provider
        self._attrs = attrs

    @property
    def id(self):
        return self.provider.NAME + ':' + self.pid
