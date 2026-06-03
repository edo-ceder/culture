"""Live bridge presence in the dashboard (v9.1.3).

Bridges (v9.0.0-rc.1) are ad-hoc per CC session — they synthesize an
AgentConfig at spawn time and never register in ``~/.culture/server.yaml``.
Pre-v9.1.3 the dashboard's ``list_agents`` / ``list_agents_tree`` walked
the manifest only, so a bridge was provably on the IRCd (WHOIS works) but
invisible to Mission Control.

This file is the contract test for the v9.1.3 merge — every scenario
flagged by the discover/synthesize/critique workflow's correctness +
security + reliability panels has a regression here, plus the load-bearing
fixture that prevents the dev machine's real ``~/.culture/run/`` from
polluting other tests (the critique's blocker #6 — ``test_tree_empty
asserts == {projects: [], peer_bosses: []}``).
"""

from __future__ import annotations

import os

import pytest

from tests._sdk_stub import install_claude_sdk_stub

install_claude_sdk_stub()

from culture.dashboard import server as dashboard_server  # noqa: E402

# ----------------------------------------------------------------------
# Shared fixtures
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_culture_home(tmp_path, monkeypatch):
    """Every test runs against a private ``CULTURE_HOME``. The live
    presence helper honors ``CULTURE_HOME`` (see ``_live_bridge_presence``
    in ``culture/dashboard/server.py``) so this fixture is sufficient to
    keep the real ``~/.culture/run/`` invisible.
    """
    monkeypatch.setenv("CULTURE_HOME", str(tmp_path))
    # Live-presence is default-on; reset the cache so tests don't see
    # each other's data through the resolved-path cache.
    dashboard_server._live_presence_cache_clear()
    yield tmp_path
    dashboard_server._live_presence_cache_clear()


@pytest.fixture
def run_dir(isolated_culture_home):
    """Convenience: the live-presence run dir under the isolated home."""
    p = isolated_culture_home / "run"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_bridge_pid(run_dir, nick: str, pid: int | str) -> None:
    """Write a ``bridge-<nick>.pid`` file with the given PID payload."""
    (run_dir / f"bridge-{nick}.pid").write_text(str(pid))


def _write_manifest(home, agents: list[dict]) -> str:
    """Write a minimal ``server.yaml`` with the given agents and return
    its path so the dashboard handlers can load it."""
    import yaml

    path = home / "server.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server": {"name": "local", "host": "127.0.0.1", "port": 6667},
                "agents": agents,
            }
        )
    )
    return str(path)


# ----------------------------------------------------------------------
# Synthesizer contract
# ----------------------------------------------------------------------


class TestSynthesizeRowShape:
    """The synthesized bridge row must carry every field a manifest row
    carries plus the two new enrichment fields. Frontend destructures
    these names — missing one is a rendering crash."""

    REQUIRED_MANIFEST_FIELDS = {
        "nick",
        "state",
        "pending",
        "last_action",
        "is_boss",
        "boss",
        "idle",
        "channels",
        "last_assistant",
        "last_brief",
        "role",
    }

    NEW_ENRICHMENT_FIELDS = {"bridge_status", "bridge_pid", "live_source"}

    def test_synth_row_has_full_manifest_shape(self):
        row = dashboard_server._synthesize_bridge_row("local-fork", "running", 4242, pending={})
        missing = self.REQUIRED_MANIFEST_FIELDS - row.keys()
        assert not missing, f"synthesized row missing manifest fields: {missing}"

    def test_synth_row_has_enrichment_fields(self):
        row = dashboard_server._synthesize_bridge_row("local-fork", "running", 4242, pending={})
        missing = self.NEW_ENRICHMENT_FIELDS - row.keys()
        assert not missing, f"synthesized row missing enrichment fields: {missing}"

    @pytest.mark.parametrize(
        "bridge_status,expected_state",
        [
            ("running", "running"),
            ("stale", "stopped"),
            ("reused", "stopped"),
            ("broken", "stopped"),
        ],
    )
    def test_state_mapping_honest(self, bridge_status, expected_state):
        """Only ``bridge_status='running'`` → ``state='running'``.
        Anything else maps to ``stopped`` so the dashboard's existing
        state-badge UI tells the truth (the precise classification is in
        ``bridge_status``)."""
        row = dashboard_server._synthesize_bridge_row("local-x", bridge_status, 999, pending={})
        assert row["state"] == expected_state
        assert row["bridge_status"] == bridge_status

    def test_every_bridge_is_a_boss(self):
        """AD-2: every CC session IS a boss. Bridge synth rows therefore
        always carry ``is_boss=True``."""
        for status in ("running", "stale", "reused", "broken"):
            row = dashboard_server._synthesize_bridge_row("local-x", status, 1, pending={})
            assert row["is_boss"] is True
            assert row["boss"] == ""

    def test_pending_perm_count_populated(self):
        row = dashboard_server._synthesize_bridge_row(
            "local-fork", "running", 1, pending={"local-fork": 5}
        )
        assert row["pending"] == 5


# ----------------------------------------------------------------------
# Scan + merge: the six critique scenarios
# ----------------------------------------------------------------------


class TestLivePresenceScan:
    def test_empty_manifest_plus_live_bridge(self, isolated_culture_home, run_dir, monkeypatch):
        """A) Empty manifest + one live bridge → bridge appears as a
        synthesized row."""
        _write_bridge_pid(run_dir, "local-fork", 12345)
        config_path = _write_manifest(isolated_culture_home, agents=[])
        monkeypatch.setattr(dashboard_server, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(dashboard_server, "_pending_counts", lambda: {})
        # Patch the CLI helper's liveness ladder too — that's the helper
        # the dashboard delegates to.
        from culture.cli import bridge as cli_bridge

        monkeypatch.setattr(cli_bridge, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(cli_bridge, "is_culture_process", lambda pid: True)

        rows = dashboard_server.list_agents(config_path)
        assert len(rows) == 1
        assert rows[0]["nick"] == "local-fork"
        assert rows[0]["bridge_status"] == "running"
        assert rows[0]["live_source"] == "bridge_pid"

    def test_manifest_boss_plus_live_bridge_both_present(
        self, isolated_culture_home, run_dir, monkeypatch
    ):
        """B) Same nick in manifest AND on disk as PID file → manifest
        identity wins, but the bridge_status enrichment is added.
        ``live_source`` becomes ``"both"`` so a future UI can show the
        combined provenance."""
        cfg_dir = isolated_culture_home / "agents" / "local-fork"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "culture.yaml").write_text("nick: local-fork\nbackend: claude\ntags: [boss]\n")
        config_path = _write_manifest(
            isolated_culture_home,
            agents=[{"nick": "local-fork", "directory": str(cfg_dir), "tags": ["boss"]}],
        )
        _write_bridge_pid(run_dir, "local-fork", 12345)
        monkeypatch.setattr(dashboard_server, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(dashboard_server, "_pending_counts", lambda: {})
        from culture.cli import bridge as cli_bridge

        monkeypatch.setattr(cli_bridge, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(cli_bridge, "is_culture_process", lambda pid: True)

        rows = dashboard_server.list_agents(config_path)
        assert len(rows) == 1
        row = rows[0]
        # Manifest fields preserved.
        assert row["nick"] == "local-fork"
        # Bridge enrichment added.
        assert row["bridge_status"] == "running"
        assert row["bridge_pid"] == 12345
        assert row["live_source"] == "both"

    def test_manifest_boss_plus_stale_bridge_state_honest(
        self, isolated_culture_home, run_dir, monkeypatch
    ):
        """C) Manifest entry + stale PID file → manifest state preserved
        (could be 'paused' / 'stopped' per ``_agent_state``), but the
        ``bridge_status='stale'`` enrichment is surfaced so the UI can
        explain what's going on."""
        cfg_dir = isolated_culture_home / "agents" / "local-fork"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "culture.yaml").write_text("nick: local-fork\nbackend: claude\n")
        config_path = _write_manifest(
            isolated_culture_home,
            agents=[{"nick": "local-fork", "directory": str(cfg_dir)}],
        )
        _write_bridge_pid(run_dir, "local-fork", 99999)
        monkeypatch.setattr(dashboard_server, "_pending_counts", lambda: {})
        from culture.cli import bridge as cli_bridge

        # Process not alive → stale.
        monkeypatch.setattr(cli_bridge, "is_process_alive", lambda pid: False)

        rows = dashboard_server.list_agents(config_path)
        assert len(rows) == 1
        # Manifest's reported state stays — we did NOT promote to "running".
        # The precise classification is in bridge_status.
        assert rows[0]["bridge_status"] == "stale"

    def test_manifest_only_no_bridge_falls_through(self, isolated_culture_home, monkeypatch):
        """D) Pure manifest, no PID files at all → rows look exactly
        like pre-v9.1.3 (no ``bridge_status`` keys, no synth rows).
        Regression guard: existing tree-shape callers must not break."""
        cfg_dir = isolated_culture_home / "agents" / "local-fork"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "culture.yaml").write_text("nick: local-fork\nbackend: claude\n")
        config_path = _write_manifest(
            isolated_culture_home,
            agents=[{"nick": "local-fork", "directory": str(cfg_dir)}],
        )
        monkeypatch.setattr(dashboard_server, "_pending_counts", lambda: {})

        rows = dashboard_server.list_agents(config_path)
        assert len(rows) == 1
        assert "bridge_status" not in rows[0]
        assert "live_source" not in rows[0]

    def test_irc_unreachable_degrades_gracefully(self, isolated_culture_home, monkeypatch):
        """E) Earlier blueprint considered live IRC scrape; current
        impl is FS-only so this scenario reduces to 'no PID files
        present'. Sanity-checking that the filesystem-only design has
        no implicit IRC dependency — flipping every IRC-shaped patch to
        raise should NOT affect ``list_agents``."""
        config_path = _write_manifest(isolated_culture_home, agents=[])
        monkeypatch.setattr(dashboard_server, "_pending_counts", lambda: {})

        rows = dashboard_server.list_agents(config_path)
        assert rows == []

    def test_bridge_only_collision_manifest_wins(self, isolated_culture_home, run_dir, monkeypatch):
        """F) Critique blocker: a bridge PID file for a nick that ALSO
        has a manifest entry must not produce two rows. Manifest wins
        for identity; bridge is enrichment only."""
        cfg_dir = isolated_culture_home / "agents" / "local-fork"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "culture.yaml").write_text("nick: local-fork\nbackend: claude\n")
        config_path = _write_manifest(
            isolated_culture_home,
            agents=[{"nick": "local-fork", "directory": str(cfg_dir)}],
        )
        _write_bridge_pid(run_dir, "local-fork", 7777)
        monkeypatch.setattr(dashboard_server, "_pending_counts", lambda: {})
        from culture.cli import bridge as cli_bridge

        monkeypatch.setattr(cli_bridge, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(cli_bridge, "is_culture_process", lambda pid: True)

        rows = dashboard_server.list_agents(config_path)
        # Exactly ONE row — not two.
        assert len({r["nick"] for r in rows}) == 1
        assert len(rows) == 1


# ----------------------------------------------------------------------
# Env-var kill switch
# ----------------------------------------------------------------------


class TestKillSwitch:
    """The ``CULTURE_DASHBOARD_LIVE_PRESENCE=0`` rollback (critique
    reliability concern: 'no circuit breaker') must instantly revert to
    pre-v9.1.3 behavior — no PID scan, no synthesized rows, no
    enrichment fields on manifest rows."""

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", ""])
    def test_kill_switch_returns_empty(self, isolated_culture_home, run_dir, monkeypatch, falsy):
        _write_bridge_pid(run_dir, "local-fork", 12345)
        monkeypatch.setenv("CULTURE_DASHBOARD_LIVE_PRESENCE", falsy)
        assert dashboard_server._live_bridge_presence() == {}

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy_values_enable(self, monkeypatch, truthy):
        monkeypatch.setenv("CULTURE_DASHBOARD_LIVE_PRESENCE", truthy)
        assert dashboard_server._live_presence_enabled() is True

    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("CULTURE_DASHBOARD_LIVE_PRESENCE", raising=False)
        assert dashboard_server._live_presence_enabled() is True


# ----------------------------------------------------------------------
# Cache behaviour
# ----------------------------------------------------------------------


class TestCacheBehaviour:
    def test_cache_keyed_by_resolved_run_dir(self, isolated_culture_home, run_dir, monkeypatch):
        """Two different ``CULTURE_HOME`` values must produce two
        independent cache entries — the critique's reliability
        concern about test-time leakage."""
        _write_bridge_pid(run_dir, "local-a", 100)
        from culture.cli import bridge as cli_bridge

        monkeypatch.setattr(cli_bridge, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(cli_bridge, "is_culture_process", lambda pid: True)

        first = dashboard_server._live_bridge_presence()
        assert "local-a" in first

        # Switch CULTURE_HOME to an EMPTY tmp dir; the new run dir is
        # empty and must NOT return the previously-cached "local-a".
        new_home = isolated_culture_home.parent / "other"
        new_home.mkdir(exist_ok=True)
        (new_home / "run").mkdir(exist_ok=True)
        monkeypatch.setenv("CULTURE_HOME", str(new_home))
        second = dashboard_server._live_bridge_presence()
        assert second == {}

    def test_cache_clear_drops_everything(self, run_dir, monkeypatch):
        _write_bridge_pid(run_dir, "local-a", 100)
        from culture.cli import bridge as cli_bridge

        monkeypatch.setattr(cli_bridge, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(cli_bridge, "is_culture_process", lambda pid: True)
        dashboard_server._live_bridge_presence()
        assert len(dashboard_server._LIVE_PRESENCE_CACHE) >= 1
        dashboard_server._live_presence_cache_clear()
        assert len(dashboard_server._LIVE_PRESENCE_CACHE) == 0


# ----------------------------------------------------------------------
# Tree-view integration
# ----------------------------------------------------------------------


class TestTreeIntegration:
    """``list_agents_tree`` must pick up the bridge rows as projects.
    The blueprint did not require a tree-builder change because
    bridges are synthesized with ``is_boss=True`` and ``list_agents_tree``
    already iterates the flat list — but the assertion belongs here as
    a regression guard."""

    def test_bridge_appears_as_project(self, isolated_culture_home, run_dir, monkeypatch):
        _write_bridge_pid(run_dir, "local-payments", 42)
        config_path = _write_manifest(isolated_culture_home, agents=[])
        monkeypatch.setattr(dashboard_server, "_pending_counts", lambda: {})
        from culture.cli import bridge as cli_bridge

        monkeypatch.setattr(cli_bridge, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(cli_bridge, "is_culture_process", lambda pid: True)

        tree = dashboard_server.list_agents_tree(config_path)
        assert len(tree["projects"]) == 1
        project = tree["projects"][0]
        assert project["project_nick"] == "payments"
        assert project["boss"]["nick"] == "local-payments"
        assert project["boss"]["bridge_status"] == "running"
        assert project["workers"] == []

    def test_bridge_with_manifest_workers_groups_correctly(
        self, isolated_culture_home, run_dir, monkeypatch
    ):
        """A manifest worker pointing at a bridge boss must slot under
        that bridge's project — the bridge becomes the parent of
        manifest-registered children."""
        worker_dir = isolated_culture_home / "agents" / "local-qa"
        worker_dir.mkdir(parents=True, exist_ok=True)
        (worker_dir / "culture.yaml").write_text(
            "nick: local-qa\nbackend: claude\nboss: local-payments\n"
        )
        config_path = _write_manifest(
            isolated_culture_home,
            agents=[
                {"nick": "local-qa", "directory": str(worker_dir), "boss": "local-payments"},
            ],
        )
        _write_bridge_pid(run_dir, "local-payments", 42)
        monkeypatch.setattr(dashboard_server, "_pending_counts", lambda: {})
        from culture.cli import bridge as cli_bridge

        monkeypatch.setattr(cli_bridge, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(cli_bridge, "is_culture_process", lambda pid: True)

        tree = dashboard_server.list_agents_tree(config_path)
        assert len(tree["projects"]) == 1
        project = tree["projects"][0]
        assert project["boss"]["nick"] == "local-payments"
        worker_nicks = {w["nick"] for w in project["workers"]}
        assert "local-qa" in worker_nicks

    def test_empty_manifest_empty_run_dir_is_truly_empty(self, isolated_culture_home, run_dir):
        """Critique blocker #6 reframed as a regression test: with
        ``CULTURE_HOME`` isolating the scan, an empty manifest + empty
        run dir must produce ``{projects: [], peer_bosses: []}`` — same
        contract pre-v9.1.3 tests rely on."""
        config_path = _write_manifest(isolated_culture_home, agents=[])
        tree = dashboard_server.list_agents_tree(config_path)
        assert tree == {"projects": [], "peer_bosses": []}
