"""Independent preparation checks; no inherited cases or application imports.

These tests execute selected source definitions and literal base-schema DDL only.
They do not authenticate mail, exercise the full evaluator, or prove runtime readiness.
"""
from __future__ import annotations

import ast
import json
import re
import socket
import sqlite3
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
PROVISIONING = "Demo Provisioning Support"
ACCEPTANCE = "Demo Connectivity Acceptance Support"


def source(path):
    return (ROOT / path).read_text(encoding="utf-8-sig")


def literal_assignment(path, name):
    for node in ast.parse(source(path)).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing literal assignment: {path} {name}")


def enum_values(name):
    node = next(node for node in ast.parse(source("src/models/enums.py")).body
                if isinstance(node, ast.ClassDef) and node.name == name)
    return [ast.literal_eval(item.value) for item in node.body
            if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant)]


def extracted_authority():
    module = types.ModuleType("public_demo_extracted_authority")
    sys.modules[module.__name__] = module
    module.__dict__["Enum"] = Enum
    level = next(node for node in ast.parse(source("src/models/progress_assessment.py")).body
                 if isinstance(node, ast.ClassDef) and node.name == "EvidenceLevel")
    nodes = [level]
    for node in ast.parse(source("src/email/source_authority.py")).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src."):
                continue
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "SourceAuthority":
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in {
            "normalized_sender", "resolve_source_authority"
        }:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id.startswith("CONNECTIVITY_ACCEPTANCE_SUPPORT_")
            for target in node.targets
        ):
            nodes.append(node)
    tree = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(tree, "<extracted-public-demo-authority>", "exec"), module.__dict__)
    return module


class PublicDemoChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network_guard = patch.object(socket, "socket", side_effect=AssertionError("Network forbidden"))
        cls.network_guard.start()
        cls.authority = extracted_authority()
        cls.contract = json.loads(source("wiki/playbook/fix-connectivity/milestone-contracts.json"))

    @classmethod
    def tearDownClass(cls):
        cls.network_guard.stop()
        sys.modules.pop("public_demo_extracted_authority", None)

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE organisations(org_id TEXT, org_type TEXT);
            CREATE TABLE contacts(contact_id TEXT, organisation_id TEXT,
                primary_email TEXT, email_addresses TEXT, role TEXT,
                department TEXT, contact_type TEXT, created_by TEXT);
            CREATE TABLE contact_project_links(project_id TEXT, contact_id TEXT);
            CREATE TABLE project_parties(project_id TEXT, org_id TEXT, party_role TEXT);
            CREATE TABLE projects(project_id TEXT, client_email TEXT);
            INSERT INTO projects VALUES('DEMO-PROJECT', 'client@client.demo.invalid');
        """)

    def tearDown(self):
        self.conn.close()

    def resolve(self, sender):
        return self.authority.resolve_source_authority(
            self.conn, project_id="DEMO-PROJECT", sender_email=sender)

    def add_internal(self, email, role, *, department="", created_by="operator_confirmed", aliases="[]"):
        self.conn.execute("INSERT INTO contacts VALUES(?,NULL,?,?,?,?,?,?)", (
            f"contact-{self.conn.execute('SELECT COUNT(*) FROM contacts').fetchone()[0]}",
            email, aliases, role, department, "internal", created_by))

    def assert_unknown(self, sender):
        result = self.resolve(sender)
        self.assertNotIn(result.actor, {PROVISIONING, ACCEPTANCE})
        self.assertEqual(result.evidence_level.value, "L5_unverified_or_indirect")

    def test_exact_acceptance_domain_and_single_sender_normalization(self):
        for sender in ["analyst@acceptance.demo.invalid", "ANALYST@ACCEPTANCE.DEMO.INVALID",
                       "Demo User <analyst@acceptance.demo.invalid>"]:
            with self.subTest(sender=sender):
                result = self.resolve(sender)
                self.assertEqual(result.actor, ACCEPTANCE)
                self.assertEqual(result.evidence_level.value, "L3_named_authority_written")
                self.assertEqual(result.basis, "operator_confirmed_support_domain")

    def test_exact_exception_mailbox_is_not_a_domain_grant(self):
        self.assertEqual(self.resolve("acceptance-special@exception.demo.invalid").actor, ACCEPTANCE)
        for sender in ["other@exception.demo.invalid", "acceptance-special@sub.exception.demo.invalid",
                       "acceptance-special@exception.demo.invalid.attacker.invalid"]:
            with self.subTest(sender=sender):
                self.assert_unknown(sender)

    def test_rejects_subdomains_lookalikes_and_display_name_claims(self):
        for sender in ["user@sub.acceptance.demo.invalid", "user@acceptance-demo.invalid",
                       "user@acceptance.demo.invalid.attacker.invalid",
                       "Demo Provisioning Support <outsider@outside.demo.invalid>",
                       "Demo Connectivity Acceptance Support <outsider@outside.demo.invalid>",
                       "Demo Provisioning Support", "PCT completed by Demo Provisioning Support"]:
            with self.subTest(sender=sender):
                self.assert_unknown(sender)

    def test_rejects_multiple_senders_and_header_injection(self):
        for sender in ["one@acceptance.demo.invalid, two@acceptance.demo.invalid",
                       "one@acceptance.demo.invalid\r\nBcc: outsider@outside.demo.invalid", None, ""]:
            with self.subTest(sender=sender):
                self.assert_unknown(sender)

    def test_provisioning_requires_confirmed_directory_binding(self):
        sender = "provisioning@provisioning.demo.invalid"
        self.assert_unknown(sender)
        self.add_internal(sender, PROVISIONING, created_by="imported")
        self.assert_unknown(sender)
        self.add_internal(sender, PROVISIONING)
        self.assertEqual(self.resolve(sender).actor, PROVISIONING)
        self.assertEqual(self.resolve(sender).basis, "operator_confirmed_contact")

    def test_directory_alias_and_department_binding(self):
        self.add_internal("primary@team.demo.invalid", "", department=PROVISIONING,
                          aliases=json.dumps(["alias@team.demo.invalid"]))
        self.assertEqual(self.resolve("alias@team.demo.invalid").actor, PROVISIONING)

    def test_conflicting_directory_roles_fail_closed(self):
        self.add_internal("conflict@team.demo.invalid", PROVISIONING, department=ACCEPTANCE)
        result = self.resolve("conflict@team.demo.invalid")
        self.assertEqual(result.basis, "conflicting_contact_roles")
        self.assert_unknown("conflict@team.demo.invalid")
        self.add_internal("duplicate@team.demo.invalid", PROVISIONING)
        self.add_internal("duplicate@team.demo.invalid", ACCEPTANCE)
        self.assert_unknown("duplicate@team.demo.invalid")

    def test_contract_stage_and_frontend_union_agree(self):
        stages = enum_values("Milestone")
        self.assertEqual(len(stages), 8)
        self.assertEqual(stages, [stage["id"] for stage in self.contract["milestones"]])
        frontend = re.search(r"export const MILESTONES = \[(.*?)\] as const",
                             source("dashboard/src/lib/enums.ts"), re.S).group(1)
        self.assertEqual(stages, re.findall(r'"(M[1-8]_[^"\n]+)"', frontend))
        union = next(line for line in source("dashboard/src/lib/generated/api-schema.ts").splitlines()
                     if "Milestone:" in line)
        self.assertEqual(stages, re.findall(r'"(M[1-8]_[^"\n]+)"', union))

    def test_base_schema_accepts_only_coordinated_stage_and_owner_values(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(literal_assignment("src/db/schema.py", "SCHEMA_DDL"))
            for index, stage in enumerate(enum_values("Milestone")):
                conn.execute("INSERT INTO projects(project_id,current_milestone) VALUES(?,?)", (f"DEMO-{index}", stage))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO projects(project_id,current_milestone) VALUES('DEMO-INVALID','M9_Invalid')")
            for index, team in enumerate(enum_values("OwnerTeam")):
                conn.execute("INSERT INTO internal_actions(action_id,owner_team,title) VALUES(?,?,?)", (f"DEMO-ACTION-{index}", team, "Independent check"))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO internal_actions(action_id,owner_team,title) VALUES('DEMO-INVALID','Unknown Team','check')")
        finally:
            conn.close()

    def test_contract_role_capabilities_remain_distinct(self):
        milestones = self.contract["milestones"]
        m5 = milestones[4]
        self.assertIn(PROVISIONING, m5["acceptance_authority"]["primary"])
        self.assertNotIn(ACCEPTANCE, m5["acceptance_authority"]["primary"])
        self.assertIn("provisioning_support_approved_exemptions",
                      " ".join(m5["required_exit_criteria"][0]["accepted_evidence_predicates"]))
        self.assertEqual(milestones[5]["required_exit_criteria"][0]["signoff_authorities"], [PROVISIONING])
        self.assertEqual(set(milestones[6]["required_exit_criteria"][0]["signoff_authorities"]), {PROVISIONING, ACCEPTANCE})
        self.assertEqual(set(self.contract["pct_scope_policy"]["not_applicable_policy"]["approval_authorities"]), {PROVISIONING, ACCEPTANCE})
        self.assertTrue(self.contract["pct_scope_policy"]["not_applicable_policy"]["per_step_record_required"])
        self.assertEqual(self.contract["global_transition_policy"]["rollback_transition"]["allowed"], [])
        for stage in milestones:
            self.assertTrue(stage["acceptance_authority"]["operator_review_required_for_inferred_advance"])

    def test_demo_wiki_drafts_have_real_local_source_and_links(self):
        pages = [path for path in (ROOT / "wiki").rglob("*.md")
                 if path.relative_to(ROOT / "wiki").parts[0] in {"concept", "playbook"}]
        self.assertEqual(len(pages), 10)
        for path in pages:
            text = path.read_text(encoding="utf-8-sig")
            self.assertTrue(text.startswith("---\n"))
            frontmatter = text.split("---\n", 2)[1]
            self.assertIn("confidence: low", frontmatter)
            self.assertIn("status: draft", frontmatter)
            self.assertIn('"doc:docs/PUBLIC_DEMO_POLICY.md"', frontmatter)
            self.assertTrue((ROOT / "docs/PUBLIC_DEMO_POLICY.md").is_file())
            for target in re.findall(r"target: ([^\s]+)", frontmatter):
                self.assertTrue((ROOT / "wiki" / f"{target}.md").is_file(), target)
        self.assertFalse((ROOT / "wiki/concept/project_creation_constraints.md").exists())

    def test_demo_configuration_is_blank_and_separated(self):
        for path, api_port, ui_port in [
            ("config/config.example.json", 8317, 3317),
            ("config/config.test.example.json", 8318, 3318),
        ]:
            config = json.loads(source(path))
            self.assertEqual(config["api_port"], api_port)
            self.assertEqual(config["dashboard_base_url"], f"http://127.0.0.1:{ui_port}")
            self.assertEqual(config["telegram"]["bot_token"], "")
            self.assertEqual(config["telegram"]["chat_id"], 0)
            self.assertEqual(config["telegram"]["allowed_chat_ids"], [])
            self.assertFalse(config["email"]["imap"]["enabled"])
            self.assertFalse(config["msgraph"]["enabled"])
            self.assertEqual(config["email"]["smtp_password"], "")
            self.assertEqual(config["msgraph"]["client_secret"], "")
            self.assertFalse(config["dashboard_auth"]["local_registration_enabled"])
        self.assertIn('"ollama": "http://llm.demo.invalid/v1"', source("src/llm/routing.py"))

    def test_preparation_launchers_and_supervisor_are_blocked(self):
        for path in ["services.bat", "_start_bot.cmd", "_start_api.cmd", "_start_ui.cmd"]:
            self.assertRegex(source(path).lower(), r"exit /b [1-9][0-9]*")
        tree = ast.parse(source("src/supervisor.py"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "run_supervisor")
        self.assertIsInstance(function.body[1], ast.Raise)
        tree = ast.parse(source("scripts/run_bot.py"))
        main_guard = next(node for node in tree.body if isinstance(node, ast.If)
                          and isinstance(node.test, ast.Compare)
                          and isinstance(node.test.left, ast.Name)
                          and node.test.left.id == "__name__")
        self.assertIsInstance(main_guard.body[0], ast.Raise)

    def test_preparation_does_not_inherit_knowledge_approval(self):
        tree = ast.parse(source("src/email/draft_grounding.py"))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "APPROVED_DRAFT_WIKI_PATHS" for target in node.targets))
        self.assertEqual(ast.unparse(assignment.value), "frozenset()")


if __name__ == "__main__":
    unittest.main()
