"""Tests for ontologia.sensing.git_sensor."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ontologia.sensing.git_sensor import GitSensor


class TestGitSensorAvailability:
    def test_name(self, tmp_path):
        sensor = GitSensor(tmp_path)
        assert sensor.name == "git"

    def test_is_available_reflects_git_binary(self, monkeypatch, tmp_path):
        sensor = GitSensor(tmp_path)

        monkeypatch.setattr("ontologia.sensing.git_sensor.shutil.which", lambda name: "/bin/git")
        assert sensor.is_available() is True

        monkeypatch.setattr("ontologia.sensing.git_sensor.shutil.which", lambda name: None)
        assert sensor.is_available() is False


class TestGitSensorRepoDiscovery:
    def _mark_repo(self, path: Path) -> None:
        (path / ".git").mkdir(parents=True)

    def test_find_repos_walks_visible_directories_to_depth_three(self, tmp_path):
        direct = tmp_path / "repo-direct"
        nested = tmp_path / "organ" / "repo-nested"
        deep = tmp_path / "superproject" / "modules" / "repo-deep"
        hidden_parent = tmp_path / ".hidden" / "repo-hidden"
        hidden_child = tmp_path / "visible" / ".hidden" / "repo-hidden"

        for repo in (direct, nested, deep, hidden_parent, hidden_child):
            self._mark_repo(repo)
        (tmp_path / "not-a-repo").mkdir()

        repos = set(GitSensor(tmp_path)._find_repos())

        assert repos == {direct, nested, deep}


class TestGitSensorRecentCommits:
    def test_recent_commits_runs_expected_git_log_and_strips_output(self, monkeypatch, tmp_path):
        calls = []
        repo = tmp_path / "repo-a"
        repo.mkdir()

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="abc123 Add registry\n\n def456 Fix sensor \n",
            )

        monkeypatch.setattr("ontologia.sensing.git_sensor.subprocess.run", fake_run)

        commits = GitSensor(tmp_path, hours=6)._recent_commits(repo)

        assert commits == ["abc123 Add registry", "def456 Fix sensor"]
        assert calls == [
            (
                [
                    "git",
                    "-C",
                    str(repo),
                    "log",
                    "--oneline",
                    "--since=6 hours ago",
                ],
                {
                    "capture_output": True,
                    "text": True,
                    "timeout": 10,
                    "check": False,
                },
            ),
        ]

    def test_recent_commits_returns_empty_on_nonzero_exit(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo-a"
        repo.mkdir()

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 128, stdout="abc123 ignored\n")

        monkeypatch.setattr("ontologia.sensing.git_sensor.subprocess.run", fake_run)

        assert GitSensor(tmp_path)._recent_commits(repo) == []

    def test_recent_commits_returns_empty_on_subprocess_errors(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo-a"
        repo.mkdir()
        sensor = GitSensor(tmp_path)

        def run_that_raises(error):
            def fake_run(cmd, **kwargs):
                raise error

            return fake_run

        for error in (
            subprocess.TimeoutExpired("git", 10),
            FileNotFoundError("git"),
            OSError("boom"),
        ):
            monkeypatch.setattr(
                "ontologia.sensing.git_sensor.subprocess.run",
                run_that_raises(error),
            )
            assert sensor._recent_commits(repo) == []


class TestGitSensorScan:
    def _mark_repo(self, path: Path) -> None:
        (path / ".git").mkdir(parents=True)

    def test_scan_returns_empty_when_git_is_unavailable(self, monkeypatch, tmp_path):
        sensor = GitSensor(tmp_path)

        monkeypatch.setattr(sensor, "is_available", lambda: False)

        assert sensor.scan() == []

    def test_scan_emits_commit_signals_for_repos_with_recent_commits(self, monkeypatch, tmp_path):
        repo_with_commits = tmp_path / "repo-a"
        repo_without_commits = tmp_path / "repo-b"
        self._mark_repo(repo_with_commits)
        self._mark_repo(repo_without_commits)

        sensor = GitSensor(tmp_path)
        monkeypatch.setattr(sensor, "is_available", lambda: True)
        monkeypatch.setattr(
            sensor,
            "_recent_commits",
            lambda repo_path: (
                ["abc123 Add ontologia tests", "def456 Earlier work"]
                if repo_path == repo_with_commits
                else []
            ),
        )

        signals = sensor.scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.sensor_name == "git"
        assert signal.signal_type == "git_commit"
        assert signal.entity_id == "repo-a"
        assert signal.details == {
            "commit_count": 2,
            "value": "abc123 Add ontologia tests",
            "path": str(repo_with_commits),
        }
        assert signal.timestamp
