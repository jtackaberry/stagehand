import asyncio

from ..utils import load_plugins, invoke_plugins

plugins, broken_plugins = load_plugins('platform', ['win32'])

@asyncio.coroutine
def start(manager):
    """
    Called when the manager is starting.
    """
    yield from invoke_plugins(plugins, 'start', manager)



def stop():
    for plugin in plugins.values():
        if hasattr(plugin, 'stop'):
            plugin.stop()