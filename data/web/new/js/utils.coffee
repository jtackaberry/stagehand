@defer = (timeout, callback) -> setTimeout callback, timeout
@cancel = (timer) -> clearTimeout timer
@repeat = (timeout, callback) ->
    id = setInterval ( ->
        if callback() == false
            clearInterval(id)
    ), timeout

class @Stagehand
    constructor: (@root) ->
        @jobs = {}
        @timer = null
        @min_interval = 5000
        @max_interval = 10000
        @handlers = {}
        @poll @min_interval

    bind: (ntype , f) ->
        @handlers[ntype] ?= []
        @handlers[ntype].push(f)


    api: (url, data={}, type='GET') ->
        dfd = $.Deferred()
        xhr = $.ajax(url: @root + url, data: data, type: type.toUpperCase())
            .done (response) =>
                if not response.jobid?
                    # Not a webcoroutine
                    response.xhr = xhr
                    return dfd.resolve(response)

                @jobs[response.jobid] = [dfd, xhr]
                if response.pending
                    dfd.notify response.jobid
                if response.pending and @interval > response.interval
                    # Job is pending and suggested interval from server is
                    # less than what the current interval is, so restart
                    # the poll timer.
                    @poll(response.interval)
                else if @interval > 1000
                    # Even if the job isn't pending, we're not idle, so
                    # increase the poll frequency.
                    @poll(1000)
                @handle_response response

            .fail (xhr, status) =>
                dfd.reject message: "HTTP #{xhr.status}: #{xhr.statusText}", xhr: xhr
        return dfd.promise()


    handle_response: ({jobs, notifications}) ->
        for job in jobs
            if @jobs[job.id]
                [dfd, xhr] = @jobs[job.id]
                delete @jobs[job.id]
                if job.error
                    job.error.xhr = xhr
                    dfd.reject job.error
                else
                    dfd.resolve job.result

        for n in notifications
            if @handlers[n._ntype]?
                for f in @handlers[n._ntype]
                    f(n)

            if n._ntype == 'alert'
                # Initialize alert defaults
                n.type ?= 'notice'
                n.nonblock ?= false
                n.animation ?= 'fade'
                n.closer ?= true
                n.delay ?= 8000
                for key, value of n
                    if typeof value == 'string'
                        # Replace instances of {{root}} in string-based values with
                        # root.  Poor-man's template variable for notifications.
                        value = value.replace(/{{root}}/g, @root)
                    n['pnotify_' + key] = value
                $.pnotify n

    poll: (interval=@interval) ->
        if @timer
            if interval == @interval
                # Timer already running at this interval
                return
            clearInterval @timer

        @interval = if interval <= @max_interval then interval else @max_interval
        @timer = repeat @interval, =>
            # FIXME: need a way to handle timeouts of pending jobs.
            # If there are pending jobs, pass them as a query parameter
            data = if not $.isEmptyObject(@jobs) then {jobs: (jobid for jobid, dfd of @jobs).join(',')} else null
            $.ajax(url: @root + '/api/jobs', data: data, timeout: @interval)
                .done ({jobs, notifications}) =>
                    @handle_response {jobs, notifications}
                    # If we have no active jobs or notifications and we're below the max interval,
                    # then back off.
                    if $.isEmptyObject(@jobs) and notifications.length == 0
                        if @interval < @max_interval
                            @poll @interval * 2
                    else if @interval > @min_interval
                        # There was activity, so drop to the min interval
                        @poll @min_interval

                .fail (xhr, status, error) =>
                    # Some type of error occured (network failure?), so drop to
                    # the max interval straightaway.
                    @poll @max_interval
