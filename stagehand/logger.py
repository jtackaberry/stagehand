import logging

class Logger(logging.Logger):
    def makeRecord(self, name, *args, **kwargs):
        if name.startswith('stagehand.'):
            name = name[10:]
        return super().makeRecord(name, *args, **kwargs)

    def debug2(self, msg, *args, **kwargs):
        if self.isEnabledFor(logging.DEBUG2):
            self._log(logging.DEBUG2, msg, args, **kwargs)

logging.DEBUG2 = 5
logging.addLevelName(logging.DEBUG2, 'DEBUG2')
logging.setLoggerClass(Logger)
logging.basicConfig(format='%(asctime)s [%(levelname)s] %(name)s: %(message)s', level=logging.WARN)
