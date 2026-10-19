from unittest import mock

import pytest
import requests

from list_sync.api.overseerr import OverseerrClient


def _seerr_with_wrong_key(url, *args, **kwargs):
    # /status is public, so it succeeds no matter which key is sent
    response = requests.Response()
    response.status_code = 200 if url.endswith("/api/v1/status") else 403
    response.url = url
    return response


def test_connection_rejects_wrong_api_key():
    client = OverseerrClient("https://seerr.example", "stale-key")
    with mock.patch("list_sync.api.overseerr.requests.get", side_effect=_seerr_with_wrong_key):
        with pytest.raises(requests.HTTPError):
            client.test_connection()
