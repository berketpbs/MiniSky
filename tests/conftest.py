"""
Shared pytest fixtures and session-wide safety net.

Several modules (StateManager, MiniSkyConfig, GPUCatalog,
CredentialManager, AutostopAgent, ...) default to reading/writing under
Path.home() / '.minisky' when no explicit path is passed. Some of that
happens at IMPORT time - e.g. minisky/api/server.py builds its module-level
ClusterController/JobController (which now persist via a default
StateManager()) as soon as `from minisky.api.server import app` is
evaluated, which for test modules happens during pytest's collection
phase, before any per-test fixture (autouse or not) has run. A
function-scoped fixture patching Path.home() would be too late to protect
that import.

So this redirects Path.home() for the entire test session, unconditionally,
at conftest.py's own import time - which pytest loads before collecting any
test module. Nothing under test can ever reach the developer's real
~/.minisky/ this way, regardless of whether it's touched at collection,
fixture-setup, or test-body time.
"""

import atexit
import shutil
import tempfile
from pathlib import Path

_TEST_HOME = Path(tempfile.mkdtemp(prefix="minisky-test-home-"))
atexit.register(shutil.rmtree, _TEST_HOME, True)

Path.home = staticmethod(lambda: _TEST_HOME)
