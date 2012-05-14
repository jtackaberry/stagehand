from __future__ import absolute_import
from email.mime.text import MIMEText
import smtplib
import logging
import kaa

from .base import NotifierBase, NotifierError
from ..config import config
from .mail_config import config as modconfig

__all__ = ['Notifier']

log = logging.getLogger('stagehand.notifiers.mail')

class Notifier(NotifierBase):


    @kaa.coroutine()
    def _notify(self, episodes):

        summary = "Summary of Episodes\n"
        overview = "\nOverview of Episodes\n" 

        for i, ep in enumerate(episodes):
            summary += "%02d: %s %s %s\n" % (i+1, ep.series.name, ep.code,
                        ep.name)
            overview += "%02d: %s %s %s (%s)\n%s\n" % (i+1, ep.series.name, ep.code, 
                        ep.name, ep.airdate, ep.overview)

        mime = MIMEText("%s\n%s" % (summary, overview))
        mime["Subject"] = "[ stagehand ] downloaded %d episodes" % len(episodes) 
        mime["From"] = modconfig.mail_from
        mime["To"] = modconfig.rcpt_to

        try:
            mail = smtplib.SMTP(modconfig.hostname, modconfig.tcp_port)
            mail.sendmail(modconfig.mail_from, [modconfig.rcpt_to],
                         mime.as_string())
            mail.quit()
        except smtplib.SMTPException, e:
            log.error("Unable to send email notification (%s)" % e)

        yield
