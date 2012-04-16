from __future__ import absolute_import
import logging
import kaa.logger

class Logger(kaa.logger.Logger):
    def makeRecord(self, name, *args, **kwargs):
        if name.startswith('stagehand.'):
            name = name[10:]
        return kaa.logger.Logger.makeRecord(self, name, *args, **kwargs)


logging.setLoggerClass(Logger)
fmt = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
log = logging.getLogger('stagehand').ensureRootHandler(fmt)
