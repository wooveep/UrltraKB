"""Pytest layer selection and fail-fast offline model/network boundaries."""

import ipaddress
import json
import socket
from collections import Counter
from pathlib import Path

import pytest
from layer_policy import LAYERS, MARKERS, classify, default_selection, is_live, needs_native


class OfflineFixtureAccess(pytest.fail.Exception):
    """Offline tests cannot instantiate or reuse the real provider fixture."""


class OfflineNetworkAccess(pytest.fail.Exception):
    """An offline test attempted a non-loopback connection."""


def _item(request):
    # Session SubRequests retain the requesting item, even when node is Session.
    return getattr(request, "_pyfuncitem", request.node)


def require_live_fixture(request, name):
    if name == "live_llm" and not is_live(_item(request)):
        raise OfflineFixtureAccess(
            f"{_item(request).nodeid}: offline test requested live_llm. "
            "Use an explicit live marker for genuine model responses; local "
            "domain preparation must not depend on provider credentials."
        )


def loopback(host):
    if host is None:
        return True  # Passive local address lookup, not an outbound destination.
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if str(host).rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def pytest_addoption(parser):
    group = parser.getgroup("test layers")
    group.addoption(
        "--test-layer",
        choices=["unit", "integration", "live", "offline", "all"],
        help="Execution layer; default is offline unless -m explicitly selects live",
    )
    group.addoption("--test-layer-report", help="Save collected/selected layer evidence as JSON")


def pytest_configure(config):
    from _pytest.fixtures import FixtureRequest

    known = {entry.split(":", 1)[0].strip() for entry in config.getini("markers")}
    for name, description in MARKERS.items():
        if name not in known:
            config.addinivalue_line("markers", f"{name}: {description}")
    patch = pytest.MonkeyPatch()
    # Vendor imports can call load_dotenv during collection, before fixtures.
    # Online credentials still come from explicit dotenv_values(env_path).
    patch.setenv("PYTHON_DOTENV_DISABLED", "1")
    original = FixtureRequest.getfixturevalue

    def guarded_getfixturevalue(request, name):
        # Check before pytest's cached session-fixture fast path as well as setup.
        require_live_fixture(request, name)
        return original(request, name)

    patch.setattr(FixtureRequest, "getfixturevalue", guarded_getfixturevalue)
    config._test_layer_patch = patch
    config._test_layer_rows = []


def pytest_unconfigure(config):
    if patch := getattr(config, "_test_layer_patch", None):
        patch.undo()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    selected = default_selection(config.getoption("test_layer"), config.getoption("markexpr"))
    allowed = (
        LAYERS
        if selected == "all"
        else {"unit", "integration"}
        if selected == "offline"
        else {selected}
    )
    retained, removed = [], []
    for item in items:
        try:
            layer, reason = classify(item)
        except ValueError as error:
            raise pytest.UsageError(str(error)) from error
        if item.get_closest_marker(layer) is None:
            item.add_marker(getattr(pytest.mark, layer))
        native = needs_native(item)
        if native and item.get_closest_marker("native") is None:
            item.add_marker(pytest.mark.native)
        item.user_properties.extend([("test_layer", layer), ("layer_basis", reason)])
        config._test_layer_rows.append(
            {"nodeid": item.nodeid, "layer": layer, "basis": reason, "native": native}
        )
        (retained if layer in allowed else removed).append(item)
    if removed:
        config.hook.pytest_deselected(items=removed)
    items[:] = retained


def pytest_collection_finish(session):
    config = session.config
    rows = config._test_layer_rows
    selected = {item.nodeid for item in session.items}
    config._test_layer_summary = {
        "collected": dict(Counter(row["layer"] for row in rows)),
        "selected": dict(Counter(row["layer"] for row in rows if row["nodeid"] in selected)),
        "native_collected": sum(row["native"] for row in rows),
        "native_selected": sum(row["native"] for row in rows if row["nodeid"] in selected),
        "nodes": [{**row, "selected": row["nodeid"] in selected} for row in rows],
    }
    if target := config.getoption("test_layer_report"):
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config._test_layer_summary, ensure_ascii=False, indent=2))


def pytest_terminal_summary(terminalreporter, config):
    summary = getattr(config, "_test_layer_summary", None)
    if summary:
        terminalreporter.write_line(
            f"Test layers: collected {summary['collected']}; selected {summary['selected']}; "
            f"native {summary['native_selected']}/{summary['native_collected']}. "
            "Offline tests reject real-model fixtures and non-loopback traffic."
        )


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    require_live_fixture(request, fixturedef.argname)


@pytest.fixture(autouse=True)
def _offline_layer_network(request, monkeypatch):
    if is_live(request.node):
        return
    connect, connect_ex, getaddrinfo = (
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.getaddrinfo,
    )

    def check_socket(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6} and not loopback(address[0]):
            raise OfflineNetworkAccess(
                f"{request.node.nodeid}: non-loopback traffic is forbidden in offline tests; "
                "mark genuine external model work live"
            )

    def guarded_connect(sock, address):
        check_socket(sock, address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        check_socket(sock, address)
        return connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not loopback(host):
            raise OfflineNetworkAccess(
                f"{request.node.nodeid}: external DNS lookup is forbidden in offline tests"
            )
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
