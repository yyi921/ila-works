"""Long admin jobs run in a background thread, so the admin page returns straight away.

Copying a year or two of transactions from Xero and scanning them takes minutes, but the web server stops a request
after 30 seconds (and Cloudflare, in front of ila.works, after 100). So the admin action only claims the job and
starts it; the job writes its progress and outcome onto the Accrual (job_status, job_message), which the Accruals
list shows. One job per organisation at a time; a job that hasn't finished after STALE_AFTER (e.g. its worker was
restarted by a deploy) can be started again.
"""

import logging
import threading
from datetime import timedelta

from django.db import close_old_connections, connections
from django.utils import timezone

from core.models import AuditLog

from .models import Accrual
from .scan import copy_history, default_window, scan

logger = logging.getLogger(__name__)

RUNNING, DONE, FAILED = "running", "done", "failed"
STALE_AFTER = timedelta(minutes=30)


def _in_thread(work):
    def run():
        close_old_connections()
        try:
            work()
        finally:
            connections.close_all()

    threading.Thread(target=run, name="accrual-job").start()


start = _in_thread  # tests run jobs straight away instead


def claim(accrual, message):
    """Mark the accrual's job as running, unless one already is. Returns whether it was claimed."""
    now = timezone.now()
    busy = Accrual.objects.filter(pk=accrual.pk, job_status=RUNNING, job_started_at__gt=now - STALE_AFTER)
    if busy.exists():
        return False
    Accrual.objects.filter(pk=accrual.pk).update(job_status=RUNNING, job_message=message, job_started_at=now,
                                                 job_finished_at=None)
    return True


def _progress(accrual_id, message):
    Accrual.objects.filter(pk=accrual_id).update(job_message=message)


def _finish(accrual_id, status, message):
    Accrual.objects.filter(pk=accrual_id).update(job_status=status, job_message=message,
                                                 job_finished_at=timezone.now())


def copy_and_scan(accrual, *, copy, user=None):
    """Start the job: copy the window from Xero (if ``copy``), then scan. Returns False if one is already running."""
    if not claim(accrual, "Copying from Xero…" if copy else "Scanning…"):
        return False
    accrual_id, user_id = accrual.pk, getattr(user, "pk", None)

    def work():
        done = []
        try:
            row = Accrual.objects.select_related("organisation").get(pk=accrual_id)
            first, last = default_window(row.history_months)
            if copy:
                from xero.source import source_for

                copied = copy_history(row, source_for(row.organisation), first, last)
                done.append(f"{copied.total} transactions read from Xero ({copied.added} new, {copied.updated} "
                            "changed)")
                _progress(accrual_id, "; ".join(done) + ". Scanning…")
            result = scan(row, first, last)
            done.append(f"{len(result.profiles)} profiles learned from {first:%b %Y} to {last:%b %Y}")
            _finish(accrual_id, DONE, "; ".join(done) + ".")
            AuditLog.objects.create(organisation=row.organisation, user_id=user_id, action=AuditLog.Action.SYNC,
                                    input="accruals: copy from Xero and scan" if copy else "accruals: scan",
                                    target_type="accounting.accrual", target_id=str(accrual_id),
                                    result={"message": "; ".join(done)})
        except Exception as exc:  # recorded on the accrual for the admin to see
            logger.exception("Accrual job for accrual %s failed", accrual_id)
            _finish(accrual_id, FAILED, ("; ".join(done) + ". " if done else "") + f"Failed: {exc}")

    start(work)
    return True


def read_bills(accrual, period_end, *, user=None):
    """Start the job: read the PDFs of the bills that came after ``period_end`` (up to today, within the bill window)
    on accrued accounts. Returns False if a job is already running."""
    if not claim(accrual, "Reading bill PDFs…"):
        return False
    accrual_id, user_id = accrual.pk, getattr(user, "pk", None)

    def work():
        from xero.source import source_for

        from .reading import read_bills as read

        try:
            row = Accrual.objects.select_related("organisation").get(pk=accrual_id)
            until = min(timezone.localdate(), period_end + timedelta(days=row.bill_window_days))
            done, failed = read(row, period_end, until, source_for(row.organisation),
                                progress=lambda text: _progress(accrual_id, text))
            message = (f"{done} bill(s) after {period_end:%d %b %Y} read" + (f", {failed} couldn't be read" if failed
                                                                              else "") + ".")
            _finish(accrual_id, DONE, message)
            AuditLog.objects.create(organisation=row.organisation, user_id=user_id, action=AuditLog.Action.SYNC,
                                    input=f"accruals: read bill PDFs after {period_end:%Y-%m-%d}",
                                    target_type="accounting.accrual", target_id=str(accrual_id),
                                    result={"read": done, "failed": failed})
        except Exception as exc:  # recorded on the accrual for the admin to see
            logger.exception("Reading bills for accrual %s failed", accrual_id)
            _finish(accrual_id, FAILED, f"Failed: {exc}")

    start(work)
    return True
