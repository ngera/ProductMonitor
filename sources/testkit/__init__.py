"""Plugin conformance kit (ADR-0027).

Third-party source authors run this test kit against their plugin to
prove it honors the contract in `sources.base`. The kit is shipped as
part of the main package so it evolves with the codebase — a plugin
that passes today's kit will keep passing until we bump the contract.

Usage in a plugin repo:

    # tests/test_conformance.py
    from sources.testkit import SourceConformanceTests
    from my_plugin import MySource

    class TestMySource(SourceConformanceTests):
        source_class = MySource
        # Minimal stream config accepted by fetch_since; must be enough
        # for the source to run against the fixture below.
        stream_config = {"feed_url": "https://example.com/feed"}
        # Records the source is expected to yield when run against the
        # fixture. See build_fake_transport() for how to wire an
        # httpx.MockTransport into your source's client.
        expected_min_items = 1

Each `test_*` method in `SourceConformanceTests` codifies one line of
the contract. Authors override `stream_config` and (optionally)
`build_source()` to plug their setup in; the tests run unchanged.
"""

from sources.testkit.conformance import SourceConformanceTests
from sources.testkit.fake_http import FakeHTTPTransport

__all__ = ["SourceConformanceTests", "FakeHTTPTransport"]
