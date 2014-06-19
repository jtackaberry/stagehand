from email.mime.text import MIMEText
import smtplib
import logging
import asyncio

from ..toolbox import tobytes
from .base import NotifierBase, NotifierError
from .email_config import config as modconfig

__all__ = ['Notifier']

log = logging.getLogger('stagehand.notifiers.email')

class Notifier(NotifierBase):
    def _do_smtp(self, mime, recipients):
        """
        Send email over SMTP.

        smtplib uses blocking sockets, so we do this in a thread.
        """
        cls = smtplib.SMTP if not modconfig.ssl else smtplib.SMTP_SSL
        mail = cls(str(modconfig.hostname), int(modconfig.port))
        mail.ehlo_or_helo_if_needed()
        if not modconfig.ssl and mail.has_extn('starttls'):
            mail.starttls()
        if modconfig.username:
            mail.login(modconfig.username, modconfig.password)
        try:
            mail.sendmail(modconfig.sender, recipients, mime.as_string())
        finally:
            mail.quit()


    @asyncio.coroutine
    def _notify(self, episodes):
        # Sanity check configuration
        if '@' not in modconfig.recipients:
            log.error('invalid recipients, skipping email notification')
            return

        summary = u'Summary of Episodes\n'
        overview = u'\nOverview of Episodes\n'
        recipients = [addr.strip() for addr in modconfig.recipients.split(',')]

        for i, ep in enumerate(episodes, 1):
            summary += '%02d: %s %s %s\n' % (i, ep.series.name, ep.code, ep.name)
            overview += '%02d: %s %s %s (%s)\n%s\n\n' % (i, ep.series.name, ep.code, ep.name, ep.airdatetime, ep.overview)

        mime = MIMEText('%s\n%s' % (summary, overview), 'plain', 'utf-8')
        mime['Subject'] = '[stagehand] downloaded %d episodes' % len(episodes)
        mime['From'] = modconfig.sender
        mime['To'] = ', '.join(recipients)

        try:
            yield from self._loop.run_in_executor(None, self._do_smtp, mime, recipients)
        except smtplib.SMTPException as e:
            log.error('unable to send email notification: %s %s', type(e), e)
        else:
            log.info('sent email notification to %s', modconfig.recipients)
