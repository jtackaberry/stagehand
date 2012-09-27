from __future__ import absolute_import
import os
import time
import re
import logging
import random
import gc
from datetime import datetime, timedelta
import kaa, kaa.rpc, kaa.config

from . import web, searchers, retrievers, notifiers, providers
from .tvdb import TVDB
from .config import config
from .utils import fixsep
from .searchers import SearcherError
from .retrievers import RetrieverError, RetrieverAborted, RetrieverAbortedHard, RetrieverAbortedSoft
from .notifiers import NotifierError

log = logging.getLogger('stagehand.manager')


class Manager(object):
    def __init__(self, cfgdir=None, datadir=None, cachedir=None):
        self._retrieve_queue_processor_signal = kaa.Signal()
        # A list of episodes to be retrieved.
        # [(Episode, [SearchResult, ...]), (Episode, [SearchResult, ...])]
        self._retrieve_queue = []
        # Like _retrieve_queue, but these are the episodes actively being processed.
        # The first SearchResult is the one being retrieved.
        self._retrieve_queue_active = []
        # Maps Episode objects to InProgress objects for active downloads.
        self._retrieve_inprogress = {}
        self._check_new_timer = kaa.AtTimer(self.check_new_episodes)

        if not datadir:
            # No defaults allowed right now.
            raise ValueError('No data directory given')
        if not cachedir:
            cachedir = os.path.join(os.getenv('XDG_CACHE_HOME', '~/.cache'), 'stagehand')
        if not cfgdir:
            cfgdir = os.path.join(os.getenv('XDG_CONFIG_HOME', '~/.config'), 'stagehand')

        # static web handler does path checking of requests, so make sure
        # datadir is an absolute, not relative path.
        self.datadir = os.path.abspath(os.path.expanduser(datadir))
        self.cachedir = os.path.expanduser(cachedir)
        self.cfgdir = os.path.expanduser(cfgdir)
        self.cfgfile = os.path.join(self.cfgdir, 'config')

        if not os.path.isdir(self.datadir):
            raise ValueError("Data directory %s doesn't exist" % self.datadir)
        if not os.path.exists(self.cfgdir):
            os.makedirs(self.cfgdir)
            config.save(self.cfgfile)
        else:
            config.load(self.cfgfile)
            # If the config schema changed (new version of Stagehand installed?)
            # then write out a new config file.
            config.save(force=False)

        # Monitor config file for changes.
        config.watch()
        config.autosave = True
        config.signals['reloaded'].connect(self._load_config)

        if not os.path.exists(os.path.join(self.cachedir, 'web')):
            os.makedirs(os.path.join(self.cachedir, 'web'))
        if not os.path.exists(os.path.join(self.cachedir, 'logs')):
            os.makedirs(os.path.join(self.cachedir, 'logs'))

        handler = logging.FileHandler(os.path.join(self.cachedir, 'logs', 'stagehand.log'))
        handler.setFormatter(logging.getLogger().handlers[0].formatter)
        logging.getLogger().addHandler(handler)

        handler = logging.FileHandler(os.path.join(self.cachedir, 'logs', 'http.log'))
        handler.setFormatter(logging.getLogger('stagehand.http').handlers[0].formatter)
        logging.getLogger('stagehand.http').addHandler(handler)

        self.tvdb = TVDB(os.path.join(self.cfgdir, 'tv.db'))
        self.rpc = kaa.rpc.Server('stagehand')
        self.rpc.register(self)


    @property
    def retrieve_queue(self):
        """
        The current retrieve queue, in the form:
        [(Episode, [SearchResult, ...]), (Episode, [SearchResult, ...])]

        The first element of this queue is probably active, but you should
        call :meth:`get_episode_retrieve_inprogress` to be sure.
        """
        return self._retrieve_queue_active + self._retrieve_queue


    @kaa.coroutine()
    def _load_config(self, changed=None):
        if changed is not None:
            # if changed is given (even if it's an empty list), it means we've
            # been invoked from the config reloaded signal.
            log.info('config file changed; reloading')
            if 'retrievers.parallel' in changed:
                # Might have increased the number of parallel downloads allowed.
                # Wake up retrieve queue manager.
                self._retrieve_queue_processor_signal.emit_when_handled()
        yield self._load_series_from_config()

        try:
            check_hours = [int(h) for h in config.searchers.hours.split(',')]
        except ValueError:
            log.warning('invalid searchers.hours config value (%s), using default', config.searchers.hours)
            check_hours = [int(h) for h in kaa.config.get_default(config.searchers.hours).split(',')]
        check_min = random.randint(0, 59)
        if self._check_new_timer.hours != tuple(sorted(check_hours)):
            log.info('scheduling checks at %s', ', '.join('%d:%02d' % (hour, check_min) for hour in check_hours))
            self._check_new_timer.start(hour=check_hours, min=check_min)


    @kaa.rpc.expose()
    def shutdown(self):
        kaa.main.stop()

    @kaa.rpc.expose()
    def pid(self):
        return os.getpid()


    @kaa.coroutine()
    def _check_update_tvdb(self):
        servertime = self.tvdb.get_last_updated()
        if servertime and time.time() - float(servertime) > 60*60*24:
            count = yield self.tvdb.sync()
            # FIXME: if count, need to go through all episodes and mark future episodes as STATUS_NEED


    @kaa.coroutine()
    def _add_series_to_db(self, id, fast=False):
        log.info('adding new series %s to database', id)
        series = yield self.tvdb.add_series_by_id(id, fast=fast)
        if not series:
            log.error('provider did not know about %s', id)
            yield None
        log.debug('found series %s (%s) on server', id, series.name)

        # Initialize status for all old episodes as STATUS_IGNORE.  Note that
        # episodes with no airdate can be future episodes, so we mustn't
        # set those to ignore.
        # XXX: changed weeks
        cutoff = datetime.now() - timedelta(weeks=0)
        for ep in series.episodes:
            if ep.airdate and ep.airdate < cutoff:
                ep.status = ep.STATUS_IGNORE

        yield series


    @kaa.coroutine()
    def add_series(self, id):
        """
        Add new series by id, or return existing series if already added.
        """
        series = self.tvdb.get_series_by_id(id)
        if not series:
            series = yield self._add_series_to_db(id, fast=True)
            if not self.tvdb.get_config_for_series(id, series):
                identifier = 'date' if series.has_genre('talk show', 'news') else 'epcode'
                config.series.append(config.series(id=id, path=fixsep(series.name), identifier=identifier))
        yield series


    def delete_series(self, id):
        series = self.tvdb.get_series_by_id(id)
        if not series:
            return
        # Delete from config before we delete from database, since accessing
        # series.cfg indirectly needs the dbrow.
        try:
            config.series.remove(series.cfg)
        except ValueError:
            pass
        self.tvdb.delete_series(series)


    @kaa.coroutine()
    def _load_series_from_config(self):
        """
        Ensure all the TV series in the config are included in the DB.
        """
        seen = set()
        for cfg in config.series:
            try:
                series = self.tvdb.get_series_by_id(cfg.id)
            except ValueError, e:
                log.error('malformed config: %s', e)
                continue

            if not series:
                log.info('discovered new series %s in config; adding to database.', cfg.id)
                try:
                    series = yield self._add_series_to_db(cfg.id)
                except Exception, e:
                    log.exception('failed to add series %s', cfg.id)

                if not series:
                    # Could not be added to DB, probably because it doesn't exist.
                    # _add_series_to_db() will have logged an error about it.
                    continue

            if cfg.path == kaa.config.get_default(cfg.path):
                # Set the path based on the show name explicitly to make the
                # config file more readable.
                cfg.path = fixsep(series.name)

            if cfg.provider != series.provider.NAME:
                if not cfg.provider:
                    cfg.provider = kaa.config.get_default(cfg.provider)
                try:
                    yield series.change_provider(cfg.provider)
                except ValueError, e:
                    log.error('invalid config: %s', e.args[0])

            # Add all ids for this series to the seen list.
            seen.update(series.ids)

        # Check the database for series that aren't in the config.  This indicates the
        # DB is out of sync with config.  Log an error, and mark the series as
        # ignored.
        for series in self.tvdb.series:
            if series.id not in seen:
                log.error('series %s (%s) in database but not config; ignoring', series.id, series.name)
                self.tvdb.ignore_series_by_id(series.id)


    def get_episode_retrieve_inprogress(self, ep):
        """
        Returns the InProgress object for the given Episode object if it is
        currently being retrieved, otherwise returns None.
        """
        return self._retrieve_inprogress.get(ep)


    def is_episode_queued_for_retrieval(self, ep):
        """
        Is the given episode currently queued for retrieval?
        """
        for qep, qresults in self.retrieve_queue:
            if ep == qep:
                return True
        return False


    def cancel_episode_retrieval(self, ep):
        # If the episode is in the pending queue, remove it.
        for qep, results in self._retrieve_queue[:]:
            if ep == qep:
                self._retrieve_queue.remove((qep, results))

        # If the episode is being actively downloaded, abort it.
        ip = self.get_episode_retrieve_inprogress(ep)
        if ip:
            try:
                ip.abort(RetrieverAbortedHard('download cancelled by request'))
            except RetrieverAbortedHard:
                pass


    def _shutdown(self):
        log.info('shutting down')
        config.save(self.cfgfile)


    def _notify_web_retriever_progress(self, progress=None):
        """
        Issues a notification to web clients about the state of the current
        download queue.
        """
        queue = []
        for ep, results in self.retrieve_queue:
            ip = self.get_episode_retrieve_inprogress(ep)
            if ip:
                progress = (
                    ip.progress.percentage,
                    '%.1f' % (ip.progress.pos / 1024.0),
                    '%.1f' % (ip.progress.max / 1024.0),
                    int(ip.progress.speed)
                )
            else:
                progress = None
            queue.append((ep.series.id, ep.code, progress))
        web.notify('dlprogress', queue=queue, replace=True, universal=True)


    @kaa.coroutine()
    def start(self):
        # TODO: randomize time, twice a day
        kaa.Timer(self._check_update_tvdb).start(60*60, now=True)
        kaa.signals['shutdown'].connect_once(self._shutdown)
        #web.notify('alert', title='Global alert', text='Stagehand was restarted')
        yield self._load_config()

        # Start all plugins in parallel
        yield kaa.InProgressAll(searchers.start(self), retrievers.start(self),
                                notifiers.start(self), providers.start(self))

        # Resume downloading any episodes we aborted by adding to retrieve queue.
        for series in self.tvdb.series:
            for ep in series.episodes:
                if ep.ready and ep.search_result:
                    self._retrieve_queue.append((ep, [ep.search_result]))

        # Start the retrieve queue processor
        self._retrieve_queue_processor()


    @kaa.coroutine(policy=kaa.POLICY_SINGLETON)
    def check_new_episodes(self, only=[], force_next=False):
        log.info('checking for new episodes and availability')
        # Get a list of all episodes that are ready for retrieval, building a list by series.
        need = {}
        for series in self.tvdb.series:
            if only and series not in only:
                continue
            needlist = []
            for ep in series.episodes:
                # TODO: force_next: if True, force-add the first STATUS_NEED/NONE episode
                # for the latest season regardless of airdate.  (Gives the user a way
                # to force an update if airdate is not correct on tvdb.)
                if ep.ready and not ep.series.cfg.paused:
                    airdate = ep.airdatetime.strftime('%Y-%m-%d %H:%M') if ep.airdatetime else 'unknown air date'
                    log.debug('need %s %s (%s): %s', series.name, ep.code, airdate, ep.name)
                    if self.is_episode_queued_for_retrieval(ep):
                        log.debug('episode is already queued for retrieval, skipping')
                        log.debug('retrieve queue is %s', self.retrieve_queue)
                    else:
                        needlist.append(ep)
            if needlist:
                need[series] = needlist

        # XXX find a better place for this
        gc.collect()
        if gc.garbage:
            log.warning('uncollectable garbage exists: %s', gc.garbage)

        found = []
        if not need:
            log.info('no new episodes; we are all up to date')
        elif not config.searchers.enabled:
            log.error('episodes require fetching but no searchers are enabled')
        else:
            found = yield self._search_and_retrieve_needed_episodes(need)
        yield need, found


    @kaa.coroutine()
    def _search_and_retrieve_needed_episodes(self, need):
        """
        Go through each series' need list and do a search for the required episodes,
        retrieving them if available.
        """
        episodes_found = []
        for series, episodes in need.items():
            log.info('searching for %d episode(s) of %s', len(episodes), series.name)
            results = yield searchers.search(series, episodes)
            if results:
                # We have results, so add them to the retrieve queue and start
                # the retriever coroutine (which is a no-op if it's already
                # running, due to POLICY_SINGLETON).
                #
                # FIXME: need a way to cache results for a given episode, so that if we
                # restart, we have a way to resume downloads without full searching.
                for ep, ep_results in results.items():
                    # XXX: sanity check: the given episode should not have been searched
                    # for if it was already queued for retrieval.  But I've seen cases
                    # where episodes existed multiple times in the retrieve queue, and it's
                    # not clear how.
                    if self.is_episode_queued_for_retrieval(ep):
                        log.error('BUG: searched for episode %s which is already in retrieve queue %s', ep, 
                                   self.retrieve_queue)
                        continue
                    for r in ep_results:
                        log.debug2('result %s (%dM) (%s)', r.filename, r.size / 1048576.0, r.searcher)
                episodes_found.extend(results.keys())
                self._retrieve_queue.extend(results.items())
                self._retrieve_queue_processor_signal.emit_when_handled()

        log.info('new episode check finished, found %d/%d episodes', len(episodes_found), len(need))
        yield episodes_found


    @kaa.coroutine(policy=kaa.POLICY_SINGLETON)
    def _retrieve_queue_processor(self):
        """
        This coroutine lives forever and monitors the retrieve queue.  If new episodes
        are found on the retrieve queue, it starts as many parallel downloads are allowed
        by config.retrievers.parallel.

        The processor can be "woken up" by emitting _retrieve_queue_processor_signal.
        This signal should be emitted when the queue is changed.
        """
        retrieved = []
        active = []
        while True:
            # Check the retrieve queue.  If the number of active downloads is less than
            # the user-configured parallel limit, then pop from the queue.
            ep = None
            if len(active) < config.retrievers.parallel:
                # Before popping, sort retrieve queue so that result sets with
                # older episodes appear first.  FIXME: ep.airdate could be None
                # which Python 3 won't like sorting.
                self._retrieve_queue.sort(key=lambda (ep, _): ep.airdate)
                try:
                    ep, ep_results = self._retrieve_queue.pop(0)
                except IndexError:
                    # Tried to pop on an empty queue.  We might be done a batch now,
                    # so do notifications if so.
                    if retrieved and not active:
                        self._notify_web_retriever_progress()
                        notifiers.notify(retrieved)
                        retrieved = []

            # If ep is not None, then we have a free download slot and a queued episode.
            if ep:
                # Sanity check.
                if ep.status == ep.STATUS_HAVE:
                    log.error('BUG: scheduled to retrieve %s %s but it is already STATUS_HAVE',
                              ep.series.name, ep.code)
                    continue
                # Check to see if the episode exists locally.
                elif ep.filename and os.path.exists(os.path.join(ep.season.path, ep.filename)):
                    # The episode filename exists.  Do we need to resume?
                    if ep.search_result:
                        # Yes, there is a search result for this episode, so move it to the
                        # front of the result list so it will be tried first.
                        if ep.search_result in ep_results:
                            ep_results.remove(ep.search_result)
                        ep_results = [ep.search_result] + ep_results
                        log.info('resuming download from last search result')
                    else:
                        # XXX: should we move it out of the way and try again?
                        log.error('retriever was scheduled to fetch %s but it already exists, skipping',
                                  ep.filename)
                        continue

                # Add the episode to the active list, start the download by calling _get_episode(),
                # and continue to process additional episodes from the queue (if possible).
                self._retrieve_queue_active.append((ep, ep_results))
                active.append((self._get_episode(ep, ep_results), ep, ep_results))
                continue

            # If we're here, then we've either filled up all the download slots or we have
            # exhausted the queue.  Wait now for any of the active downloads to finish, or
            # for the processor signal to force us to pick up newly enqueued episodes.
            idx, res = yield kaa.InProgressAny([a[0] for a in active] +
                                               [self._retrieve_queue_processor_signal])
            # idx now refers to the positional async task passed to InProgressAny
            # just above.  If idx is the last item, it's the processor signal waking us
            # up, which we now are, so no further action is needed.
            if idx < len(active):
                # A download finished (or errored out).  Remove it from the active lists.
                ip, ep, ep_results = active[idx]
                self._retrieve_queue_active.remove((ep, ep_results))
                active.pop(idx)
                try:
                    # Access the result attribute, which will either succeed
                    # or induce an exception if the async task failed.
                    ip.result
                    # If we got this far without raising, the download finished without
                    # error.
                    retrieved.append(ep)
                except RetrieverAbortedHard:
                    # retrieve() (called by _get_episode()) will already have logged the
                    # abort message, so nothing to do.
                    pass
                except Exception as e:
                    # Some other non-abort related error occured, so log it now.
                    log.error('download failed: %s', e)


    @kaa.coroutine()
    def _get_episode(self, ep, ep_results):
        """
        Initiate the retriever plugin for the given search result.

        On failure, False is returned, and it's up to the caller to retry with
        a different search result.
        """
        if not os.path.isdir(ep.season.path):
            # TODO: handle failure
            os.makedirs(ep.season.path)

        msg = 'Starting retrieval of %s %s' % (ep.series.name, ep.code)
        log.info(msg)
        msg += '<br/><br/>Check progress of <a href="{{root}}/schedule/aired">active downloads</a>.'
        web.notify('alert', title='Episode Download', text=msg)

        for search_result in ep_results:
            # Determine name of target file based on naming preferences.
            if config.naming.rename:
                ext = os.path.splitext(search_result.filename)[-1]
                target = ep.preferred_path + kaa.py3_b(ext.lower())
            else:
                target = os.path.join(ep.season.path, kaa.py3_b(search_result.filename))

            ep.search_result = search_result
            ep.filename = os.path.basename(target)
            try:
                # Inner try block catches any RetrieverError so it can properly
                # clean up any partially fetched file, and then reraises so the
                # outer try block can handle more specific exceptions.
                try:
                    # retrieve() ensures that only RetrieverError is raised
                    ip = retrievers.retrieve(search_result, target, ep)
                    #log.debug('not actually retrieving %s %s', ep.series.name, ep.code)
                    #ip = fake_download(search_result, target, ep)
                    ip.progress.connect(self._notify_web_retriever_progress)
                    self._retrieve_inprogress[ep] = ip
                    yield ip
                except RetrieverError as e:
                    ep.filename = ep.search_result = None
                    if os.path.exists(target):
                        # TODO: handle permission problem
                        log.debug('deleting failed/aborted attempt %s', target)
                        os.unlink(target)
                    # Reraise for outer try block
                    raise
            except RetrieverAbortedSoft:
                # Soft abort: try another result.
                continue
            else:
                # TODO: notify per episode (as well as batches)
                log.info('successfully retrieved %s %s', ep.series.name, ep.code)
                #log.debug('not really')
                ep.status = ep.STATUS_HAVE
                break
            finally:
                del self._retrieve_inprogress[ep]
        else:
            raise RetrieverError('exhausted all search results')


@kaa.coroutine(progress=True)
def fake_download(progress, result, outfile, episode):
    for i in range(100):
        progress.set(i*1*1024, 100*1024, 2048)
        print progress.get_progressbar(), progress.speed
        yield kaa.delay(1)
