# Requires bgpos.js from https://github.com/brandonaaron/jquery-cssHooks/
# Inspired heavily from http://wp.me/pz02y-cf
$ = jQuery

$.widget 'ui.toggleswitch',
    options:
        peek: 8
        speed: 120
        disabled: null
        yesno: false
        onoff: true

    _is_checked: -> if @element.is(':checked') then 1 else 0

    _update: (speed=@options.speed) ->
        pos = @_shift?[@_is_checked()] or 0
        if speed == 0
            @_span.stop().css backgroundPositionX: pos
        else
            @_span.stop().animate({backgroundPositionX: pos}, speed)

    _getOffShift: ($span=@_span) ->
        offShift = $span.css('backgroundPositionX')
        if offShift == '0%'
            # Opera and IE can't fetch the style of an element before DOM
            # ready.  We can't get backgroundPositionX of @_span because it
            # might have been initialized off.
            $ =>
                $tmp = $('<span class="ui-toggleswitch-container">').css(display: 'none').appendTo('body')
                @_getOffShift($tmp)
                $tmp.detach()
        else
            @_peek = [parseInt(offShift) + @options.peek, -@options.peek]
            @_shift = [parseInt(offShift), 0]
            @_update(0)


    _create: ->
        @options.disabled ?= @element.propAttr 'disabled'
        @options.disabled = true if @element.is(':disabled')

        @_span = @element
        .hide()
        .addClass('ui-toggleswitch')
        .change =>
            @_update()
        .wrap("<span class='ui-toggleswitch-container' tabindex='0'></span>")
        .after('<span></span>')
        .parent()
        .hover(
            => @_span.stop().animate(backgroundPositionX: @_peek[@_is_checked()], 100) if not @options.disabled
            => @_span.stop().animate(backgroundPositionX: @_shift[@_is_checked()], 100) if not @options.disabled
        ).click =>
            if not @options.disabled
                if @_is_checked()
                    @element.removeAttr('checked')
                else
                    @element.attr('checked', 'checked')
                @element.change()
        .on 'selectstart', -> false

        if @options.yesno
            @_span.addClass('ui-toggleswitch-yesno')
        @_getOffShift()
        @_setOption 'disabled', @options.disabled


    _setOption: (key, value) ->
        $.Widget.prototype._setOption.apply this, arguments
        switch key
            when 'disabled'
                value = if value then true else false
                @element.propAttr('disabled', value)
                @_span.stop()
                if value
                    @_span.addClass('ui-state-disabled')
                else
                    @_span.removeClass('ui-state-disabled')

    _destroy: ->
        $.Widget.prototype.destroy.call this

    refresh: -> @_update 0
