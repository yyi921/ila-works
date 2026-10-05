import pytest


@pytest.mark.django_db
def test_healthz_ok_over_plain_http_even_with_ssl_redirect(client, settings):
    settings.SECURE_SSL_REDIRECT = True
    resp = client.get("/healthz/")
    assert resp.status_code == 200
    assert resp.content == b"ok"


@pytest.mark.django_db
def test_other_pages_redirect_to_https(client, settings):
    settings.SECURE_SSL_REDIRECT = True
    resp = client.get("/xero/")
    assert resp.status_code == 301
    assert resp["Location"].startswith("https://")


@pytest.mark.django_db
def test_healthz_accepts_railway_healthcheck_host(client, settings):
    settings.ALLOWED_HOSTS = ["healthcheck.railway.app"]
    assert client.get("/healthz/", HTTP_HOST="healthcheck.railway.app").status_code == 200
