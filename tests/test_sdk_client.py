"""Tests for the MiniSky SDK client (minisky/sdk/client.py)."""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import minisky.sdk.client as client_module
from minisky.sdk.client import MiniSkyClient, AsyncMiniSkyClient


def _cluster_response(**overrides):
    data = {
        "cluster_id": "sky-1",
        "name": "c",
        "state": "init",
        "provider": "mock",
        "num_nodes": 1,
        "head_ip": None,
        "launched_at": None,
    }
    data.update(overrides)
    return data


class TestExplicitAutostopZero:
    def test_sync_create_sends_autostop_minutes_zero(self):
        client = MiniSkyClient("http://localhost:8000")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _cluster_response()
        client._client.post = MagicMock(return_value=mock_response)

        client.clusters.create("c", autostop_minutes=0)

        sent_json = client._client.post.call_args.kwargs["json"]
        assert sent_json["autostop_minutes"] == 0

    def test_sync_create_omits_autostop_when_not_given(self):
        client = MiniSkyClient("http://localhost:8000")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = _cluster_response()
        client._client.post = MagicMock(return_value=mock_response)

        client.clusters.create("c")

        sent_json = client._client.post.call_args.kwargs["json"]
        assert "autostop_minutes" not in sent_json


class TestAsyncEnsureClientConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_ensure_client_only_constructs_one_httpx_client(self):
        client = AsyncMiniSkyClient("http://localhost:8000")

        created = []

        class SlowAsyncClient:
            def __init__(self, **kwargs):
                created.append(self)

        with patch.object(client_module.httpx, "AsyncClient", side_effect=SlowAsyncClient):
            await asyncio.gather(*[client._ensure_client() for _ in range(10)])

        assert len(created) == 1
        assert client._client is created[0]


class TestSubscribeTopicFilter:
    @pytest.mark.asyncio
    async def test_events_without_topic_are_skipped_when_filtering(self):
        client = AsyncMiniSkyClient("http://localhost:8000")

        messages = [
            json.dumps({"type": "job_state_change", "payload": {}, "timestamp": "2024-01-01T00:00:00", "topic": "job:abc"}),
            json.dumps({"type": "log_line", "payload": {}, "timestamp": "2024-01-01T00:00:00", "topic": None}),
            json.dumps({"type": "job_state_change", "payload": {}, "timestamp": "2024-01-01T00:00:00", "topic": "job:xyz"}),
        ]

        class FakeWebSocket:
            def __aiter__(self):
                return self._gen()

            async def _gen(self):
                for m in messages:
                    yield m

        class FakeConnect:
            def __call__(self, url):
                return self

            async def __aenter__(self):
                return FakeWebSocket()

            async def __aexit__(self, *args):
                return False

        with patch.object(client_module.websockets, "connect", FakeConnect()):
            received = []
            async for event in client.subscribe(topic="job:abc"):
                received.append(event)

        assert len(received) == 1
        assert received[0].topic == "job:abc"
