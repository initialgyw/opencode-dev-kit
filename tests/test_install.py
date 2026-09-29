from __future__ import annotations

import contextlib
import http.server
import io
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
    def run_captured(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = install.run(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_bundled_agent_inventory_and_delegation_boundaries(self) -> None:
        agents_dir = install.SCRIPT_DIR / "agents"
        for agent_name in ("plan", "build", "coder", "researcher", "observer", "documenter", "reviewer"):
            self.assertTrue((agents_dir / f"{agent_name}.md").is_file())
        for removed_name in ("planner", "builder", "code-reviewer"):
            self.assertFalse((agents_dir / f"{removed_name}.md").exists())

        plan = (agents_dir / "plan.md").read_text(encoding="utf-8")
        self.assertIn("mode: primary", plan)
        self.assertIn("researcher: allow", plan)
        self.assertIn("observer: allow", plan)
        self.assertIn("Required information-gathering gate", plan)
        self.assertIn("what the subagent should look for", plan)
        self.assertIn("optional flow representation", plan)
        self.assertIn("funcA() -> funcB() -> funcC()", plan)
        self.assertNotIn("Use Mermaid diagrams when", plan)
        self.assertNotIn("magnite", plan.lower())

        build = (agents_dir / "build.md").read_text(encoding="utf-8")
        self.assertIn("mode: primary", build)
        for delegate in ("researcher", "coder", "documenter", "observer", "reviewer"):
            self.assertIn(f"    {delegate}: allow", build)
        self.assertIn("Clarification gate", build)
        self.assertIn("what to look for", build)
        self.assertIn("documentation-only", build)
        self.assertNotIn("magnite", build.lower())

        documenter = (agents_dir / "documenter.md").read_text(encoding="utf-8")
        self.assertIn("mode: subagent", documenter)
        self.assertIn("edit: true", documenter)
        self.assertIn("write: true", documenter)
        self.assertIn("Documentation-only requests belong here", documenter)
        self.assertIn('"**/*.md": allow', documenter)
        self.assertIn('"**/agent/**": deny', documenter)
        self.assertIn('"**/agents/**": deny', documenter)
        self.assertIn('"**/command/**": deny', documenter)
        self.assertIn('"**/commands/**": deny', documenter)
        self.assertIn('"**/skill/**": deny', documenter)
        self.assertIn('"**/skills/**": deny', documenter)
        self.assertIn('"**/AGENTS.md": deny', documenter)
        self.assertIn('"**/.*/**": deny', documenter)
        self.assertIn('"**/SKILL.md": deny', documenter)
        self.assertNotIn("`SKILL.md`", documenter)

        investigation_skill = (install.SCRIPT_DIR / "skills/summarize-investigation/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Mermaid, ASCII, or concise call/data-flow syntax", investigation_skill)
        self.assertIn("funcA() -> funcB() -> funcC()", investigation_skill)
        self.assertIn("Optional verified flow representation", investigation_skill)
        self.assertIn("omit it when prose is clearer", investigation_skill)
        self.assertIn("Any flow representation must reflect verified behavior", investigation_skill)
        self.assertNotIn("Include at least one Mermaid diagram", investigation_skill)
        self.assertNotIn("```mermaid\nflowchart TD", investigation_skill)

        reviewer = (agents_dir / "reviewer.md").read_text(encoding="utf-8")
        self.assertIn("mode: subagent", reviewer)
        self.assertIn("edit: false", reviewer)
        self.assertIn("write: false", reviewer)
        self.assertIn('"*": deny', reviewer)
        self.assertIn('"researcher": allow', reviewer)

    def test_list_profiles_resolves_assignments_in_config_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "model-aliases": {
                            "opus": {"model": "anthropic/claude-opus-5-5", "variant": "high"},
                            "fast": "google/gemini-3.8-flash",
                        },
                        "agent-models": {
                            "thorough": {
                                "coder": "OPUS",
                                "researcher": {"alias": "fast", "variant": "low"},
                                "observer": {"model": "openai/gpt-5", "variant": "minimal"},
                                "reviewer": {"alias": "opus", "variant": "max"},
                            },
                            "quick": {
                                "coder": "vendor/coder-model",
                                "documenter": "fast",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            result, stdout, stderr = self.run_captured(["--list-profiles", "--config", str(config)])

            self.assertEqual(result, 0, stderr)
            self.assertEqual(stderr, "")
            lines = stdout.splitlines()
            thorough_index = lines.index("Profile: thorough")
            quick_index = lines.index("Profile: quick")
            self.assertLess(thorough_index, quick_index)
            self.assertEqual(
                lines[thorough_index + 1 : thorough_index + 6],
                [
                    "  coder: anthropic/claude-opus-5-5 (high)",
                    "  researcher: google/gemini-3.8-flash (low)",
                    "  observer: openai/gpt-5 (minimal)",
                    "  reviewer: anthropic/claude-opus-5-5 (max)",
                    "  documenter: not assigned",
                ],
            )
            self.assertEqual(
                lines[quick_index + 1 : quick_index + 6],
                [
                    "  coder: vendor/coder-model",
                    "  researcher: not assigned",
                    "  observer: not assigned",
                    "  reviewer: not assigned",
                    "  documenter: google/gemini-3.8-flash",
                ],
            )
            self.assertIn("plan and build are not profile-controlled", stdout)
            self.assertIn("target runtime config is not read", stdout)

    def test_list_profiles_escapes_terminal_controls_in_config_values(self) -> None:
        profile_name = "profile-λ\x1b[31m"
        model_id = "vendor/模型\x1b[31m"
        variant = "高\x07"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "agent-models": {
                            profile_name: {"coder": {"model": model_id, "variant": variant}}
                        }
                    }
                ),
                encoding="utf-8",
            )

            result, stdout, stderr = self.run_captured(["--list-profiles", "--config", str(config)])

            self.assertEqual(result, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertIn("Profile: profile-λ\\x1b[31m", stdout)
            self.assertIn("  coder: vendor/模型\\x1b[31m (高\\x07)", stdout)
            self.assertFalse(
                any(not character.isprintable() and character != "\n" for character in stdout),
                "stdout contains a raw terminal control character",
            )

    def test_list_profiles_handles_empty_config_and_optional_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(install.Path, "cwd", return_value=root):
                result, stdout, stderr = self.run_captured(["--list-profiles", "--config"])

            self.assertEqual(result, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertIn("No agent-model profiles found.", stdout)
            self.assertIn("plan and build are not profile-controlled", stdout)
            self.assertFalse((root / ".opencode").exists())

    def test_list_profiles_requires_config(self) -> None:
        result, stdout, stderr = self.run_captured(["--list-profiles"])

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error: --list-profiles requires --config [PATH]", stderr)

    def test_list_profiles_reports_missing_and_malformed_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_config = root / "missing.json"
            malformed_config = root / "malformed.json"
            malformed_config.write_text("{", encoding="utf-8")

            for config, expected_error in (
                (missing_config, "config file does not exist"),
                (malformed_config, "invalid JSON/JSONC"),
            ):
                with self.subTest(config=config.name):
                    result, stdout, stderr = self.run_captured(
                        ["--list-profiles", "--config", str(config)]
                    )
                    self.assertEqual(result, 1)
                    self.assertEqual(stdout, "")
                    self.assertIn(expected_error, stderr)

    def test_list_profiles_fails_clearly_on_unresolved_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "agent-models": {
                            "valid-first": {"coder": "vendor/coder-model"},
                            "broken-profile": {"coder": {"alias": "missing"}},
                        }
                    }
                ),
                encoding="utf-8",
            )

            result, stdout, stderr = self.run_captured(["--list-profiles", "--config", str(config)])

            self.assertEqual(result, 1)
            self.assertEqual(stdout, "")
            self.assertIn("agent-models profile 'broken-profile'", stderr)
            self.assertIn("references unknown model alias: 'missing'", stderr)

    def test_list_profiles_rejects_unsupported_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            for agent_name, expected_error in (
                ("plan", "cannot override manually managed primary agents: plan"),
                ("diagnostician", "supports only bundled delegated agents"),
            ):
                with self.subTest(agent=agent_name):
                    config.write_text(
                        json.dumps(
                            {"agent-models": {"selected": {agent_name: "vendor/model"}}}
                        ),
                        encoding="utf-8",
                    )
                    result, stdout, stderr = self.run_captured(["--list-profiles", "--config", str(config)])
                    self.assertEqual(result, 1)
                    self.assertEqual(stdout, "")
                    self.assertIn("agent-models profile 'selected'", stderr)
                    self.assertIn(expected_error, stderr)

    def test_list_profiles_argparse_errors_for_unknown_and_incomplete_options(self) -> None:
        invalid_arguments = (
            (
                ["--list-profiles", "--config", "config.json", "--unknown"],
                "unrecognized arguments: --unknown",
            ),
            (
                ["--list-profiles", "--config", "config.json", "--plugin"],
                "argument --plugin: expected one argument",
            ),
        )
        for arguments, expected_error in invalid_arguments:
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        install.parse_args(arguments)

                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn(expected_error, stderr.getvalue())

    def test_list_profiles_help_documents_ignored_options(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            install.parse_args(["--list-profiles", "--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("requires --config", help_text)
        self.assertIn("recognized installation options are ignored", help_text)
        for option in (
            "--target",
            "--profile",
            "--skill",
            "--skills",
            "--agent",
            "--agents",
            "--plugin",
            "--plugins",
            "--dry-run",
        ):
            self.assertIn(option, help_text)

    def test_list_profiles_ignores_install_options_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "project"
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "skills": ["https://example.invalid/configured-skills/"],
                        "agents": ["https://example.invalid/configured-agents/"],
                        "plugins": ["https://example.invalid/configured-plugin.js"],
                        "agent-models": {
                            "selected": {"coder": "vendor/coder-model"},
                            "another-profile": {"researcher": "vendor/researcher-model"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            base_result, base_stdout, base_stderr = self.run_captured(
                ["--list-profiles", "--config", str(config)]
            )
            self.assertEqual(base_result, 0, base_stderr)
            self.assertEqual(base_stderr, "")

            ignored_options = [
                "--target",
                str(target),
                "--profile",
                "nonexistent-profile",
                "--skill",
                "https://example.invalid/skill.md",
                "--skills",
                "https://example.invalid/skills/",
                "--agent",
                "https://example.invalid/agent.md",
                "--agents",
                "https://example.invalid/agents/",
                "--plugin",
                "https://example.invalid/plugin.js",
                "--plugins",
                "https://example.invalid/plugins/",
                "--dry-run",
            ]
            with (
                mock.patch.object(
                    install, "resolve_target", side_effect=AssertionError("target resolution")
                ) as resolve_target,
                mock.patch.object(
                    install, "build_sources", side_effect=AssertionError("source resolution")
                ) as build_sources,
                mock.patch.object(
                    install, "resolve_sources", side_effect=AssertionError("source downloads")
                ) as resolve_sources,
                mock.patch.object(
                    install, "prepare_plan", side_effect=AssertionError("install preparation")
                ) as prepare_plan,
                mock.patch.object(
                    install, "apply_plan", side_effect=AssertionError("install writes")
                ) as apply_plan,
            ):
                result, stdout, stderr = self.run_captured(
                    ["--list-profiles", "--config", str(config), *ignored_options]
                )

            self.assertEqual(result, 0, stderr)
            self.assertEqual(stderr, "")
            self.assertEqual(stdout, base_stdout)
            self.assertIn("Profile: selected", stdout)
            self.assertIn("Profile: another-profile", stdout)
            resolve_target.assert_not_called()
            build_sources.assert_not_called()
            resolve_sources.assert_not_called()
            prepare_plan.assert_not_called()
            apply_plan.assert_not_called()
            self.assertFalse(target.exists())

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
                                "coder": "opus",
                                "observer": {"alias": "fast", "variant": "low"},
                                "reviewer": "opus",
                                "documenter": "fast",
                            },
                            "profile2": {
                                "coder": "fast",
                                "documenter": "fast"
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
                runtime["agent"]["coder"],
                {"mode": "subagent", "model": "anthropic/claude-opus-5-5", "variant": "high"},
            )
            self.assertEqual(
                runtime["agent"]["observer"],
                {"mode": "subagent", "model": "google/gemini-3.8-flash", "variant": "low"},
            )
            self.assertEqual(
                runtime["agent"]["reviewer"],
                {"mode": "subagent", "model": "anthropic/claude-opus-5-5", "variant": "high"},
            )
            self.assertEqual(
                runtime["agent"]["documenter"],
                {"mode": "subagent", "model": "google/gemini-3.8-flash"},
            )
            self.assertEqual(
                install.run(["--target", str(project), "--config", str(config), "--profile", "profile2"]),
                0,
            )
            switched = json.loads((project / ".opencode/opencode.json").read_text(encoding="utf-8"))
            self.assertEqual(
                switched["agent"]["coder"],
                {"mode": "subagent", "model": "google/gemini-3.8-flash"},
            )
            self.assertEqual(
                switched["agent"]["documenter"],
                {"mode": "subagent", "model": "google/gemini-3.8-flash"},
            )

    def test_profile_does_not_override_primary_agent_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            runtime_config = project / ".opencode/opencode.json"
            runtime_config.parent.mkdir(parents=True)
            original = {
                "agent": {
                    "plan": {"mode": "primary", "model": "manual/plan-model"},
                    "build": {"mode": "primary", "model": "manual/build-model"},
                }
            }
            runtime_config.write_text(json.dumps(original), encoding="utf-8")
            config = root / "config.json"
            config.write_text(
                json.dumps({"agent-models": {"profile1": {"coder": "provider/coder-model"}}}),
                encoding="utf-8",
            )

            self.assertEqual(
                install.run(["--target", str(project), "--config", str(config), "--profile", "profile1"]),
                0,
            )
            runtime = json.loads(runtime_config.read_text(encoding="utf-8"))
            self.assertEqual(runtime["agent"]["plan"], original["agent"]["plan"])
            self.assertEqual(runtime["agent"]["build"], original["agent"]["build"])
            self.assertNotIn("subagent_depth", runtime)

    def test_profile_rejects_primary_agent_model_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "agent-models": {
                            "profile1": {
                                "plan": "provider/plan-model",
                                "build": "provider/build-model",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                install.run(["--target", str(project), "--config", str(config), "--profile", "profile1"]),
                1,
            )
            self.assertFalse(project.exists())

    def test_profile_rejects_non_specialist_agent_model_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            config = root / "config.json"
            config.write_text(
                json.dumps({"agent-models": {"profile1": {"diagnostician": "provider/model"}}}),
                encoding="utf-8",
            )

            self.assertEqual(
                install.run(["--target", str(project), "--config", str(config), "--profile", "profile1"]),
                1,
            )
            self.assertFalse(project.exists())

    def test_unknown_agent_model_alias_fails_before_target_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            config = root / "config.json"
            config.write_text(
                json.dumps({"agent-models": {"profile1": {"coder": "missing-alias"}}}),
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
                json.dumps({"agent-models": {"coder": {"alias": "opus", "variant": "high"}}}),
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
            original = {
                "agent": {
                    "plan": {"model": "existing/plan-model"},
                    "build": {"model": "existing/build-model"},
                    "coder": {"model": "existing/coder-model"},
                }
            }
            runtime_config.write_text(json.dumps(original), encoding="utf-8")
            config = root / "config.json"
            config.write_text(
                json.dumps({"agent-models": {"profile1": {"coder": "opus"}}}),
                encoding="utf-8",
            )
            self.assertEqual(install.run(["--target", str(project), "--config", str(config)]), 0)
            self.assertEqual(json.loads(runtime_config.read_text(encoding="utf-8")), original)

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
            self.assertFalse((project / ".opencode/agents/reviewer.md").exists())

    def test_default_install_creates_relative_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            self.assertEqual(install.run(["--target", str(project)]), 0)
            target = project / ".opencode"
            skill = target / "skills" / "code-philosophy"
            self.assertTrue(skill.is_symlink())
            self.assertEqual(skill.resolve(), (install.SCRIPT_DIR / "skills/code-philosophy").resolve())
            for agent_name in ("plan", "build", "coder", "researcher", "observer", "documenter", "reviewer"):
                installed = target / "agents" / f"{agent_name}.md"
                self.assertTrue(installed.is_symlink())
                self.assertEqual(installed.resolve(), (install.SCRIPT_DIR / f"agents/{agent_name}.md").resolve())
            self.assertFalse((target / "agents" / "planner.md").exists())
            self.assertFalse((target / "agents" / "builder.md").exists())
            self.assertFalse((target / "agents" / "code-reviewer.md").exists())
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
            real_file = agents / "reviewer.md"
            real_file.write_text("user-owned file\n", encoding="utf-8")
            wrong_link = agents / "researcher.md"
            wrong_link.symlink_to(real_file)

            self.assertEqual(install.run(["--target", str(project)]), 0)
            backup_files = [path for path in (project / ".opencode/.install-backups").rglob("*") if path.is_file()]
            self.assertEqual(len(backup_files), 1)
            self.assertEqual(backup_files[0].read_text(encoding="utf-8"), "user-owned file\n")
            self.assertTrue((agents / "reviewer.md").is_symlink())
            self.assertTrue((agents / "researcher.md").is_symlink())
            self.assertEqual((agents / "reviewer.md").resolve(), (install.SCRIPT_DIR / "agents/reviewer.md").resolve())
            self.assertEqual((agents / "researcher.md").resolve(), (install.SCRIPT_DIR / "agents/researcher.md").resolve())

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
            external = root / "reviewer.md"
            external.write_text("---\ndescription: external reviewer\n---\nExternal.\n", encoding="utf-8")
            custom = project / ".opencode/agents/custom.md"
            custom.parent.mkdir(parents=True)
            custom.write_text("keep me\n", encoding="utf-8")
            self.assertEqual(install.run(["--target", str(project), "--agent", str(external)]), 0)
            reviewer = project / ".opencode/agents/reviewer.md"
            self.assertEqual(reviewer.resolve(), (install.SCRIPT_DIR / "agents/reviewer.md").resolve())
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
            original = agents / "reviewer.md"
            original.symlink_to(old_target)
            real_create = install.create_relative_link

            def fail_reviewer(destination: Path, expected: Path) -> None:
                if destination.name == "reviewer.md":
                    raise OSError("injected link failure")
                real_create(destination, expected)

            with mock.patch.object(install, "create_relative_link", side_effect=fail_reviewer):
                self.assertEqual(install.run(["--target", str(project)]), 1)
            self.assertTrue(original.is_symlink())
            self.assertEqual(original.readlink(), old_target)
            self.assertFalse((agents / "researcher.md").exists())

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
            "https://github.com/example/repository/blob/main/agents/reviewer.md"
        )
        self.assertEqual(tree, ("example", "repository", "tree", "main", ".claude/skills"))
        self.assertEqual(blob, ("example", "repository", "blob", "main", "agents/reviewer.md"))


if __name__ == "__main__":
    unittest.main()
