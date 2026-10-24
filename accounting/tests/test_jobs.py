from datetime import timedelta
from types import SimpleNamespace

from django.urls import reverse
from django.utils import timezone

from accounting import jobs
from accounting.models import Accrual
from core.models import AuditLog


def _post(admin_client, action, accrual):
    return admin_client.post(reverse("admin:accounting_accrual_changelist"),
                             {"action": action, "_selected_action": [accrual.pk]}, follow=True).content.decode()


def test_copy_and_scan_runs_as_a_job(ledger, admin_client, monkeypatch):
    monkeypatch.setattr("xero.source.source_for", lambda org: "source")
    monkeypatch.setattr("accounting.jobs.copy_history",
                        lambda accrual, source, start, end: SimpleNamespace(total=1240, added=1200, updated=40))
    page = _post(admin_client, "copy_and_scan", ledger.accrual)
    assert "started copying from Xero, then scanning" in page
    accrual = Accrual.objects.get(pk=ledger.accrual.pk)
    assert accrual.job_status == jobs.DONE and accrual.job_finished_at
    assert accrual.job_message.startswith("1240 transactions read from Xero (1200 new, 40 changed); ")
    assert "profiles learned" in accrual.job_message
    assert AuditLog.objects.filter(input="accruals: copy from Xero and scan").exists()


def test_a_failure_is_recorded_not_a_server_error(ledger, admin_client, monkeypatch):
    def boom(org):
        raise RuntimeError("The Xero connection has been disconnected.")

    monkeypatch.setattr("xero.source.source_for", boom)
    page = _post(admin_client, "copy_and_scan", ledger.accrual)
    assert "Failed: The Xero connection has been disconnected." in page
    assert Accrual.objects.get(pk=ledger.accrual.pk).job_status == jobs.FAILED


def test_one_job_at_a_time_unless_stale(ledger, admin_client):
    Accrual.objects.filter(pk=ledger.accrual.pk).update(job_status=jobs.RUNNING, job_message="Copying from Xero…",
                                                        job_started_at=timezone.now())
    page = _post(admin_client, "scan_history", ledger.accrual)
    assert "a job is already running" in page and "Running since" in page
    Accrual.objects.filter(pk=ledger.accrual.pk).update(job_started_at=timezone.now() - timedelta(hours=1))
    assert "Stopped without finishing" in admin_client.get(reverse("admin:accounting_accrual_changelist")).content.decode()
    page = _post(admin_client, "scan_history", ledger.accrual)
    assert "started scanning" in page
    assert Accrual.objects.get(pk=ledger.accrual.pk).job_status == jobs.DONE
