import os
import time
import re
import logging
import random
import gc
import asyncio
from datetime import datetime, timedelta

from . import web, searchers, retrievers, notifiers, providers
from .toolbox import FutureProgress, singleton
from .toolbox.utils import fsname
from .toolbox.dateutils import to_timestamp
from .toolbox.config import get_default
from .tvdb import TVDB
from .config import config
from .utils import fixsep
from .searchers import SearcherError
from .retrievers import RetrieverError, RetrieverSoftError, RetrieverHardError, RetrieverAborted, RetrieverAbortedHard, RetrieverAbortedSoft
from .notifiers import NotifierError

log = logging.getLogger('stagehand.manager')


class Manager:
    def __init__(self, paths, *, loop=None):
        super().__init__()
        self.paths = paths
        self.loop = loop or asyncio.get_event_loop()
        self._retrieve_queue_event = asyncio.Event()
        # A list of episodes to be retrieved.
        # [(Episode, [SearchResult, ...]), (Episode, [SearchResult, ...])]
        self._retrieve_queue = []
        # Like _retrieve_queue, but these are the episodes actively being processed.
        # The first SearchResult is the one being retrieved.
        self._retrieve_queue_active = []
        # Maps Episode objects to InProgress objects for active downloads.
        self._retrieve_tasks = {}
        self._next_episode_check_timer  = None

        # If the config schema changed (new version of Stagehand installed?)
        # then write out a new config file.
        config.save(paths.config, force=not os.path.exists(paths.config))

        # Monitor config file for changes.
        config.watch()
        config.autosave = True
        config.signals['reloaded'].connect(self._load_config)

        self.tvdb = TVDB(paths.db, loop=self.loop)


    @asyncio.coroutine
    def start(self):
        """
        Starts the Stagehand manager, which starts all plugins and
        schedules tasks for
        """
        yield from self._load_config()

        # TODO: randomize time, twice a day
        self.loop.call_soon(asyncio.async, self._check_update_tvdb())
        #web.notify('alert', title='Global alert', text='Stagehand was restarted')

        self._schedule_next_episode_check(skip_current_hour=False)
        yield from self.check_new_episodes()

        # Start all plugins in parallel
        yield from asyncio.gather(searchers.start(self), retrievers.start(self),
                                  notifiers.start(self), providers.start(self))

        # Resume downloading any episodes we aborted by adding to retrieve queue.
        log.info('checking all epsiodes to see if any need resuming')
        for series in self.tvdb.series:
            for ep in series.episodes:
                if ep.ready and ep.search_result:
                    self._retrieve_queue.append((ep, [ep.search_result]))
            # For a large number of series, this loop (which involves reading
            # all episodes from the database) can take some time.  So yield
            # back to the main loop so we don't starve other tasks.
            yield

        # Start the retrieve queue processor
        self._retrieve_queue_processor()



    @asyncio.coroutine
    def _load_config(self, changed=None):
        if changed is not None:
            # if changed is given (even if it's an empty list), it means we've
            # been invoked from the config reloaded signal.
            log.info('config file changed; reloading')
            if 'retrievers.parallel' in changed:
                # Might have increased the number of parallel downloads allowed.
                # Wake up retrieve queue manager.
                self._retrieve_queue_processor_signal.emit_when_handled()
        yield from self._load_series_from_config()



    def _schedule_next_episode_check(self, skip_current_hour=True):
        try:
            hours = sorted(int(h) for h in config.searchers.hours.split(','))
        except ValueError:
            log.warning('invalid searchers.hours config value (%s), using default', config.searchers.hours)
            hours = sorted(int(h) for h in get_default(config.searchers.hours).split(','))

        # Pick a random minute of the hour for the next check.
        randmin = random.randint(0, 59)
        t = datetime.now().replace(second=0, microsecond=0)
        # Which future hours are eligible?
        next_hour = [h for h in hours if h > t.hour or (not skip_current_hour and h == t.hour and randmin > t.minute)]
        if next_hour:
            next = t.replace(hour=next_hour[0], minute=randmin)
        else:
            # No schedulable hour in the future for the current day, so advance to tomorrow.
            tmrw = t + timedelta(days=1)
            next = tmrw.replace(hour=hours[0], minute=randmin)

        log.info('scheduling next episode check for %s', next)
        if self._next_episode_check_timer:
            self._next_episode_check_timer.cancel()
        delta = to_timestamp(next) - time.time()
        handle = self.loop.call_at(self.loop.time() + delta, asyncio.async,
                                    self.check_new_episodes(reschedule=True))
        self._next_episode_check_timer = handle


    @property
    def retrieve_queue(self):
        """
        The current retrieve queue, in the form:
        [(Episode, [SearchResult, ...]), (Episode, [SearchResult, ...])]

        The first element of this queue is probably active, but you should
        call :meth:`get_episode_retrieve_task` to be sure.
        """
        return self._retrieve_queue_active + self._retrieve_queue



    @asyncio.coroutine
    def _check_update_tvdb(self):
        servertime = self.tvdb.get_last_updated()
        if servertime and time.time() - float(servertime) > 60*60*24:
            count = yield from self.tvdb.sync()
        self.loop.call_later(4*60*60, asyncio.async, self._check_update_tvdb())
        # FIXME: if count, need to go through all episodes and mark future episodes as STATUS_NEED


    @asyncio.coroutine
    def _add_series_to_db(self, id, fast=False):
        log.info('adding new series %s to database', id)
        series = yield from self.tvdb.add_series_by_id(id, fast=fast)
        if not series:
            log.error('provider did not know about %s', id)
            return
        log.debug('found series %s (%s) on provider', id, series.name)

        # Initialize status for all old episodes as STATUS_IGNORE.  Note that
        # episodes with no airdate can be future episodes, so we mustn't
        # set those to ignore.
        # XXX: changed weeks
        cutoff = datetime.now() - timedelta(weeks=0)
        for ep in series.episodes:
            if ep.airdate and ep.airdate < cutoff:
                ep.status = ep.STATUS_IGNORE

        return series


    @asyncio.coroutine
    def add_series(self, id):
        """
        Add new series by id, or return existing series if already added.
        """
        series = self.tvdb.get_series_by_id(id)
        if not series:
            series = yield from self._add_series_to_db(id, fast=True)
            if not self.tvdb.get_config_for_series(id, series):
                identifier = 'date' if series.has_genre('talk show', 'news') else 'epcode'
                config.series.append(config.series(id=id, path=fixsep(series.name), identifier=identifier))
        return series


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


    @asyncio.coroutine
    def _load_series_from_config(self):
        """
        Ensure all the TV series in the config are included in the DB.
        """
        seen = set()
        for cfg in config.series:
            try:
                series = self.tvdb.get_series_by_id(cfg.id)
            except ValueError as e:
                log.error('malformed config: %s', e)
                continue

            if not series:
                log.info('discovered new series %s in config; adding to database.', cfg.id)
                try:
                    series = yield from self._add_series_to_db(cfg.id)
                except Exception as e:
                    log.exception('failed to add series %s', cfg.id)

                if not series:
                    # Could not be added to DB, probably because it doesn't exist.
                    # _add_series_to_db() will have logged an error about it.
                    continue

            if cfg.path == get_default(cfg.path):
                # Set the path based on the show name explicitly to make the
                # config file more readable.
                cfg.path = fixsep(series.name)

            if cfg.provider != series.provider.NAME:
                if not cfg.provider:
                    cfg.provider = get_default(cfg.provider)
                try:
                    yield from series.change_provider(cfg.provider)
                except ValueError as e:
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
            # Loop over episodes as well just to warm the cache
            for ep in series.episodes:
                pass

    def get_episode_retrieve_task(self, ep):
        """
        Returns the Future object for the given Episode object if it is
        currently being retrieved, otherwise returns None.
        """
        return self._retrieve_tasks.get(ep)


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
        task = self.get_episode_retrieve_task(ep)
        if task:
            task.cancel()


    def commit(self):
        log.info('shutting down')
        config.save(self.paths.config)
        self.tvdb.commit()


    def _notify_web_retriever_progress(self, progress=None):
        """
        Issues a notification to web clients about the state of the current
        download queue.
        """
        queue = []
        for ep, results in self.retrieve_queue:
            task = self.get_episode_retrieve_task(ep)
            if task:
                progress = (
                    task.progress.percentage,
                    '%.1f' % (task.progress.pos / 1024.0 / 1024.0),
                    '%.1f' % (task.progress.max / 1024.0 / 1024.0),
                    int(task.progress.speed / 1024.0)
                )
            else:
                progress = None
            queue.append((ep.series.id, ep.code, progress))
        web.notify('dlprogress', queue=queue, replace=True, universal=True)



    @asyncio.coroutine
    @singleton
    def check_new_episodes(self, only=[], force_next=False, reschedule=False):
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
            found = yield from self._search_and_retrieve_needed_episodes(need)

        if reschedule:
            self._schedule_next_episode_check()
        return need, found


    @asyncio.coroutine
    def _search_and_retrieve_needed_episodes(self, need):
        """
        Go through each series' need list and do a search for the required episodes,
        retrieving them if available.
        """
        episodes_found = []
        for series, episodes in need.items():
            log.info('searching for %d episode(s) of %s', len(episodes), series.name)
            results = yield from searchers.search(series, episodes)
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
                    log.debug('adding %s %s to retrieve queue with %d results',
                              series.name, ep.code, len(ep_results))
                episodes_found.extend(results.keys())
                self._retrieve_queue.extend(results.items())
                self._retrieve_queue_event.set()

        need_count = sum(len(l) for l in need.values())
        log.info('new episode check finished, found %d/%d episodes', len(episodes_found), need_count)
        return episodes_found


    @singleton
    @asyncio.coroutine
    def _retrieve_queue_processor(self):
        while True:
            try:
                yield from self._do_retrieve_queue_processor()
            except Exception:
                log.exception('retrieve queue processor aborted, respawning')
                yield from asyncio.sleep(1)
            else:
                break


    @asyncio.coroutine
    def _do_retrieve_queue_processor(self):
        """
        This coroutine lives forever and monitors the retrieve queue.  If new episodes
        are found on the retrieve queue, it starts as many parallel downloads are allowed
        by config.retrievers.parallel.

        The processor can be "woken up" by setting _retrieve_queue_event.  This
        event should be set when the queue is changed.
        """
        retrieved = []
        active = {}
        log.debug('starting retrieve queue processor')
        while True:
            # Check the retrieve queue.  If the number of active downloads is less than
            # the user-configured parallel limit, then pop from the queue.
            ep = None
            if len(active) < config.retrievers.parallel:
                # Before popping, sort retrieve queue so that result sets with
                # older episodes appear first.
                self._retrieve_queue.sort(key=lambda item: (item[0].airdate or datetime.now(), item[0].code))
                try:
                    ep, ep_results = self._retrieve_queue.pop(0)
                except IndexError:
                    # Tried to pop on an empty queue.  We might be done a batch now,
                    # so do notifications if so.
                    if retrieved and not active:
                        self._notify_web_retriever_progress()
                        asyncio.async(notifiers.notify(retrieved))
                        retrieved = []
                else:
                    log.debug2('popped %s from retrieve queue', ep)

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
                log.debug2('spawning episode retrieval task')
                self._retrieve_queue_active.append((ep, ep_results))
                task = asyncio.Task(self._get_episode(ep, ep_results))
                active[task] = ep, ep_results
                continue

            # If we're here, then we've either filled up all the download slots or we have
            # exhausted the queue.  Wait now for any of the active downloads to finish, or
            # for the processor event to force us to pick up newly enqueued episodes.
            tasks = list(active.keys()) + [self._retrieve_queue_event.wait()]
            log.debug2('retrieve queue processor waiting for any of %d tasks', len(tasks))
            done, pending = yield from asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            log.debug2('retrieve queue processor woke up, done=%d pending=%d', len(done), len(pending))
            # Just blindly clear the retrieve queue event.  The point of the
            # event is to wake us up, which we now are.  If it's already unset,
            # then this is a no-op.
            self._retrieve_queue_event.clear()

            for task in done:
                if task not in active:
                    # This task must have been the retrieve queue even.  Ignore.
                    continue

                # A download finished (or errored out).  Remove it from the active lists.
                ep, ep_results = active.pop(task)
                self._retrieve_queue_active.remove((ep, ep_results))
                # Now that the episode has been removed from the retrieve
                # queue, send an updated notification so the web client sees
                # the epsiode has been removed from the dlprogress list.
                self._notify_web_retriever_progress()
                try:
                    # Accessing result() here will raise any exceptions that
                    # might have occurred in the task.  If it doesn't raise
                    # and returns True then retrieval was successful.  If it
                    # doesn't return True then none of the retrievers could
                    # get the episode.
                    if task.result():
                        retrieved.append(ep)
                except RetrieverAbortedHard:
                    # Result was aborted by user, so do nothing.
                    pass
                except Exception as e:
                    # Some other non-abort related error occured, so log it now.
                    strerror = str(e).split('\n')[-1]
                    log.error('download failed: %s', strerror)
                    msg = 'Download failed with an unrecoverable error: %s.' + \
                          ' Intervention is needed.  Check the logs for more details.'
                    web.notify('alert', title='Download Failed', text=msg % strerror, type='error')


    @asyncio.coroutine
    def _get_episode(self, ep, ep_results):
        """
        Initiate the retriever plugin for the given search result.

        :returns: True if the episode was successfully retrieved, or False if no
                  retrievers were capable of fetching any of the search results.
        :raises: RetrieverHardError if a more serious error occurred with one
                 of the retrievers.
        """
        if not os.path.isdir(ep.season.path):
            # TODO: handle failure
            os.makedirs(ep.season.path)

        msg = 'starting retrieval of %s %s' % (ep.series.name, ep.code)
        log.info(msg)
        msg += '<br/><br/>Check progress of <a href="{{root}}/schedule/aired">active downloads</a>.'
        web.notify('alert', title='Episode Download', text=msg)

        for search_result in ep_results:
            # Determine name of target file based on naming preferences.
            if config.naming.rename:
                ext = os.path.splitext(search_result.filename)[-1]
                target = ep.preferred_path + fsname(ext.lower())
            else:
                target = os.path.join(ep.season.path, fsname(search_result.filename))

            ep.search_result = search_result
            ep.filename = os.path.basename(target)
            try:
                # Inner try block catches any RetrieverError so it can properly
                # clean up any partially fetched file, and then reraises so the
                # outer try block can handle more specific exceptions.
                try:
                    # retrieve() ensures that only RetrieverError is raised
                    progress = FutureProgress()
                    if 1:
                        task = asyncio.Task(retrievers.retrieve(progress, search_result, target, ep))
                    else:
                        log.debug('not actually retrieving %s %s', ep.series.name, ep.code)
                        task = asyncio.Task(fake_download(progress, search_result, target, ep))
                    task.progress = progress
                    progress.connect(self._notify_web_retriever_progress)
                    self._retrieve_tasks[ep] = task
                    yield from task
                except RetrieverError as e:
                    ep.filename = ep.search_result = None
                    if os.path.exists(target):
                        # TODO: handle permission problem
                        log.debug('deleting failed/aborted attempt %s', target)
                        os.unlink(target)
                    # Reraise for outer try block
                    raise
            except RetrieverSoftError:
                # Soft error (including soft abort): try another result.
                continue
            else:
                # TODO: notify per episode (as well as batches)
                log.info('successfully retrieved %s %s', ep.series.name, ep.code)
                #log.debug('not really')
                ep.status = ep.STATUS_HAVE
                return True
            finally:
                # ep may not be in _retrieve_tasks if e.g. retrieve() raises
                # an exception.
                if ep in self._retrieve_tasks:
                    del self._retrieve_tasks[ep]

        return False

@asyncio.coroutine
def fake_download(progress, result, outfile, episode):
    for i in range(100):
        progress.set(i*1*1024*1024, 100*1024*1024, 2048)
        print(progress.get_ascii_bar(), progress.speed)
        yield from asyncio.sleep(1)
