"""读取并验证机器人毕业能力交接资料。

资料以一份 JSON 描述每台机器人的身份、软硬件配置、课程任务、实战记录、
故障处置、能力证据、能力认证、毕业码、变更复验、身份事件链与岗位交接。

本模块只依赖标准库：
- ``load_domain`` 读取并执行全部领域规则校验；
- ``capability_conclusions`` 给出每台机器人各项能力的当前结论；
- ``resolve_code`` 模拟“扫毕业码”，返回接收方看到的当前结论；
- ``handover_view`` 按岗位范围裁剪接收方可见资料；
- ``full_history`` 返回学校保留的完整历史。
"""

import json
from pathlib import Path

REQUIRED_TOP_LEVEL = {"domain", "version", "facts", "entities", "rules", "sample"}

# 各项资料允许出现的状态取值
_PASS = "通过"
_PENDING = "待复验"
_REVOKED = "撤销"
_VALID_CODE = "有效"
_CODE_WITH_PENDING = "有效（含待复验能力）"
_CODE_INVALID = "失效"


def load_domain(path: Path) -> dict:
    """返回结构与规则均完整的领域资料。"""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_domain(value)


def validate_domain(value: dict) -> dict:
    """校验领域资料，违规则抛出 ``ValueError``，校验通过后原样返回。"""
    missing = REQUIRED_TOP_LEVEL.difference(value or {})
    if missing:
        raise ValueError(f"领域资料缺少必要字段: {sorted(missing)}")
    if not value["facts"] or not value["entities"] or not value["rules"]:
        raise ValueError("领域资料清单不能为空")

    sample = value["sample"]
    errors: list[str] = []
    org_ids = {o["org_id"] for o in sample.get("organizations", [])}
    if len(org_ids) != len(sample.get("organizations", [])):
        errors.append("组织编号重复")

    robots = sample.get("robots", [])
    robot_ids = [r.get("robot_id") for r in robots]
    if len(set(robot_ids)) != len(robot_ids):
        errors.append("机器人身份编号重复")

    for robot in robots:
        _validate_robot(robot, org_ids, errors)

    if errors:
        raise ValueError("; ".join(errors))
    return value


# ---------------------------------------------------------------------------
# 单台机器人的规则校验
# ---------------------------------------------------------------------------

def _validate_robot(robot: dict, org_ids: set[str], errors: list[str]) -> None:
    rid = robot.get("robot_id", "未登记机器人")

    def err(msg: str) -> None:
        errors.append(f"{rid}: {msg}")

    caps = _index(robot.get("capabilities", []), "capability_id", err, "能力")
    configs = _index(robot.get("hardware_configs", []), "config_id", err, "硬件配置")
    softwares = _index(robot.get("software_versions", []), "software_id", err, "软件版本")
    records = _index(robot.get("field_records", []), "record_id", err, "实战记录")
    evidences = _index(robot.get("evidence", []), "evidence_id", err, "能力证据")
    certs = _index(robot.get("certifications", []), "cert_id", err, "能力认证")
    changes = _index(robot.get("changes", []), "change_id", err, "变更")
    retests = robot.get("retests", [])
    faults = _index(robot.get("faults", []), "fault_id", err, "故障")
    handovers = _index(robot.get("handovers", []), "handover_id", err, "交接")

    for task in robot.get("course_tasks", []):
        _check_time_order(task, "recorded_at", "uploaded_at", err, f"课程任务 {task.get('task_id')}")

    for config in configs.values():
        if config.get("supersedes") and config["supersedes"] not in configs:
            err(f"硬件配置 {config['config_id']} 替换了不存在的 {config['supersedes']}")

    for software in softwares.values():
        if software.get("supersedes") and software["supersedes"] not in softwares:
            err(f"软件版本 {software['software_id']} 替换了不存在的 {software['supersedes']}")

    for record in records.values():
        _check_time_order(record, "recorded_at", "uploaded_at", err, f"实战记录 {record['record_id']}")
        if record.get("config_id") not in configs:
            err(f"实战记录 {record['record_id']} 引用了未知硬件配置")
        if record.get("software_id") not in softwares:
            err(f"实战记录 {record['record_id']} 引用了未知软件版本")

    for ev in evidences.values():
        label = f"证据 {ev.get('evidence_id')}"
        _check_time_order(ev, "recorded_at", "uploaded_at", err, label)
        if ev.get("capability_id") not in caps:
            err(f"{label} 引用了未知能力")
        if ev.get("record_id") not in records:
            err(f"{label} 引用了未知实战记录")
        if ev.get("config_id") not in configs:
            err(f"{label} 引用了未知硬件配置")
        if ev.get("software_id") not in softwares:
            err(f"{label} 引用了未知软件版本")
        if ev.get("result") != _PASS:
            err(f"{label} 结果不是通过，不能作为能力证据")

    for fault in faults.values():
        if fault.get("change_id") and fault["change_id"] not in changes:
            err(f"故障 {fault['fault_id']} 引用了未知变更")

    _validate_certifications(robot, caps, configs, softwares, evidences, org_ids, err)
    _validate_changes_and_retests(robot, caps, configs, softwares, changes, retests, evidences, err)
    _validate_codes(robot, caps, certs, err)
    _validate_events(robot, caps, certs, changes, retests, faults, handovers, err)
    _validate_handovers(robot, caps, handovers, org_ids, err)


def _validate_certifications(robot, caps, configs, softwares, evidences, org_ids, err) -> None:
    for cert in robot.get("certifications", []):
        cid = cert.get("cert_id")
        if cert.get("capability_id") not in caps:
            err(f"认证 {cid} 引用了未知能力")
        if cert.get("config_id") not in configs:
            err(f"认证 {cid} 引用了未知硬件配置")
        if cert.get("software_id") not in softwares:
            err(f"认证 {cid} 引用了未知软件版本")
        if not cert.get("evidence_ids"):
            err(f"认证 {cid} 没有能力证据")
        for ev_id in cert.get("evidence_ids", []):
            ev = evidences.get(ev_id)
            if ev is None:
                err(f"认证 {cid} 引用了未知证据 {ev_id}")
                continue
            if ev.get("capability_id") != cert.get("capability_id"):
                err(f"认证 {cid} 引用了其他能力的证据 {ev_id}")
            if ev.get("config_id") != cert.get("config_id") or ev.get("software_id") != cert.get("software_id"):
                err(f"认证 {cid} 的证据 {ev_id} 与认证基线不一致")
        if cert.get("status") == _REVOKED and not cert.get("revoked_at"):
            err(f"认证 {cid} 已撤销但缺少撤销时间")

        # 多方签署：通过的认证必须由至少两个不同单位签署
        signatures = cert.get("signatures", [])
        sign_orgs = {s.get("org_id") for s in signatures}
        unknown_orgs = sign_orgs.difference(org_ids)
        if unknown_orgs:
            err(f"认证 {cid} 含未知签署单位 {sorted(unknown_orgs)}")
        if cert.get("status") == _PASS and len(sign_orgs) < 2:
            err(f"认证 {cid} 未经至少两个不同单位签署，不能生效")


def _validate_changes_and_retests(robot, caps, configs, softwares, changes, retests, evidences, err) -> None:
    for change in changes.values():
        for cap_id in change.get("affects", []):
            if cap_id not in caps:
                err(f"变更 {change['change_id']} 影响了未知能力 {cap_id}")
        if change.get("resulting_config_id") and change["resulting_config_id"] not in configs:
            err(f"变更 {change['change_id']} 指向未知硬件配置")
        if change.get("resulting_software_id") and change["resulting_software_id"] not in softwares:
            err(f"变更 {change['change_id']} 指向未知软件版本")

    seen_pairs: set[tuple[str, str]] = set()
    for rt in retests:
        rid = rt.get("retest_id")
        change = changes.get(rt.get("change_id"))
        if change is None:
            err(f"复验 {rid} 引用了未知变更")
            continue
        cap_id = rt.get("capability_id")
        if cap_id not in change.get("affects", []):
            err(f"复验 {rid} 的能力不在变更 {change['change_id']} 影响范围内")
        seen_pairs.add((change["change_id"], cap_id))

        if rt.get("status") == _PASS:
            ev = evidences.get(rt.get("evidence_id"))
            if ev is None:
                err(f"复验 {rid} 通过但缺少变化后能力证据")
                continue
            if ev.get("capability_id") != cap_id:
                err(f"复验 {rid} 的证据属于其他能力")
            if ev.get("recorded_at", "") < change.get("at", ""):
                err(f"复验 {rid} 的证据早于变更时间，不能证明变化后能力")
            if change.get("resulting_config_id") and ev.get("config_id") != change["resulting_config_id"]:
                err(f"复验 {rid} 的证据未基于变化后硬件基线")
            if change.get("resulting_software_id") and ev.get("software_id") != change["resulting_software_id"]:
                err(f"复验 {rid} 的证据未基于变化后软件基线")
        elif rt.get("status") == _PENDING:
            if not rt.get("due_at"):
                err(f"复验 {rid} 待复验但缺少期限")
        else:
            err(f"复验 {rid} 状态非法")

    for change in changes.values():
        for cap_id in change.get("affects", []):
            if (change["change_id"], cap_id) not in seen_pairs:
                err(f"变更 {change['change_id']} 影响的能力 {cap_id} 缺少复验记录")


def _validate_codes(robot, caps, certs, err) -> None:
    seen_codes: set[str] = set()
    for code in robot.get("codes", []):
        value = code.get("code")
        if value in seen_codes:
            err(f"毕业码 {value} 重复")
        seen_codes.add(value)
        if not code.get("cert_ids"):
            err(f"毕业码 {value} 没有引用任何能力认证")
        covered = set()
        for cid in code.get("cert_ids", []):
            cert = certs.get(cid)
            if cert is None:
                err(f"毕业码 {value} 引用了未知认证 {cid}")
                continue
            if cert.get("capability_id") in covered:
                err(f"毕业码 {value} 对同一能力重复引用认证")
            covered.add(cert["capability_id"])
            # 赋码只能引用签发时已经通过的项目
            if cert.get("certified_at", "") > code.get("issued_at", ""):
                err(f"毕业码 {value} 引用了签发时尚未通过的认证 {cid}")
            if cert.get("status") == _REVOKED and cert.get("revoked_at", "") <= code.get("issued_at", ""):
                err(f"毕业码 {value} 引用了签发时已撤销的认证 {cid}")


def _validate_events(robot, caps, certs, changes, retests, faults, handovers, err) -> None:
    events = robot.get("events", [])
    previous_at = ""
    for index, event in enumerate(events, start=1):
        if event.get("seq") != index:
            err(f"事件链第 {index} 条序号断裂（实际为 {event.get('seq')}），身份事件链只允许追加")
        at = event.get("at", "")
        if at < previous_at:
            err(f"事件链 {event.get('seq')} 时间倒流，历史记录不得改写")
        previous_at = at

        etype = event.get("type")
        if etype == "毕业赋码" and not any(c["code"] == event.get("code") for c in robot.get("codes", [])):
            err(f"事件 {event.get('seq')} 引用了未知毕业码")
        if "fault_id" in event and event["fault_id"] not in faults:
            err(f"事件 {event.get('seq')} 引用了未知故障")
        if "change_id" in event and event["change_id"] not in changes:
            err(f"事件 {event.get('seq')} 引用了未知变更")
        if "retest_id" in event and not any(r["retest_id"] == event["retest_id"] for r in retests):
            err(f"事件 {event.get('seq')} 引用了未知复验")
        if "cert_id" in event:
            cert = certs.get(event["cert_id"])
            if cert is None:
                err(f"事件 {event.get('seq')} 引用了未知认证")
            elif etype == "证书撤销" and cert.get("status") != _REVOKED:
                err(f"事件 {event.get('seq')} 声明撤销但认证状态不是撤销")
        if "handover_id" in event and event["handover_id"] not in handovers:
            err(f"事件 {event.get('seq')} 引用了未知交接")


def _validate_handovers(robot, caps, handovers, org_ids, err) -> None:
    conclusions = capability_conclusions(robot)
    for handover in robot.get("handovers", []):
        hid = handover.get("handover_id")
        if handover.get("receiver_org_id") not in org_ids:
            err(f"交接 {hid} 的接收单位未知")
        for cap_id in handover.get("required_capabilities", []):
            if cap_id not in caps:
                err(f"交接 {hid} 要求了未知能力 {cap_id}")
            elif conclusions.get(cap_id, {}).get("conclusion") != _PASS:
                err(f"交接 {hid} 要求的能力 {cap_id} 当前结论不是通过，不能向接收方给出通过结论")


def _index(items, key, err, label):
    result = {}
    for item in items or []:
        ident = item.get(key)
        if ident in result:
            err(f"{label}编号重复: {ident}")
        result[ident] = item
    return result


def _check_time_order(item, earlier_key, later_key, err, label):
    if item.get(later_key, "") < item.get(earlier_key, ""):
        err(f"{label} 的补传时间早于实际记录时间")


# ---------------------------------------------------------------------------
# 当前结论、扫码与交接视图
# ---------------------------------------------------------------------------

def _latest(items, date_key):
    return max(items, key=lambda x: x.get(date_key, ""), default=None)


def current_baseline(robot: dict) -> dict:
    """返回当前生效的硬件配置与软件版本。"""
    return {
        "config": _latest(robot.get("hardware_configs", []), "effective_from"),
        "software": _latest(robot.get("software_versions", []), "effective_from"),
    }


def capability_conclusions(robot: dict) -> dict[str, dict]:
    """计算每项能力的当前结论：通过、待复验或未认证。

    最近一次影响该能力的变更若尚未复验通过，即使旧认证仍是“通过”，
    当前结论也必须降级为“待复验”。
    """
    changes = robot.get("changes", [])
    retests = robot.get("retests", [])
    certs = robot.get("certifications", [])
    result = {}

    for cap in robot.get("capabilities", []):
        cap_id = cap["capability_id"]
        affecting = [c for c in changes if cap_id in c.get("affects", [])]
        latest_change = _latest(affecting, "at")
        latest_cert = _latest(
            [c for c in certs if c.get("capability_id") == cap_id and c.get("status") == _PASS],
            "certified_at",
        )

        entry = {
            "capability_id": cap_id,
            "name": cap.get("name"),
            "conclusion": _PENDING,
            "cert_id": latest_cert["cert_id"] if latest_cert else None,
            "change_id": latest_change["change_id"] if latest_change else None,
            "retest_id": None,
            "evidence_id": None,
        }

        if latest_change is not None:
            retest = next(
                (r for r in retests
                 if r.get("change_id") == latest_change["change_id"]
                 and r.get("capability_id") == cap_id),
                None,
            )
            if retest and retest.get("status") == _PASS:
                entry["conclusion"] = _PASS
                entry["retest_id"] = retest["retest_id"]
                entry["evidence_id"] = retest.get("evidence_id")
            else:
                entry["conclusion"] = _PENDING
                if retest:
                    entry["retest_id"] = retest["retest_id"]
                    entry["due_at"] = retest.get("due_at")
        elif latest_cert is not None:
            entry["conclusion"] = _PASS
            entry["evidence_id"] = latest_cert.get("evidence_ids", [None])[-1]
        else:
            entry["conclusion"] = "未认证"

        result[cap_id] = entry
    return result


def resolve_code(value: dict, code: str) -> dict:
    """模拟接收方扫描毕业码：确认机器人当前会做什么、在哪些场景测过、
    最近换件后哪些能力尚未复验。证书撤销后旧码失效。"""
    robot = _find_robot_by_code(value, code)
    if robot is None:
        raise ValueError(f"未知毕业码: {code}")

    certs = {c["cert_id"]: c for c in robot.get("certifications", [])}
    conclusions = capability_conclusions(robot)
    baseline = current_baseline(robot)

    code_entry = next(c for c in robot["codes"] if c["code"] == code)
    referenced_certs = [certs[cid] for cid in code_entry["cert_ids"]]
    revoked = [c for c in referenced_certs if c.get("status") == _REVOKED]

    capabilities = []
    for cert in referenced_certs:
        cap_id = cert["capability_id"]
        cap_info = next(c for c in robot["capabilities"] if c["capability_id"] == cap_id)
        conclusion = conclusions[cap_id]
        capabilities.append({
            "capability_id": cap_id,
            "name": cap_info.get("name"),
            "cert_id": cert["cert_id"],
            "cert_status": cert.get("status"),
            "conclusion": conclusion["conclusion"],
            "retest_id": conclusion["retest_id"],
            "evidence_id": conclusion["evidence_id"],
        })

    if revoked:
        scan_status = _CODE_INVALID
    elif any(c["conclusion"] == _PENDING for c in capabilities):
        scan_status = _CODE_WITH_PENDING
    else:
        scan_status = _VALID_CODE

    pending = [
        {
            "capability_id": cap_id,
            "name": next(c.get("name") for c in robot["capabilities"] if c["capability_id"] == cap_id),
            "retest_id": info["retest_id"],
            "due_at": info.get("due_at"),
            "change_id": info["change_id"],
        }
        for cap_id, info in conclusions.items()
        if info["conclusion"] == _PENDING
    ]

    superseded_by = None
    if scan_status == _CODE_INVALID:
        newer = [c for c in robot["codes"]
                 if c.get("issued_at", "") > code_entry.get("issued_at", "")
                 and code_is_currently_valid(robot, c["code"])]
        superseded_by = _latest(newer, "issued_at")

    return {
        "code": code,
        "scan_status": scan_status,
        "robot_id": robot["robot_id"],
        "name": robot.get("name"),
        "robot_type": robot.get("robot_type"),
        "current_hardware_config": baseline["config"]["config_id"] if baseline["config"] else None,
        "current_software": baseline["software"]["software_id"] if baseline["software"] else None,
        "capabilities": capabilities,
        "tested_scenarios": [
            {
                "record_id": r["record_id"],
                "scenario": r["scenario"],
                "recorded_at": r["recorded_at"],
                "config_id": r["config_id"],
                "software_id": r["software_id"],
            }
            for r in robot.get("field_records", [])
        ],
        "pending_retests": pending,
        "superseded_by": superseded_by["code"] if superseded_by else None,
    }


def code_is_currently_valid(robot: dict, code: str) -> bool:
    """当前是否有效：引用的认证全部未被撤销。"""
    entry = next((c for c in robot.get("codes", []) if c["code"] == code), None)
    if entry is None:
        return False
    certs = {c["cert_id"]: c for c in robot.get("certifications", [])}
    return all(certs.get(cid, {}).get("status") == _PASS for cid in entry["cert_ids"])


def handover_view(value: dict, handover_id: str) -> dict:
    """接收方视图：只含岗位所需能力的当前结论，不含学校的完整历史。"""
    robot, handover = _find_handover(value, handover_id)
    org = _find_org(value, handover["receiver_org_id"])
    conclusions = capability_conclusions(robot)

    valid_codes = [c for c in robot.get("codes", []) if code_is_currently_valid(robot, c["code"])]
    current_code = _latest(valid_codes, "issued_at")

    return {
        "handover_id": handover_id,
        "receiver": {"org_id": org["org_id"], "name": org.get("name")},
        "position": handover.get("position"),
        "issued_at": handover.get("issued_at"),
        "robot_id": robot["robot_id"],
        "name": robot.get("name"),
        "robot_type": robot.get("robot_type"),
        "current_code": current_code["code"] if current_code else None,
        "capabilities": [
            {
                "capability_id": cap_id,
                "name": next(c.get("name") for c in robot["capabilities"] if c["capability_id"] == cap_id),
                "conclusion": conclusions[cap_id]["conclusion"],
                "evidence_id": conclusions[cap_id]["evidence_id"],
            }
            for cap_id in handover.get("required_capabilities", [])
        ],
    }


def full_history(value: dict, robot_id: str) -> dict:
    """学校视图：沿原身份保留从课堂到职场的完整历史。"""
    robot = next((r for r in value["sample"]["robots"] if r["robot_id"] == robot_id), None)
    if robot is None:
        raise ValueError(f"未知机器人身份: {robot_id}")
    return {
        "robot_id": robot_id,
        "conclusions": capability_conclusions(robot),
        "hardware_configs": robot.get("hardware_configs", []),
        "software_versions": robot.get("software_versions", []),
        "course_tasks": robot.get("course_tasks", []),
        "field_records": robot.get("field_records", []),
        "faults": robot.get("faults", []),
        "evidence": robot.get("evidence", []),
        "certifications": robot.get("certifications", []),
        "changes": robot.get("changes", []),
        "retests": robot.get("retests", []),
        "codes": robot.get("codes", []),
        "events": robot.get("events", []),
        "handovers": robot.get("handovers", []),
    }


def _find_robot_by_code(value: dict, code: str):
    return next(
        (r for r in value["sample"]["robots"]
         if any(c["code"] == code for c in r.get("codes", []))),
        None,
    )


def _find_handover(value: dict, handover_id: str):
    for robot in value["sample"]["robots"]:
        for handover in robot.get("handovers", []):
            if handover["handover_id"] == handover_id:
                return robot, handover
    raise ValueError(f"未知交接: {handover_id}")


def _find_org(value: dict, org_id: str) -> dict:
    org = next((o for o in value["sample"]["organizations"] if o["org_id"] == org_id), None)
    if org is None:
        raise ValueError(f"未知组织: {org_id}")
    return org
