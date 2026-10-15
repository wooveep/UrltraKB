"""Exercise collection, lazy fixture access and real local network boundaries."""

from pathlib import Path

import pytest


@pytest.mark.unit
@pytest.mark.parametrize(
    "host, allowed",
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("api.deepseek.com", False),
        ("203.0.113.1", False),
        ("0.0.0.0", False),
    ],
)
def test_offline_destination_policy(host, allowed):
    from test_layers import loopback

    assert loopback(host) is allowed


@pytest.mark.unit
@pytest.mark.parametrize(
    "expression, expected",
    [
        ("", "offline"),
        ("unit", "offline"),
        ("not live", "offline"),
        ("not native", "offline"),
        ("live", "all"),
        ("live and native", "all"),
    ],
)
def test_paid_selection_requires_explicit_live_intent(expression, expected):
    from layer_policy import default_selection

    assert default_selection(None, expression) == expected
    assert default_selection("offline", expression) == "offline"
    assert default_selection("all", expression) == "all"


@pytest.mark.unit
def test_dynamic_real_fixture_access_fails_before_credentials_are_read(request, monkeypatch):
    import live_llm
    from test_layers import OfflineFixtureAccess

    def forbidden_constructor(*args, **kwargs):
        pytest.fail("Offline fixture access reached provider construction")

    monkeypatch.setattr(live_llm, "LiveLLM", forbidden_constructor)
    for _ in range(2):
        with pytest.raises(OfflineFixtureAccess, match="offline test requested live_llm"):
            request.getfixturevalue("live_llm")


@pytest.mark.integration
def test_offline_guard_rejects_external_connect_and_dns():
    import socket

    from test_layers import OfflineNetworkAccess

    with socket.socket() as connection:
        with pytest.raises(OfflineNetworkAccess, match="non-loopback traffic"):
            connection.connect(("203.0.113.1", 443))
        with pytest.raises(OfflineNetworkAccess, match="non-loopback traffic"):
            connection.connect_ex(("203.0.113.1", 443))
    with pytest.raises(OfflineNetworkAccess, match="external DNS"):
        socket.getaddrinfo("api.deepseek.com", 443)


@pytest.mark.integration
def test_offline_guard_allows_real_local_error_http():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx

    class LocalError(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"local request rejected")

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalError)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        response = httpx.get(f"http://127.0.0.1:{server.server_port}/contract", trust_env=False)
        assert response.status_code == 400 and response.text == "local request rejected"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def _configure_plugin(pytester, *, model_service=False):
    # A real child process does not inherit pytester's in-process sys.path.
    plugins = (
        ("test_layers", "model_service_fixtures", "live_llm") if model_service else ("test_layers",)
    )
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        f"pytest_plugins = {plugins!r}"
    )


def _suite(pytester):
    _configure_plugin(pytester)
    pytester.makepyfile(
        """
        import pytest
        @pytest.mark.unit
        def test_rule():
            assert 1 + 1 == 2
        @pytest.mark.integration
        @pytest.mark.native
        def test_local_state(tmp_path):
            (tmp_path / "state").write_text("retained")
            assert (tmp_path / "state").read_text() == "retained"
        @pytest.mark.live
        def test_provider_work_must_be_explicit():
            pytest.fail("The default selection executed live work")
        """
    )


@pytest.mark.integration
def test_default_selection_runs_offline_and_reports_live_deselection(pytester):
    _suite(pytester)
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=2, deselected=1)
    result.stdout.fnmatch_lines(["*Test layers:*selected*unit*integration*"])


@pytest.mark.integration
@pytest.mark.parametrize("selection", [("-m", "live"), ("--test-layer=live",)])
def test_live_can_be_selected_without_executing_it(pytester, selection):
    _suite(pytester)
    result = pytester.runpytest_subprocess("--collect-only", "-q", *selection)
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*test_provider_work_must_be_explicit*", "*1/3 tests collected*"])


@pytest.mark.integration
def test_conflicting_layers_are_collection_errors(pytester):
    _configure_plugin(pytester)
    pytester.makepyfile(
        """
        import pytest
        @pytest.mark.unit
        @pytest.mark.live
        def test_conflict():
            pass
        """
    )
    result = pytester.runpytest_subprocess("--collect-only", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*conflicting test layers*"])


@pytest.mark.integration
def test_indirect_model_service_cannot_read_credentials_in_offline_layer(pytester):
    _configure_plugin(pytester)
    pytester.makepyfile(
        """
        import pytest
        @pytest.fixture
        def live_llm():
            pytest.fail("Credential lookup must never run")
        @pytest.fixture
        def model_service(live_llm):
            pytest.fail("Real model-service setup must never run")
        @pytest.mark.integration
        def test_local(model_service):
            pass
        """
    )
    result = pytester.runpytest_subprocess("--collect-only", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*integration depends on live_llm*"])


@pytest.mark.integration
@pytest.mark.parametrize("include_source", [False, True])
def test_global_model_fixture_resolves_single_consumer_and_with_source_module(
    pytester, include_source
):
    import json

    _configure_plugin(pytester, model_service=True)
    with (pytester.path / "conftest.py").open("a") as configuration:
        configuration.write(
            """
import json
from pathlib import Path
def pytest_collection_finish(session):
    for item in session.items:
        if item.name == 'test_consumer':
            definitions = item._fixtureinfo.name2fixturedefs
            Path('resolution.json').write_text(json.dumps({
                'live': item.get_closest_marker('live') is not None,
                'provider': [fd.func.__module__ for fd in definitions.get('model_service', ())],
                'live_dependency': bool(definitions.get('live_llm')),
            }))
"""
        )
    consumer = pytester.makepyfile(
        test_consumer="""
        import pytest
        def test_consumer(model_service):
            pytest.fail('Collection verification must not execute provider work')
        """
    )
    paths = [str(consumer)]
    if include_source:
        source = pytester.makepyfile(
            test_vendor_source=(Path(__file__).with_name("test_vendor_sdk.py")).read_text()
        )
        paths.append(str(source))
    result = pytester.runpytest_subprocess("--collect-only", "-q", "-m", "live", *paths)
    assert result.ret == 0
    resolution = json.loads((pytester.path / "resolution.json").read_text())
    assert resolution["live"] and resolution["live_dependency"]
    assert resolution["provider"] and set(resolution["provider"]) == {"model_service_fixtures"}


@pytest.mark.integration
@pytest.mark.parametrize("name", ["model_service", "_model"])
def test_unresolved_model_fixture_is_conservatively_live_at_collection(pytester, name):
    _configure_plugin(pytester)
    pytester.makepyfile(
        f"def test_consumer({name}):\n"
        "    raise AssertionError('Collection must not execute model work')\n"
    )
    result = pytester.runpytest_subprocess("--collect-only", "-q", "-m", "live")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*test_consumer*", "*1 test collected*"])


@pytest.mark.integration
def test_collection_cannot_autoload_credentials_but_explicit_env_reading_still_works(
    pytester, monkeypatch
):
    # The child must establish its own boundary, without inheriting one from this run.
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("OFFLINE_UNEXPECTED_SECRET", raising=False)
    _configure_plugin(pytester)
    (pytester.path / ".env").write_text("OFFLINE_UNEXPECTED_SECRET=explicit-fixture-only\n")
    pytester.makepyfile(
        """
        import os
        import pytest
        from dotenv import dotenv_values, load_dotenv

        # This is the same collection/import boundary used by vendor packages.
        assert load_dotenv() is False

        @pytest.mark.integration
        def test_explicit_source():
            assert 'OFFLINE_UNEXPECTED_SECRET' not in os.environ
            assert dotenv_values('.env')['OFFLINE_UNEXPECTED_SECRET'] == 'explicit-fixture-only'
            assert 'OFFLINE_UNEXPECTED_SECRET' not in os.environ
        """
    )
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1)
