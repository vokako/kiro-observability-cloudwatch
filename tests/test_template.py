"""Offline checks. Run: python3 -m unittest discover -s tests"""
import json
import os
import re
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import build_template  # noqa: E402


def load_yaml(text):
    import yaml  # PyYAML

    class Loader(yaml.SafeLoader):
        pass

    def tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {suffix: loader.construct_sequence(node, deep=True)}
        return {suffix: loader.construct_mapping(node, deep=True)}

    Loader.add_multi_constructor("!", tag)
    return yaml.load(text, Loader=Loader)


def kiro_module():
    """Load the Kiro policy Lambda code with botocore stubbed (merge() needs no AWS)."""
    for m in ("botocore", "botocore.session", "botocore.auth", "botocore.awsrequest"):
        mod = types.ModuleType(m)
        mod.SigV4Auth = mod.AWSRequest = object
        sys.modules.setdefault(m, mod)
    ns = {}
    exec(build_template.KIRO_LAMBDA, ns)
    return ns


ON = {
    "user_activity_report.enabled": "true",
    "user_activity_report.otel_export": "true",
    "user_activity_report.otel_secret_arn": '"arn:metrics"',
    "user_activity_report.otel_protocol": "http/protobuf",
    "user_activity_report.otel_temporality": "delta",
    "realtime_event_export.enabled": "true",
    "realtime_event_export.secret_arn": '"arn:events"',
}


class TemplateTest(unittest.TestCase):
    def test_committed_template_is_current(self):
        with open(os.path.join(ROOT, "template", "kiro-observability.yaml"), encoding="utf-8") as f:
            self.assertEqual(f.read(), build_template.template(),
                             "run: python3 src/build_template.py template > template/kiro-observability.yaml")

    def test_size_fits_inline_deploy(self):
        self.assertLess(len(build_template.template().encode()), 51200)

    def test_parts_parse(self):
        t = load_yaml(build_template.template())
        body = t["Resources"]["Dashboard"]["Properties"]["DashboardBody"]["Sub"][0]
        self.assertEqual(set(re.findall(r"\$\{[^}]+\}", body)), {"${AWS::Region}", "${LogGroupResolved}"})
        self.assertGreater(len(json.loads(body)["widgets"]), 20)
        for name in ("KeyFunction", "KiroPolicyFunction"):
            compile(t["Resources"][name]["Properties"]["Code"]["ZipFile"], name, "exec")
        cfg = t["Resources"]["KeyFunction"]["Properties"]["Environment"]["Variables"]["CFG"]["Sub"][0]
        json.loads(re.sub(r"\$\{[^}]+\}", "x", cfg))

    def test_promql_windows_fit_31_days(self):
        # CloudWatch counts the range selector toward the 31-day query limit; the dashboard spans 30 days.
        for w in build_template.dashboard()["widgets"]:
            if w["type"] == "chart":
                q = w["properties"]["data"]["queries"][0]["query"]
                self.assertNotRegex(q, r"\[(?:[2-9]|\d\d+)d\]", q)


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.k = kiro_module()

    def test_keeps_everything_else(self):
        base = ('version: "0.1"\nrules:\n  - id: a\n    capability: model_use\n    effect: allow\n'
                'settings:\n  default_model: !replace "auto"   # keep\n  user_activity_report.otel_export: false\n'
                '  # comment\n\nmetadata:\n  name: x\n')
        out = self.k["merge"](base, ON)
        self.assertIn('default_model: !replace "auto"   # keep', out)
        self.assertIn("  user_activity_report.otel_export: true\n", out)
        self.assertIn("  # comment\n  user_activity_report.enabled: true", out)
        self.assertTrue(out.endswith("\n\nmetadata:\n  name: x\n"))
        self.assertTrue(out.startswith('version: "0.1"\nrules:\n  - id: a\n'))

    def test_idempotent_and_quote_insensitive(self):
        out = self.k["merge"]("settings:\n  a: 1\n", ON)
        self.assertEqual(self.k["merge"](out, ON), out)
        unquoted = out.replace('"arn:metrics"', "arn:metrics")
        self.assertEqual(self.k["merge"](unquoted, ON), unquoted)

    def test_adds_settings_block_and_keeps_indent(self):
        self.assertIn("\nsettings:\n  user_activity_report.enabled: true", self.k["merge"]("rules: []\n", ON))
        self.assertIn("\n    realtime_event_export.enabled: true",
                      self.k["merge"]('settings:\n    default_model: "auto"\n', ON))

    def test_refuses_inline_settings(self):
        with self.assertRaises(Exception):
            self.k["merge"]("settings: {a: 1}\n", ON)


if __name__ == "__main__":
    unittest.main()
