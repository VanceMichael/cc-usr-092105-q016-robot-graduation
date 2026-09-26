import copy
import unittest
from pathlib import Path

from src.domain import load_domain, validate_dossier

FIXTURE = Path("fixtures/domain.json")


def valid_dossier() -> dict:
    return copy.deepcopy(load_domain(FIXTURE)["sample"]["dossier"])


def assert_has_error(testcase: unittest.TestCase, errors: list[str], needle: str):
    testcase.assertTrue(
        any(needle in msg for msg in errors),
        msg=f"期望校验报错包含 {needle!r}，实际报错：{errors}",
    )


class DossierHappyPathTest(unittest.TestCase):
    def test_fixture_dossier_satisfies_all_invariants(self):
        dossier = valid_dossier()
        self.assertEqual(validate_dossier(dossier), [])

    def test_reverifying_capabilities_follow_sensor_replacement(self):
        """换雷达与导航升级后，相关能力当前结论必须是待复验。"""
        dossier = valid_dossier()
        status = {c["capability_id"]: c["current_status"] for c in dossier["certifications"]}
        self.assertEqual(status["C1"], "reverifying")
        self.assertEqual(status["C3"], "reverifying")
        self.assertEqual(status["C4"], "reverifying")
        self.assertEqual(status["C2"], "passed")
        self.assertEqual(status["C5"], "revoked")


class QrCodeRuleTest(unittest.TestCase):
    def test_code_issued_after_revocation_cannot_reference_revoked(self):
        dossier = valid_dossier()
        # C5 于 08-25 被撤销，09-01 补发的码不得再引用。
        dossier["qr_codes"].append(
            {
                "code_id": "GRAD-0117-R2",
                "issued_at": "2026-09-01",
                "capability_ids": ["C1", "C5"],
            }
        )
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "GRAD-0117-R2")
        assert_has_error(self, errors, "C5")

    def test_code_can_keep_reference_when_capability_revoked_later(self):
        """签发时已通过的能力，其后被撤销不追溯否定签发；扫码解析反映当前结论。"""
        dossier = valid_dossier()
        dossier["qr_codes"][0]["capability_ids"].append("C5")
        errors = validate_dossier(dossier)
        self.assertEqual([e for e in errors if "C5" in e], [])

    def test_code_cannot_reference_capability_passed_after_issuance(self):
        dossier = valid_dossier()
        # 新能力在毕业码签发之后才通过，不得提前赋码。
        dossier["certifications"].append(
            {
                "capability_id": "C9",
                "name": "夜间巡场",
                "scope": "闭馆后自主巡场",
                "current_status": "passed",
                "decisions": [
                    {
                        "type": "pass",
                        "decided_at": "2026-09-10",
                        "evidence_ids": ["E6"],
                        "signatures": [
                            {"signer_id": "SCH-T-07", "organization": "杭州机器人学校", "role": "教师"},
                            {"signer_id": "MUS-C-01", "organization": "城市文化典藏馆", "role": "场地负责人"},
                        ],
                    }
                ],
            }
        )
        dossier["qr_codes"][0]["capability_ids"].append("C9")
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "C9")

    def test_code_cannot_reference_unknown_capability(self):
        dossier = valid_dossier()
        dossier["qr_codes"][0]["capability_ids"] = ["C-NOPE"]
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "不存在的能力 C-NOPE")


class ReverificationRuleTest(unittest.TestCase):
    def test_hardware_change_forces_reverifying_status(self):
        dossier = valid_dossier()
        c3 = next(c for c in dossier["certifications"] if c["capability_id"] == "C3")
        c3["current_status"] = "passed"  # 换雷达后仍标通过，违规
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "C3")
        assert_has_error(self, errors, "reverifying")

    def test_change_must_reference_known_capability(self):
        dossier = valid_dossier()
        dossier["change_events"][0]["affected_capabilities"] = ["C-X"]
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "不存在的能力 C-X")


class CertificationRuleTest(unittest.TestCase):
    def test_pass_requires_evidence(self):
        dossier = valid_dossier()
        c2 = next(c for c in dossier["certifications"] if c["capability_id"] == "C2")
        del c2["decisions"][0]["evidence_ids"]
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "缺少能力证据")

    def test_pass_evidence_must_exist(self):
        dossier = valid_dossier()
        c2 = next(c for c in dossier["certifications"] if c["capability_id"] == "C2")
        c2["decisions"][0]["evidence_ids"] = ["E-MISSING"]
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "不存在的证据 E-MISSING")

    def test_decision_needs_two_signers(self):
        dossier = valid_dossier()
        c2 = next(c for c in dossier["certifications"] if c["capability_id"] == "C2")
        c2["decisions"][0]["signatures"] = c2["decisions"][0]["signatures"][:1]
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "至少需要两人签署")

    def test_decision_needs_two_organizations(self):
        dossier = valid_dossier()
        c2 = next(c for c in dossier["certifications"] if c["capability_id"] == "C2")
        c2["decisions"][0]["signatures"] = [
            {"signer_id": "SCH-T-07", "organization": "杭州机器人学校", "role": "教师甲"},
            {"signer_id": "SCH-T-08", "organization": "杭州机器人学校", "role": "教师乙"},
        ]
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "签署机构不得少于两方")

    def test_revoke_requires_reason_and_prior_pass(self):
        dossier = valid_dossier()
        c2 = next(c for c in dossier["certifications"] if c["capability_id"] == "C2")
        c2["decisions"].append(
            {
                "type": "revoke",
                "decided_at": "2026-09-01",
                "signatures": [
                    {"signer_id": "SCH-T-07", "organization": "杭州机器人学校", "role": "教师"},
                    {"signer_id": "MUS-C-01", "organization": "城市文化典藏馆", "role": "场地负责人"},
                ],
            }
        )
        c2["current_status"] = "revoked"
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "撤销决定必须记录原因")

    def test_config_snapshot_must_match_installed_part_at_decision_time(self):
        dossier = valid_dossier()
        c1 = next(c for c in dossier["certifications"] if c["capability_id"] == "C1")
        # 06-25 通过时新雷达 SN-L-2312 要到 08-20 才安装。
        c1["decisions"][0]["config_snapshot"]["hardware"]["H-LIDAR"] = "SN-L-2312"
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "SN-L-2312 尚未安装")


class EvidenceRuleTest(unittest.TestCase):
    def test_offline_upload_captured_must_be_before_uploaded(self):
        dossier = valid_dossier()
        e4 = next(e for e in dossier["evidence"] if e["evidence_id"] == "E4")
        e4["captured_at"] = "2026-06-21"  # 晚于 06-20 的补传时间
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "E4")
        self.assertTrue(any("采集时间" in m for m in errors))

    def test_duplicate_evidence_id_rejected(self):
        dossier = valid_dossier()
        dossier["evidence"].append(dossier["evidence"][0])
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "编号重复")


class TimelineRuleTest(unittest.TestCase):
    def test_timeline_must_be_chronological(self):
        dossier = valid_dossier()
        dossier["timeline"][-1]["occurred_at"] = "2026-01-01"
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "时间线必须按发生时间追加")

    def test_timeline_event_keeps_same_identity(self):
        dossier = valid_dossier()
        dossier["timeline"][0]["robot_id"] = "RG-HZ-9999"
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "RG-HZ-9999")

    def test_trial_feedback_requires_deployment(self):
        dossier = valid_dossier()
        feedback = next(e for e in dossier["timeline"] if e["type"] == "trial_feedback")
        feedback["refs"]["receivers"] = ["RCV-OVERSEAS"]  # 海外方从未部署过本机
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "尚无在先部署记录")

    def test_return_repair_requires_incident(self):
        dossier = valid_dossier()
        repair = next(e for e in dossier["timeline"] if e["type"] == "return_repair")
        del repair["refs"]["incidents"]
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "退回返修必须关联故障记录")

    def test_cert_event_without_matching_decision_rejected(self):
        dossier = valid_dossier()
        event = next(e for e in dossier["timeline"] if e["type"] == "cert_revoked")
        event["refs"]["capabilities"] = ["C2"]  # C2 没有撤销决定
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "对应认证决定")


class ReceiverViewRuleTest(unittest.TestCase):
    def test_view_must_match_post_requirements(self):
        dossier = valid_dossier()
        museum = next(r for r in dossier["receivers"] if r["receiver_id"] == "RCV-MUSEUM")
        museum["required_capabilities"] = ["C1", "C2"]  # 视图仍含 C3
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "与岗位所需能力完全一致")

    def test_view_cannot_leak_internal_fields(self):
        dossier = valid_dossier()
        museum = next(r for r in dossier["receivers"] if r["receiver_id"] == "RCV-MUSEUM")
        museum["view"][0]["serial"] = "SN-L-2312"
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "最小披露范围外")

    def test_view_status_must_match_current_conclusion(self):
        dossier = valid_dossier()
        museum = next(r for r in dossier["receivers"] if r["receiver_id"] == "RCV-MUSEUM")
        museum["view"][0]["current_status"] = "passed"  # C1 实际待复验
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "视图状态与档案当前结论不一致")

    def test_view_scenario_needs_evidence(self):
        dossier = valid_dossier()
        overseas = next(r for r in dossier["receivers"] if r["receiver_id"] == "RCV-OVERSEAS")
        overseas["view"][0]["tested_scenarios"] = ["工厂参观"]  # C2 无此场景证据
        errors = validate_dossier(dossier)
        assert_has_error(self, errors, "无证据支撑")


if __name__ == "__main__":
    unittest.main()
