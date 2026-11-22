import asyncio

import pytest

from app.config import get_settings
from app.handlers import PermanentError, check_url, run_handler


@pytest.mark.parametrize("url", [
    "http://localhost:8000/admin",
    "http://127.0.0.1/",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata endpoint
    "http://[::1]/",
])
def test_ssrf_guard_blocks_internal_addresses(url):
    with pytest.raises(PermanentError, match="SSRF"):
        check_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "not a url", "http://"])
def test_rejects_non_http_urls(url):
    with pytest.raises(PermanentError):
        check_url(url)


def test_allowed_hosts_bypass(monkeypatch):
    monkeypatch.setenv("HTTP_ALLOWED_HOSTS", "localhost, api")
    get_settings.cache_clear()
    try:
        check_url("http://localhost:8000/health")  # no exception
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("payload,error", [
    ({"method": "TRACE", "url": "https://example.com"}, "method"),
    ({"url": "https://example.com", "headers": ["x"]}, "headers"),
    ({"url": "https://example.com", "expected_status": "200"}, "expected_status"),
    ({"url": "http://127.0.0.1"}, "SSRF"),
])
def test_http_request_validation(payload, error):
    with pytest.raises(PermanentError, match=error):
        asyncio.run(run_handler("http_request", payload))
