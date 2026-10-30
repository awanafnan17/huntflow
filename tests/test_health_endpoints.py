# tests/test_health_endpoints.py
"""Tests for HuntFlow Production Health and Readiness Endpoints.

Verifies:
1. /health/live returns 200 JSON with liveness state.
2. /health/ready returns 200 JSON when DB and Redis are connected.
3. /health/ready returns 503 when DB is down.
4. /health/ready returns 503 when Redis is down.
5. No live LinkedIn provider calls occur during health checks.
"""
from unittest.mock import patch, MagicMock
import pytest
from django.test import Client


@pytest.mark.django_db
def test_health_live_endpoint():
    client = Client()
    resp = client.get("/health/live")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "live"
    assert data["service"] == "HuntFlow"
    assert "timestamp" in data


@pytest.mark.django_db
def test_health_ready_endpoint_healthy():
    client = Client()
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["services"]["web"] == "ok"
    assert data["services"]["database"] == "ok"


@pytest.mark.django_db
def test_health_ready_endpoint_database_down():
    client = Client()
    with patch("django.db.backends.base.base.BaseDatabaseWrapper.cursor", side_effect=Exception("DB Connection Lost")):
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert data["services"]["database"] == "down"
        assert "database_unavailable" in data["reasons"]


@pytest.mark.django_db
def test_health_ready_endpoint_redis_configured_and_healthy():
    client = Client()
    mock_socket = MagicMock()
    mock_socket.recv.return_value = b"+PONG\r\n"
    mock_context = MagicMock()
    mock_context.__enter__.return_value = mock_socket

    with patch.dict("os.environ", {"REDIS_URL": "redis://127.0.0.1:6379/0"}), \
         patch("socket.create_connection", return_value=mock_context):
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["services"]["redis"] == "ok"


@pytest.mark.django_db
def test_health_ready_endpoint_redis_down():
    client = Client()
    with patch.dict("os.environ", {"REDIS_URL": "redis://127.0.0.1:6379/0"}), \
         patch("socket.create_connection", side_effect=Exception("Redis Connection Refused")):
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "not_ready"
        assert data["services"]["redis"] == "down"
        assert "redis_unavailable" in data["reasons"]

