import coroutine

from .base import NotifierBase, NotifierError
from .. import web

__all__ = ['Notifier']

class Notifier(NotifierBase):
    @asyncio.coroutine
    def _notify(self, episodes):
        # TODO: don't spam individual notifications if # of episodes is more
        # than say 5.
        for ep in episodes:
            web.notify('alert', title='Episode retrieved', text='Downloaded %s %s' % (ep.series.name, ep.code))