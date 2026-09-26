import copy
import json
import unittest
from pathlib import Path

from src.domain import (
    capability_conclusions,
    full_history,
    handover_view,
    load_domain,
    resolve_code,
    validate_domain,
)

FIXTURE = Path("fixtures/domain.json")


def loaded():
    return load_domain(FIXTURE)


def robot(value, robot_id):
    return next(r for r in value["sample"]["robots"] if r["robot_id"] == robot_id)


class FixtureStructureTest(unittest.TestCase):
    def test_fixture_is_complete(self):
        value = loaded()
        self.assertEqual(value["domain"], "robot-graduation")
        self.assertGreater(len(value["entities"]), 8)
        self.assertGreater(len(value["rules"]), 5)

    def test_two_robots_cover_museum_and_overseas(self):
        value = loaded()
        ids = {r["robot_id"] for r in value["sample"]["robots"]}
        self.assertEqual(ids, {"R-HZ-2026-007", "R-HZ-2026-012"})


class CurrentConclusionTest(unittest.TestCase):
    def setUp(self):
        self.value = loaded()
        self.r007 = robot(self.value, "R-HZ-2026-007")
        self.r012 = robot(self.value, "R-HZ-2026-012")

    def test_pending_after_recent_sensor_change(self):
        # 小舟换雷达后：避障导览已复验通过，夜间巡场仍待复验
        conclusions = capability_conclusions(self.r007)
        self.assertEqual(conclusions["C072"]["conclusion"], "通过")
        self.assertEqual(conclusions["C073"]["conclusion"], "待复验")
        self.assertEqual(conclusions["C071"]["conclusion"], "通过")
        self.assertEqual(conclusions["C073"]["change_id"], "CHG-007-1")

    def test_revoked_cert_then_recertified(self):
        # 铁哨：旧高温认证撤销，换件复验重新认证后当前结论为通过
        conclusions = capability_conclusions(self.r012)
        self.assertEqual(conclusions["C122"]["conclusion"], "通过")
        self.assertEqual(conclusions["C122"]["cert_id"], "CERT-012-4")
        self.assertEqual(conclusions["C121"]["conclusion"], "通过")

    def test_pending_retest_blocks_handover(self):
        # 典藏馆岗位只要求 C071/C072，不含待复验的 C073，可以交接
        view = handover_view(self.value, "HD-001")
        self.assertEqual(
            [c["conclusion"] for c in view["capabilities"]], ["通过", "通过"]
        )


class ScanCodeTest(unittest.TestCase):
    def setUp(self):
        self.value = loaded()

    def test_scan_active_code_shows_pending(self):
        scan = resolve_code(self.value, "GRAD-007-A")
        self.assertEqual(scan["scan_status"], "有效（含待复验能力）")
        self.assertEqual(len(scan["capabilities"]), 3)
        pending = {p["capability_id"] for p in scan["pending_retests"]}
        self.assertEqual(pending, {"C073"})
        # 扫码同时能看到在哪些场景测试过
        scenarios = {s["record_id"] for s in scan["tested_scenarios"]}
        self.assertIn("FR-007-4", scenarios)

    def test_scan_revoked_code_is_invalid_and_points_to_new_code(self):
        scan = resolve_code(self.value, "GRAD-012-A")
        self.assertEqual(scan["scan_status"], "失效")
        self.assertEqual(scan["superseded_by"], "GRAD-012-B")

    def test_scan_reissued_code_is_valid(self):
        scan = resolve_code(self.value, "GRAD-012-B")
        self.assertEqual(scan["scan_status"], "有效")
        cert_ids = {c["cert_id"] for c in scan["capabilities"]}
        self.assertNotIn("CERT-012-2", cert_ids)
        self.assertIn("CERT-012-4", cert_ids)

    def test_unknown_code(self):
        with self.assertRaises(ValueError):
            resolve_code(self.value, "NOPE")

    def test_overseas_handover_only_shows_required_capabilities(self):
        view = handover_view(self.value, "HD-002")
        self.assertEqual(view["receiver"]["org_id"], "ORG-OVERSEAS")
        self.assertEqual(view["current_code"], "GRAD-012-B")
        self.assertEqual(
            {c["capability_id"] for c in view["capabilities"]},
            {"C121", "C122", "C123"},
        )
        self.assertTrue(all(c["conclusion"] == "通过" for c in view["capabilities"]))
        # 接收方视图不暴露课堂历史
        self.assertNotIn("course_tasks", view)
        self.assertNotIn("events", view)


class FullHistoryTest(unittest.TestCase):
    def test_school_keeps_full_history_under_one_identity(self):
        value = loaded()
        history = full_history(value, "R-HZ-2026-012")
        types = [e["type"] for e in history["events"]]
        self.assertIn("入训", types)
        self.assertIn("试岗反馈", types)
        self.assertIn("退回返修", types)
        self.assertIn("证书撤销", types)
        self.assertIn("重新认证", types)
        # 撤销与重发都沿同一身份追加
        self.assertEqual(len({c["code"] for c in history["codes"]}), 2)


class RuleViolationTest(unittest.TestCase):
    """对合规样例做单点变异，每条业务规则都必须能被拦下。"""

    def setUp(self):
        self.value = loaded()
        self.r007 = robot(self.value, "R-HZ-2026-007")
        self.r012 = robot(self.value, "R-HZ-2026-012")

    def assertInvalid(self):
        with self.assertRaises(ValueError):
            validate_domain(self.value)

    def test_code_cannot_reference_unpassed_cert(self):
        self.r007["codes"][0]["cert_ids"] = ["CERT-007-1"]
        self.r007["codes"][0]["issued_at"] = "2026-08-01"  # 早于认证日
        self.assertInvalid()

    def test_code_cannot_reference_revoked_cert_at_issue(self):
        self.r012["codes"][0]["issued_at"] = "2026-09-13"  # CERT-012-2 已撤销
        self.assertInvalid()

    def test_cert_needs_multi_org_signatures(self):
        # 删掉博物馆签署，只剩学校一家
        self.r007["certifications"][0]["signatures"] = [
            self.r007["certifications"][0]["signatures"][0]
        ]
        self.assertInvalid()

    def test_offline_upload_cannot_precede_record_time(self):
        self.r007["course_tasks"][1]["uploaded_at"] = "2026-06-30"
        self.assertInvalid()

    def test_change_must_have_retest_record(self):
        # 删除 C073 的待复验记录
        self.r007["retests"] = [
            r for r in self.r007["retests"] if r["capability_id"] != "C073"
        ]
        self.assertInvalid()

    def test_passed_retest_needs_evidence_on_new_baseline(self):
        # C072 复验证据挂回旧硬件基线
        ev = next(e for e in self.r007["evidence"] if e["evidence_id"] == "EV-007-5")
        ev["config_id"] = "HW-007-1"
        self.assertInvalid()

    def test_event_chain_is_append_only(self):
        self.r012["events"][17]["seq"] = 5
        self.assertInvalid()

    def test_revoke_event_must_match_cert_status(self):
        event = next(e for e in self.r012["events"] if e["type"] == "证书撤销")
        event["cert_id"] = "CERT-012-1"  # 该认证并未撤销
        self.assertInvalid()

    def test_handover_requires_current_pass(self):
        # 让典藏馆岗位要求待复验的夜场能力
        handover = self.r007["handovers"][0]
        handover["required_capabilities"].append("C073")
        self.assertInvalid()

    def test_cert_evidence_must_match_baseline(self):
        cert = self.r007["certifications"][0]
        cert["config_id"] = "HW-007-2"  # 证据仍是 HW-007-1
        self.assertInvalid()

    def test_duplicate_identity_rejected(self):
        self.value["sample"]["robots"].append(copy.deepcopy(self.r007))
        self.assertInvalid()

    def test_missing_top_level_field(self):
        del self.value["rules"]
        self.assertInvalid()


if __name__ == "__main__":
    unittest.main()
