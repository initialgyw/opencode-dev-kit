from __future__ import annotations

import contextlib
import http.server
import json
import socketserver
import stat
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import urljoin

import install


class QuietDirectoryHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


@contextlib.contextmanager
def directory_server(root: Path):
    handler = lambda *args, **kwargs: QuietDirectoryHandler(*args, directory=str(root), **kwargs)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class InstallerTests(unittest.TestCase):
    def test_orchestrator_modes_and_read_only_planner_permissions(self) -> None:
        planner = (install.SCRIPT_DIR / "agents/planner.md").read_text(encoding="utf-8")
        builder = (install.SCRIPT_DIR / "agents/builder.md").read_text(encoding="utf-8")
        self.assertIn("mode: subagent", planner)
        self.assertIn("bash: false", planner)
        self.assertIn("bash: deny", planner)
        self.assertIn("researcher: allow", planner)
        self.assertIn("observer: allow", planner)
        self.assertIn("mode: subagent", builder)
        self.assertIn("coder: allow", builder)
        self.assertIn("code-reviewer: allow", builder)
        self.assertIn("observer: allow", builder)

    def test_agent_model_aliases_and_variants_write_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "model-aliases": {
                            "opus": {"model": "anthropic/claude-opus-5-5", "variant": "high"},
                            "fast": "google/gemini-3.8-flash",
                        },
                        "agent-models": {
                            "profile1": {
                                "planner": "opus",
                                "observer": {"alias": "fast", "variant": "low"},
                            },
                            "profile2": {
                                "planner": "fast"
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                install.run(["--target", str(project), "--config", str(config), "--profile", "profile1"]),
                0,
            )
            runtime = json.loads((project / ".opencode/opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(
                runtime["agent"]["planner"],
                {"mode": "subagent", "model": "anthropic/claude-opus-5-5", "variant": "high"},
            )
            self.assertEqual(
                runtime["agent"]["observer"],
                {"mode": "subagent", "model": "google/gemini-3.8-flash", "variant": "low"},
            )
            self.assertEqual(
                install.run(["--target", str(project), "--config", str(config), "--profile", "profile2"]),
                0,
            )
            switched = json.loads((project / ".opencode/opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(
                switched["agent"]["planner"],
                {"mode": "subagent", "model": "google/gemini-3.8-flash"},
            )

    def test_unknown_agent_model_alias_fails_before_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            config = root / "config.json"
            config.write_text(
                json.dumps({"agent-models": {"profile1": {"planner": "missing-alias"}}}),
                encoding="utf-8",
            )
            self.assertEqual(
                install.run(["--target", str(project), "--config", str(config), "--profile", "profile1"]),
                1,
            )
            self.assertFalse(project.exists())

    def test_flat_agent_models_shape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            config = root / "config.json"
            config.write_text(
                json.dumps({"agent-models": {"planner": {"alias": "opus", "variant": "high"}}}),
                encoding="utf-8",
            )
            self.assertEqual(install.run(["--target", str(project), "--config", str(config)]), 1)
            self.assertFalse(project.exists())

    def test_profile_requires_explicit_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            self.assertEqual(install.run(["--target", str(project), "--profile", "profile1"]), 1)
            self.assertFalse(project.exists())

    def test_no_profile_leaves_existing_agent_routing_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            runtime_config = project / ".opencode/opencode.json"
            runtime_config.parent.mkdir(parents=True)
            original = {"agent": {"planner": {"model": "existing/provider-model"}}}
            expected = {**original, "subagent_depth": 2}
            runtime_config.write_text(json.dumps(original), encoding="utf-8")
            config = root / "config.json"
            config.write_text(
                json.dumps({"agent-models": {"profile1": {"planner": "opus"}}}),
                encoding="utf-8",
            )
            self.assertEqual(install.run(["--target", str(project), "--config", str(config)]), 0)
            self.assertEqual(json.loads(runtime_config.read_text(encoding="utf-8")), expected)

    def test_provider_overlay_merges_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            runtime_config = project / ".opencode/opencode.json"
            runtime_config.parent.mkdir(parents=True)
            runtime_config.write_text(
                json.dumps(
                    {
                        "$schema": "https://opencode.ai/config.json",
                        "provider": {
                            "existing": {
                                "options": {"baseURL": "https://old.example", "timeout": 1000},
                                "models": {
                                    "shared": {"name": "Old", "variants": {"low": {}}}
                                },
                            }
                        },
                        "enabled_providers": ["existing"],
                        "permission": {"edit": "deny"},
                    }
                ),
                encoding="utf-8",
            )
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "opencode_json": {
                            "providers": {
                                "existing": {
                                    "options": {"baseURL": "https://new.example"},
                                    "models": {
                                        "shared": {"name": "New", "reasoning": True},
                                        "added": {"name": "Added", "tool_call": True},
                                    },
                                },
                                "custom": {
                                    "npm": "@ai-sdk/openai",
                                    "options": {"baseURL": "https://custom.example"},
                                    "models": {"custom-model": {"name": "Custom"}},
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(install.run(["--target", str(project), "--config", str(config)]), 0)
            merged = json.loads(runtime_config.read_text(encoding="utf-8"))
            self.assertEqual(merged["provider"]["existing"]["options"]["baseURL"], "https://new.example")
            self.assertEqual(merged["provider"]["existing"]["options"]["timeout"], 1000)
            self.assertEqual(merged["provider"]["existing"]["models"]["shared"]["name"], "New")
            self.assertIn("low", merged["provider"]["existing"]["models"]["shared"]["variants"])
            self.assertIn("added", merged["provider"]["existing"]["models"])
            self.assertIn("custom", merged["provider"])
            self.assertEqual(merged["enabled_providers"], ["existing", "custom"])
            self.assertEqual(merged["permission"], {"edit": "deny"})
            self.assertTrue(any((project / ".opencode/.install-backups").glob("*/opencode.json")))

    def test_jsonc_runtime_config_and_mode_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            runtime_config = project / ".opencode/opencode.json"
            runtime_config.parent.mkdir(parents=True)
            runtime_config.write_text(
                '{\n  // Existing provider configuration\n  "provider": {},\n}\n',
                encoding="utf-8",
            )
            runtime_config.chmod(0o640)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "opencode_json": {
                            "providers": {
                                "custom": {"models": {"model": {"name": "Model"}}}
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(install.run(["--target", str(project), "--config", str(config)]), 0)
            merged = json.loads(runtime_config.read_text(encoding="utf-8"))
            self.assertEqual(merged["provider"]["custom"]["models"]["model"]["name"], "Model")
            self.assertEqual(stat.S_IMODE(runtime_config.stat().st_mode), 0o640)

    def test_disabled_provider_fails_before_links_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            runtime_config = project / ".opencode/opencode.json"
            runtime_config.parent.mkdir(parents=True)
            original = {"disabled_providers": ["custom"]}
            runtime_config.write_text(json.dumps(original), encoding="utf-8")
            config = root / "config.json"
            config.write_text(
                json.dumps({"opencode_json": {"providers": {"custom": {"models": {}}}}}),
                encoding="utf-8",
            )
            self.assertEqual(install.run(["--target", str(project), "--config", str(config)]), 1)
            self.assertEqual(json.loads(runtime_config.read_text(encoding="utf-8")), original)
            self.assertFalse((project / ".opencode/agents/builder.md").exists())

    def test_default_install_creates_relative_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            self.assertEqual(install.run(["--target", str(project)]), 0)
            target = project / ".opencode"
            planner = target / "agents" / "planner.md"
            skill = target / "skills" / "code-philosophy"
            self.assertTrue(planner.is_symlink())
            self.assertTrue(skill.is_symlink())
            self.assertEqual(planner.resolve(), (install.SCRIPT_DIR / "agents/planner.md").resolve())
            self.assertEqual(skill.resolve(), (install.SCRIPT_DIR / "skills/code-philosophy").resolve())
            self.assertTrue((target / ".install-state.json").is_file())

    def test_local_plugins_link_by_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            plugin = root / "auth.js"
            plugin.write_text("export default async () => ({})\n", encoding="utf-8")
            plugins = root / "plugins"
            plugins.mkdir()
            (plugins / "metrics.js").write_text("export default async () => ({})\n", encoding="utf-8")
            self.assertEqual(
                install.run(["--target", str(project), "--plugin", str(plugin), "--plugins", str(plugins)]),
                0,
            )
            target = project / ".opencode/plugins"
            self.assertEqual((target / "auth.js").resolve(), plugin.resolve())
            self.assertEqual((target / "metrics.js").resolve(), (plugins / "metrics.js").resolve())

    def test_remote_plugin_collection_is_cached_and_linked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_dir = root / "plugins"
            plugin_dir.mkdir()
            (plugin_dir / "example.js").write_text("export default async () => ({})\n", encoding="utf-8")
            with directory_server(root) as base_url:
                with tempfile.TemporaryDirectory() as project_directory:
                    project = Path(project_directory) / "project"
                    source = urljoin(base_url, "plugins/")
                    self.assertEqual(install.run(["--target", str(project), "--plugins", source]), 0)
                    installed = project / ".opencode/plugins/example.js"
                    self.assertTrue(installed.is_symlink())
                    self.assertEqual(installed.read_text(encoding="utf-8"), "export default async () => ({})\n")
                    self.assertTrue((project / ".opencode/.install-cache").is_dir())

    def test_plugin_config_source_is_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            plugin = root / "configured.js"
            plugin.write_text("export default async () => ({})\n", encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({"plugins": ["configured.js"]}), encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--config", str(config)]), 0)
            self.assertEqual((project / ".opencode/plugins/configured.js").resolve(), plugin.resolve())

    def test_remote_single_plugin_records_cached_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "remote.js").write_text("export default async () => ({})\n", encoding="utf-8")
            with directory_server(root) as base_url:
                with tempfile.TemporaryDirectory() as project_directory:
                    project = Path(project_directory) / "project"
                    self.assertEqual(install.run(["--target", str(project), "--plugin", urljoin(base_url, "remote.js")]), 0)
                    link = project / ".opencode/plugins/remote.js"
                    self.assertTrue(link.is_symlink())
                    self.assertIn(".install-cache", str(link.resolve()))
                    state = json.loads((project / ".opencode/.install-state.json").read_text(encoding="utf-8"))
                    self.assertTrue(any(item["kind"] == "plugin" for item in state["installed"]))

    def test_plugin_real_file_is_backed_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            destination = project / ".opencode/plugins/configured.js"
            destination.parent.mkdir(parents=True)
            destination.write_text("old\n", encoding="utf-8")
            source = root / "configured.js"
            source.write_text("export default async () => ({})\n", encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--plugin", str(source)]), 0)
            backups = list((project / ".opencode/.install-backups").rglob("configured.js"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "old\n")

    def test_unicode_equivalent_plugin_collision_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            first = root / "café.js"
            second = root / "cafe\u0301.js"
            first.write_text("export default async () => ({})\n", encoding="utf-8")
            second.write_text("export default async () => ({ different: true })\n", encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--plugin", str(first), "--plugin", str(second)]), 1)
            self.assertFalse(project.exists())

    def test_blank_plugin_source_fails_before_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            config = root / "config.json"
            config.write_text(json.dumps({"plugins": [""]}), encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--config", str(config)]), 1)
            self.assertFalse(project.exists())

    def test_uppercase_plugin_extension_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            plugin = root / "plugin.JS"
            plugin.write_text("export default async () => ({})\n", encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--plugin", str(plugin)]), 1)
            self.assertFalse(project.exists())

    def test_case_insensitive_plugin_collision_fails_before_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            first = root / "first" / "Auth.js"
            second = root / "second" / "auth.js"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("export default async () => ({})\n", encoding="utf-8")
            second.write_text("export default async () => ({ different: true })\n", encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--plugin", str(first), "--plugin", str(second)]), 1)
            self.assertFalse(project.exists())

    def test_dry_run_does_not_create_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            self.assertEqual(install.run(["--target", str(project), "--dry-run"]), 0)
            self.assertFalse(project.exists())

    def test_symlinked_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            outside = root / "outside"
            outside.mkdir()
            project.mkdir()
            (project / ".opencode").symlink_to(outside, target_is_directory=True)
            self.assertEqual(install.run(["--target", str(project)]), 1)
            self.assertFalse((outside / "agents").exists())

    def test_real_file_is_backed_up_but_symlink_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            agents = project / ".opencode" / "agents"
            agents.mkdir(parents=True)
            real_file = agents / "planner.md"
            real_file.write_text("user-owned file\n", encoding="utf-8")
            wrong_link = agents / "builder.md"
            wrong_link.symlink_to(real_file)

            self.assertEqual(install.run(["--target", str(project)]), 0)
            backup_files = [path for path in (project / ".opencode/.install-backups").rglob("*") if path.is_file()]
            self.assertEqual(len(backup_files), 1)
            self.assertEqual(backup_files[0].read_text(encoding="utf-8"), "user-owned file\n")
            self.assertTrue((agents / "planner.md").is_symlink())
            self.assertTrue((agents / "builder.md").is_symlink())
            self.assertEqual((agents / "builder.md").resolve(), (install.SCRIPT_DIR / "agents/builder.md").resolve())

    def test_config_and_cli_sources_merge_and_bundled_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            external = root / "external"
            skills = external / "skills" / "sample-skill"
            agents = external / "agents"
            skills.mkdir(parents=True)
            agents.mkdir(parents=True)
            (skills / "SKILL.md").write_text(
                "---\nname: sample-skill\ndescription: A sample skill\n---\nUse it.\n",
                encoding="utf-8",
            )
            (agents / "sample-agent.md").write_text(
                "---\ndescription: A sample agent\nmode: subagent\n---\nDo it.\n",
                encoding="utf-8",
            )
            config = root / "config.json"
            config.write_text(json.dumps({"skills": ["external/skills"], "agents": ["external/agents"]}), encoding="utf-8")

            self.assertEqual(
                install.run(
                    [
                        "--target",
                        str(project),
                        "--config",
                        str(config),
                        "--skill",
                        str(skills),
                        "--agent",
                        str(agents / "sample-agent.md"),
                    ]
                ),
                0,
            )
            target = project / ".opencode"
            self.assertEqual((target / "skills/sample-skill").resolve(), skills.resolve())
            self.assertEqual((target / "agents/sample-agent.md").resolve(), (agents / "sample-agent.md").resolve())

    def test_external_conflict_fails_before_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            first = root / "first" / "same.md"
            second = root / "second" / "same.md"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("---\ndescription: first\n---\nfirst\n", encoding="utf-8")
            second.write_text("---\ndescription: second\n---\nsecond\n", encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--agent", str(first), "--agent", str(second)]), 1)
            self.assertFalse(project.exists())

    def test_bundled_name_wins_and_unrelated_entries_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            external = root / "planner.md"
            external.write_text("---\ndescription: external planner\n---\nExternal.\n", encoding="utf-8")
            custom = project / ".opencode/agents/custom.md"
            custom.parent.mkdir(parents=True)
            custom.write_text("keep me\n", encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--agent", str(external)]), 0)
            planner = project / ".opencode/agents/planner.md"
            self.assertEqual(planner.resolve(), (install.SCRIPT_DIR / "agents/planner.md").resolve())
            self.assertEqual(custom.read_text(encoding="utf-8"), "keep me\n")

    def test_crlf_agent_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "crlf-agent.md"
            agent.write_bytes(b"---\r\ndescription: CRLF agent\r\nmode: subagent\r\n---\r\nBody.\r\n")
            project = root / "project"
            self.assertEqual(install.run(["--target", str(project), "--agent", str(agent)]), 0)
            self.assertTrue((project / ".opencode/agents/crlf-agent.md").is_symlink())

    def test_rollback_restores_replaced_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            agents = project / ".opencode/agents"
            agents.mkdir(parents=True)
            old_target = root / "old.md"
            old_target.write_text("old\n", encoding="utf-8")
            original = agents / "planner.md"
            original.symlink_to(old_target)
            real_create = install.create_relative_link

            def fail_planner(destination: Path, expected: Path) -> None:
                if destination.name == "planner.md":
                    raise OSError("injected link failure")
                real_create(destination, expected)

            with mock.patch.object(install, "create_relative_link", side_effect=fail_planner):
                self.assertEqual(install.run(["--target", str(project)]), 1)
            self.assertTrue(original.is_symlink())
            self.assertEqual(original.readlink(), old_target)
            self.assertFalse((agents / "builder.md").exists())

    def test_html_skill_collection_downloads_support_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "banner-design"
            references = skill / "references"
            references.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: banner-design\ndescription: Design banners\n---\nUse references.\n",
                encoding="utf-8",
            )
            (references / "colors.md").write_text("# Colors\n", encoding="utf-8")
            (references / "SKILL.md").write_text(
                "---\nname: nested-reference\ndescription: Reference content\n---\nReference only.\n",
                encoding="utf-8",
            )
            with directory_server(root) as base_url:
                with tempfile.TemporaryDirectory() as project_directory:
                    project = Path(project_directory) / "project"
                    source = urljoin(base_url, "skills/")
                    self.assertEqual(install.run(["--target", str(project), "--skills", source]), 0)
                    installed = project / ".opencode/skills/banner-design"
                    self.assertTrue(installed.is_symlink())
                    self.assertEqual(
                        (installed / "SKILL.md").read_text(encoding="utf-8"),
                        (skill / "SKILL.md").read_text(encoding="utf-8"),
                    )
                    self.assertEqual((installed / "references/colors.md").read_text(encoding="utf-8"), "# Colors\n")
                    self.assertTrue((installed / "references/SKILL.md").is_file())
                    self.assertFalse((project / ".opencode/skills/references").exists())

    def test_encoded_html_collection_path_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skill sets" / "banner-design"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: banner-design\ndescription: Encoded path\n---\nUse it.\n",
                encoding="utf-8",
            )
            with directory_server(root) as base_url:
                with tempfile.TemporaryDirectory() as project_directory:
                    project = Path(project_directory) / "project"
                    source = urljoin(base_url, "skill%20sets/")
                    self.assertEqual(install.run(["--target", str(project), "--skills", source]), 0)
                    self.assertTrue((project / ".opencode/skills/banner-design").is_symlink())

    def test_json_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.json").write_text(
                json.dumps({"skills": [{"name": "bad-skill", "files": ["SKILL.md", "..\\\\outside"]}]}),
                encoding="utf-8",
            )
            with directory_server(root) as base_url:
                with tempfile.TemporaryDirectory() as project_directory:
                    project = Path(project_directory) / "project"
                    self.assertEqual(install.run(["--target", str(project), "--skills", urljoin(base_url, "index.json")]), 1)
                    self.assertFalse(project.exists())

    def test_remote_cache_corruption_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "sample-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: sample-skill\ndescription: Sample\n---\nUse it.\n",
                encoding="utf-8",
            )
            with directory_server(root) as base_url:
                with tempfile.TemporaryDirectory() as project_directory:
                    project = Path(project_directory) / "project"
                    source = urljoin(base_url, "skills/")
                    self.assertEqual(install.run(["--target", str(project), "--skills", source]), 0)
                    cached_skill = next((project / ".opencode/.install-cache").glob("*/SKILL.md"))
                    cached_skill.write_text("tampered\n", encoding="utf-8")
                    self.assertEqual(install.run(["--target", str(project), "--skills", source]), 1)

    def test_github_url_parser_handles_tree_and_blob(self) -> None:
        tree = install.parse_github_url(
            "https://github.com/example/repository/tree/main/.claude/skills"
        )
        blob = install.parse_github_url(
            "https://github.com/example/repository/blob/main/agents/planner.md"
        )
        self.assertEqual(tree, ("example", "repository", "tree", "main", ".claude/skills"))
        self.assertEqual(blob, ("example", "repository", "blob", "main", "agents/planner.md"))


if __name__ == "__main__":
    unittest.main()
