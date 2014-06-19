import asyncio

class NotifierError(Exception):
    pass

class NotifierBase:
    def __init__(self, loop=None):
        super().__init__()
        self._loop = loop or asyncio.get_event_loop()


    def _notify(self, episodes):
        raise NotImplementedError


    def notify(self, episodes):
        return self._notify(episodes)
