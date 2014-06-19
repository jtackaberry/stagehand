import logging
import os
import time
#from . import bottle
from .bottle import request, response, HTTPError, TEMPLATE_PATH, static_file
#from .bottle import (app, route, view, request, response, TEMPLATE_PATH, TEMPLATES,
#                     static_file, abort, redirect, debug, cookie_encode, install,
#                     cookie_decode, cookie_is_encoded, HTTPError, SimpleTemplate,
#                     json_dumps)

from .wsgi import Server, log

_s = Server()
start = _s.start
stop = _s.stop
get = _s.app.get
post = _s.app.post
put = _s.app.put
delete = _s.app.delete
install = _s.app.install