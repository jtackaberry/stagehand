/*! Copyright (c) 2010 Brandon Aaron (http://brandonaaron.net)
 * Licensed under the MIT License (LICENSE.txt).
 *
 * Modified by tack: Webkit fixes.
 */
(function($) {
    // backgroundPosition[X,Y] get hooks
    var $div = $('<div style="background-position: 3px 5px">');
    $.support.backgroundPosition   = $div.css('backgroundPosition')  === "3px 5px" ? true : false;
    /* Chrome returns backgroundPosition adjusted by zoom level.
     * http://bugs.jquery.com/ticket/9880
     * 2012-09-27: I don't know when this changed, but at least as of Chrome 22
     * (Webkit 537.1), backgroundPosition is no longer zoom-adjusted.
     */
    $.support.backgroundPositionZoomAdj = $.browser.webkit && $.browser.version < 537;
    $div = null;

    var xy = ["X","Y"];

    // helper function to parse out the X and Y values from backgroundPosition
    function parseBgPos(bgPos) {
        var parts  = bgPos.split(/\s/),
            values = {
                "X": parts[0],
                "Y": parts[1]
            };
        return values;
    }
    function round(v) {
        return v < 0 ? Math.floor(v) : Math.ceil(v);
    }

    if ($.support.backgroundPosition) {
        $.each(xy, function( i, l ) {
            $.cssHooks[ "backgroundPosition" + l ] = {
                get: function( elem, computed, extra ) {
                    var values = parseBgPos( $.css(elem, "backgroundPosition") );
                    if (!$.support.backgroundPositionZoomAdj)
                        return values[l];
                    else {
                        var zoom = document.width / $(document).width();
                        return round(parseInt(values[l]) / zoom);
                    }
                },
                set: function( elem, value ) {
                    var values = parseBgPos( $.css(elem, "backgroundPosition") ),
                        isX = l === "X";
                    elem.style.backgroundPosition = (isX ? value : values[ "X" ]) + " " +
                                                    (isX ? values[ "Y" ] : value);
                }
            };
            $.fx.step[ "backgroundPosition" + l ] = function( fx ) {
                $.cssHooks[ "backgroundPosition" + l ].set( fx.elem, fx.now + fx.unit );
            };
        });
    }
})(jQuery);
