import sys
import os
import logging
import time
import signal
import argparse
import errno
import functools

import asyncio
import aiohttp

from stagehand import web, logger, tvdb, __version__
from stagehand.manager import Manager
from stagehand.config import config
from stagehand.toolbox import singleton
from stagehand.toolbox.utils import tempfile, tostr, daemonize
from stagehand import platform

# Explicitly import the web app module so the path routes get setup.
import stagehand.web.app

log = logging.getLogger('stagehand')

class FullHelpParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write('error: %s\n' % message)
        self.print_help()
        sys.exit(2)



def find_data_path():
    # First check to see if we're running out of an executable zip.  If so,
    # use the data path from inside the zip.
    try:
        return os.path.join(os.path.abspath(__loader__.archive), 'data')
    except AttributeError:
        # Referencing the 'archive' attribute failed, so we're not running in
        # a zip.  Look on the filesystem.
        pass


    cwd = os.path.dirname(__file__)
    data_paths = [
        os.path.join(cwd, '../data'),  # running out of build/lib
        os.path.join(cwd, '../share/stagehand'),
        os.path.join(sys.exec_prefix, 'share/stagehand'),
        os.path.join(sys.prefix, 'share/stagehand'),
        '/usr/local/share/stagehand',
        '/usr/share/stagehand'
    ]
    for path in data_paths:
        if os.path.isdir(path):
            return os.path.realpath(path)

    print("Error: couldn't find static data.  I tried looking in:")
    for path in data_paths:
        print('   *', os.path.realpath(path))
    sys.exit(1)


def build_path(*parts):
    path = os.path.join(*parts)
    return os.path.expandvars(os.path.expanduser(path))


def init_default_paths(datadir):
    if not datadir:
        datadir = find_data_path()
    elif not os.path.isdir(datadir):
        raise FileNotFoundError

    class Paths:
        pass
    paths = Paths()
    paths.data = datadir
    if sys.platform == 'win32':
        appdata = build_path(os.getenv('APPDATA', '~'), 'Stagehand')
        paths.webcache = os.path.join(appdata, 'Cache')
        paths.logs = os.path.join(appdata, 'Logs')
        paths.config = os.path.join(appdata, 'config.txt')
        paths.db = os.path.join(appdata, 'stagehand.sqlite')
    else:
        cache = build_path(os.getenv('XDG_CACHE_HOME', '~/.cache'), 'stagehand')
        config = build_path(os.getenv('XDG_CONFIG_HOME', '~/.config'), 'stagehand')
        paths.webcache = os.path.join(cache, 'web')
        paths.logs = os.path.join(cache, 'logs')
        paths.config = os.path.join(config, 'config')
        paths.db = os.path.join(config, 'tv.db')
    return paths


def create_paths(paths):
    os.makedirs(paths.webcache, exist_ok=True)
    os.makedirs(paths.logs, exist_ok=True)
    os.makedirs(os.path.dirname(paths.config), exist_ok=True)


def reload(mgr):
    log.warning('received SIGHUP: purging in-memory caches and reloading config')
    config.load()
    mgr.tvdb.purge_caches()
    loop.call_soon(asyncio.async, mgr.check_new_episodes())


def resync(mgr):
    log.warning('received SIGUSR1: refreshing thetvdb')
    loop.call_soon(asyncio.async, mgr.tvdb.sync())


@asyncio.coroutine
def web_start_server(mgr, args):
    web.TEMPLATE_PATH[:] = [os.path.join(mgr.paths.data, 'web')]
    webkwargs = {}
    if config.web.username:
        webkwargs['user'] = config.web.username
        webkwargs['passwd'] = config.web.password
    if config.web.bind_address:
        webkwargs['host'] = config.web.bind_address
    if len(args.verbose) >= 2:
        # Beware: debug=True disables template caching
        webkwargs['debug'] = True

    userdata = {'stagehand.manager': mgr, 'coffee.cachedir': mgr.paths.webcache}
    # Try a number of different ports until we find out.  Now that the manager
    # is instantiated, the config file will have been loaded and
    # config.web.port will be the user-configured value (if any).
    ports = [args.port or int(config.web.port)] + list(range(8088, 8100)) + list(range(18088, 18100))
    for port in ports:
        try:
            yield from web.start(port=port, log=logging.getLogger('stagehand.http'), userdata=userdata, **webkwargs)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EADDRINUSE):
                log.error('could not start webserver on port %d: %s', port, str(e))
            elif e.errno == errno.EADDRNOTAVAIL:
                log.error('could not start webserver on interface %s: %s', config.web.bind_address, str(e))
                break
            else:
                raise
        else:
            # Server started successfully.  Store current port for service control.
            portfile = tempfile('stagehand', 'port', unique=False)
            with open(portfile, 'w') as f:
                f.write('{}\n'.format(port))

            return

    # If we get here, web server couldn't start
    log.critical('failed to start webserver; edit %s or restart with different arguments', mgr.cfgfile)
    mgr.loop.stop()



@singleton
def web_config_changed(mgr, args, var, old, new):
    """
    Invoked when any value in the config.web group changes.

    This is decorated with @singleton so that if multiple variables in the
    config.web group change, this function only actually gets executed once.
    """
    web.stop()
    log.warning('restarting webserver due to changed configuration')
    asyncio.async(web_start_server(mgr, args))


def call_rest_api(path):
    portfile = tempfile('stagehand', 'port', unique=False)
    try:
        with open(portfile) as f:
            port = int(f.read().strip())
    except FileNotFoundError:
        return

    data = None
    @asyncio.coroutine
    def _call(url):
        nonlocal data
        try:
            # FIXME: auth won't actually work.  Server uses digest, but
            # aiohttp only supports basic.
            response = yield from aiohttp.request('GET', url, auth=(config.web.username, config.web.password))
        except aiohttp.OsConnectionError:
            return
        data = yield from response.read_and_close()
        if response.status != 200:
            log.error('request failed (status %s): %s', response.status, tostr(data))

    loop = asyncio.get_event_loop()
    loop.run_until_complete(_call('http://localhost:{}{}'.format(port, path)))
    return data


def main():
    p = FullHelpParser(prog='stagehand')
    p.add_argument('-q', '--quiet', dest='quiet', action='store_true',
                   help='disable all logging')
    p.add_argument('-v', '--verbose', dest='verbose', action='append_const', default=[], const=1,
                   help='log more detail (twice or thrice logs even more)')
    p.add_argument('-b', '--bg', dest='background', action='store_true',
                   help='run stagehand in the background (daemonize)')
    p.add_argument('-p', '--port', dest='port', action='store', type=int, default=0,
                   help='port the embedded webserver listens on (default is %d)' % config.web.port)
    p.add_argument('-d', '--data', dest='data', action='store', metavar='PATH',
                   help="path to Stagehand's static data directory")
    p.add_argument('-s', '--stop', dest='stop', action='store_true',
                   help='stop a currently running instance of Stagehand')
    p.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    args = p.parse_args()

    paths = init_default_paths(args.data)

    if os.path.exists(paths.config):
        config.load(paths.config)
    if config.misc.logdir != config.misc.logdir.default:
        # If logdir configurable is non-default, then apply it.
        paths.logs = build_path(config.misc.logdir)

    create_paths(paths)

    handler = logging.FileHandler(os.path.join(paths.logs, 'stagehand.log'))
    handler.setFormatter(logging.getLogger().handlers[0].formatter)
    logging.getLogger().addHandler(handler)

    handler = logging.FileHandler(os.path.join(paths.logs, 'http.log'))
    handler.setFormatter(logging.getLogger().handlers[0].formatter)
    logging.getLogger('stagehand.http').addHandler(handler)

    if args.stop:
        if call_rest_api('/api/shutdown') is None:
            log.info('Stagehand is not running.')
        return


    # Make sure Stagehand isn't already running.
    if call_rest_api('/api/pid') is not None:
        return log.error('Stagehand is already running')


    # Default log levels.

    log.setLevel(logging.INFO)
    logging.getLogger('stagehand.http').setLevel(logging.INFO)
    logging.getLogger('config').setLevel(logging.INFO)
    logging.getLogger('http').setLevel(logging.DEBUG)

    if args.quiet:
        log.setLevel(logging.CRITICAL)
    elif len(args.verbose) == 1:
        log.setLevel(logging.DEBUG)
        logging.getLogger('config').setLevel(logging.DEBUG)
        logging.getLogger('stagehand.web').setLevel(logging.INFO)
    elif len(args.verbose) >= 2:
        log.setLevel(logging.DEBUG2)
        logging.getLogger('config').setLevel(logging.DEBUG)
        if len(args.verbose) == 3:
            logging.getLogger('stagehand.web').setLevel(logging.DEBUG2)
            logging.getLogger('stagehand.http').setLevel(logging.DEBUG)
        else:
            logging.getLogger('stagehand.web').setLevel(logging.DEBUG)
            logging.getLogger('stagehand.http').setLevel(logging.INFO)

    httplog = logging.getLogger('stagehand.http')
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [HTTP] %(message)s'))
    httplog.addHandler(handler)
    httplog.propagate = False

    if args.background:
        daemonize(chdir=None)

    log.info('starting Stagehand %s', __version__)
    loop = asyncio.get_event_loop()
    mgr = Manager(paths, loop=loop)
    # Take care to start platform plugins before any logging is done.  At least
    # for win32, for reasons yet unclear, it seems that performing any output
    # before the window is created can cause it to fail.
    loop.run_until_complete(platform.start(mgr))

    config.web.add_monitor(functools.partial(web_config_changed, mgr, args))
    asyncio.async(web_start_server(mgr, args))
    asyncio.async(mgr.start())

    if sys.platform != 'win32':
        loop.add_signal_handler(signal.SIGHUP, reload, mgr)
        loop.add_signal_handler(signal.SIGUSR1, resync, mgr)

    try:
        loop.run_forever()
    finally:
        mgr.commit()
        platform.stop()
        # Return manager for interactive interpreter
        return mgr

if __name__ == '__main__':
    m = main()
