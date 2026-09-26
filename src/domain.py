"""读取并验证机器人能力交接领域资料。

资料分为两层：`load_domain` 校验顶层清单（事实、对象、规则与样例），
`validate_dossier` 校验一台机器人的完整能力交接档案是否满足领域不变量：

- 毕业赋码只能引用签发时点已通过且未撤销的能力；
- 通过决定必须挂载真实存在的证据，且经多人、跨机构签署；
- 硬件、模型、部署地点变更后，尚未复验通过的能力必须标记为待复验；
- 时间线只能沿同一身份追加，事件编号唯一、按发生时间排序；
- 接收方视图只含岗位所需能力的当前结论，不含内部明细。
"""

import json
from pathlib import Path

TOP_REQUIRED = {"domain", "version", "facts", "entities", "rules", "sample"}

# 接收方视图允许出现的字段（最小披露）。
RECEIVER_VIEW_FIELDS = {
    "capability_id",
    "current_status",
    "scope",
    "latest_evidence_id",
    "decided_at",
    "tested_scenarios",
}

_TIMELINE_TYPE_BY_CHANGE = {
    "hardware": "hardware_change",
    "software": "software_change",
    "deployment": "deployment_change",
}


def load_domain(path: Path) -> dict:
    """返回结构完整且档案不变量全部通过的领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not TOP_REQUIRED.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if not value["facts"] or not value["entities"] or not value["rules"]:
        raise ValueError("领域资料清单不能为空")
    dossier = value["sample"].get("dossier")
    if dossier is not None:
        check_dossier(dossier)
    return value


def check_dossier(dossier: dict) -> None:
    """档案不变量不满足时抛出 ValueError。"""
    errors = validate_dossier(dossier)
    if errors:
        raise ValueError("档案校验失败：\n- " + "\n- ".join(errors))


def validate_dossier(d: dict) -> list[str]:
    """返回档案违反的全部不变量说明，空列表表示通过。"""
    errors: list[str] = []

    identity = d.get("identity", {})
    robot_id = identity.get("robot_id")

    evidence = _index(d.get("evidence", []), "evidence_id", "证据", errors)
    course_tasks = _index(d.get("course_tasks", []), "task_id", "课程任务", errors)
    field_records = _index(d.get("field_records", []), "record_id", "实战记录", errors)
    incidents = _index(d.get("incidents", []), "incident_id", "故障记录", errors)
    changes = _index(d.get("change_events", []), "change_id", "变更事件", errors)
    certs = _index(d.get("certifications", []), "capability_id", "能力", errors)
    receivers = _index(d.get("receivers", []), "receiver_id", "接收方", errors)
    timeline = d.get("timeline", [])

    def err(msg: str) -> None:
        errors.append(msg)

    # ---- 证据引用与离线补传时序 ----
    evidence_scenarios: dict[str, set[str]] = {}
    for e in d.get("evidence", []):
        eid = e.get("evidence_id")
        captured, uploaded = e.get("captured_at"), e.get("uploaded_at")
        if captured and uploaded and captured > uploaded:
            err(f"证据 {eid}：采集时间 {captured} 晚于上传时间 {uploaded}")
        if e.get("upload_mode") == "offline" and captured and uploaded and captured >= uploaded:
            err(f"证据 {eid}：离线补传的采集时间必须严格早于上传时间")
        for tid in e.get("task_ids", []):
            if tid not in course_tasks:
                err(f"证据 {eid}：引用了不存在的课程任务 {tid}")
        for fid in e.get("field_record_ids", []):
            if fid not in field_records:
                err(f"证据 {eid}：引用了不存在的实战记录 {fid}")
        scenarios = {e.get("scenario")}
        for fid in e.get("field_record_ids", []):
            fr = field_records.get(fid)
            if fr:
                scenarios.add(fr.get("scenario"))
        evidence_scenarios[eid] = {s for s in scenarios if s}

    # ---- 实战记录与故障记录引用 ----
    for fr in d.get("field_records", []):
        for eid in fr.get("evidence_ids", []):
            if eid not in evidence:
                err(f"实战记录 {fr.get('record_id')}：引用了不存在的证据 {eid}")
    for inc in d.get("incidents", []):
        iid = inc.get("incident_id")
        for cid in inc.get("related_capabilities", []):
            if cid not in certs:
                err(f"故障记录 {iid}：引用了不存在的能力 {cid}")
        if inc.get("related_change") and inc["related_change"] not in changes:
            err(f"故障记录 {iid}：引用了不存在的变更事件 {inc['related_change']}")

    # ---- 能力认证：证据、签署、配置快照、当前状态 ----
    # 每项能力历次 pass 决定覆盖的证据场景。
    pass_scenarios: dict[str, set[str]] = {cid: set() for cid in certs}
    for cid, cert in certs.items():
        decisions = cert.get("decisions", [])
        if not decisions:
            err(f"能力 {cid}：至少需要一个认证决定")
            continue
        seen_pass = False
        for dec in decisions:
            dtype, at = dec.get("type"), dec.get("decided_at")
            sigs = dec.get("signatures", [])
            if len(sigs) < 2:
                err(f"能力 {cid}：{at} 的{dtype or '认证'}决定至少需要两人签署")
            if len({s.get("organization") for s in sigs}) < 2:
                err(f"能力 {cid}：{at} 的{dtype or '认证'}决定签署机构不得少于两方")
            if dtype == "pass":
                seen_pass = True
                ev_ids = dec.get("evidence_ids", [])
                if not ev_ids:
                    err(f"能力 {cid}：{at} 的通过决定缺少能力证据")
                for eid in ev_ids:
                    if eid not in evidence:
                        err(f"能力 {cid}：{at} 的通过决定引用了不存在的证据 {eid}")
                    else:
                        pass_scenarios[cid].update(evidence_scenarios.get(eid, set()))
                _check_config_snapshot(cid, at, dec.get("config_snapshot"), d, at, err)
            elif dtype == "revoke":
                if not dec.get("reason"):
                    err(f"能力 {cid}：{at} 的撤销决定必须记录原因")
                if not seen_pass:
                    err(f"能力 {cid}：撤销之前必须先有通过决定")

        expected_status = _expected_status(cert, changes)
        if expected_status and cert.get("current_status") != expected_status:
            err(
                f"能力 {cid}：当前状态应为 {expected_status}，"
                f"实际为 {cert.get('current_status')}"
            )

    # ---- 变更事件引用 ----
    for ch in d.get("change_events", []):
        xid = ch.get("change_id")
        for cid in ch.get("affected_capabilities", []):
            if cid not in certs:
                err(f"变更事件 {xid}：引用了不存在的能力 {cid}")

    # ---- 毕业赋码：只能引用签发时点已通过且未撤销的能力 ----
    code_ids = set()
    for code in d.get("qr_codes", []):
        code_id, issued_at = code.get("code_id"), code.get("issued_at")
        if code_id in code_ids:
            err(f"毕业码 {code_id}：编号重复")
        code_ids.add(code_id)
        for cid in code.get("capability_ids", []):
            cert = certs.get(cid)
            if cert is None:
                err(f"毕业码 {code_id}：引用了不存在的能力 {cid}")
                continue
            as_of = [
                dec
                for dec in cert.get("decisions", [])
                if dec.get("decided_at") and issued_at and dec["decided_at"] <= issued_at
            ]
            if not as_of or any(dec["type"] == "revoke" for dec in as_of) or as_of[-1]["type"] != "pass":
                err(f"毕业码 {code_id}：能力 {cid} 在签发时点 {issued_at} 尚未通过或已被撤销，不得赋码")

    # ---- 接收方视图：岗位所需 + 最小披露 + 当前结论 ----
    for rcv in d.get("receivers", []):
        rid = rcv.get("receiver_id")
        required = rcv.get("required_capabilities", [])
        for cid in required:
            if cid not in certs:
                err(f"接收方 {rid}：岗位需求引用了不存在的能力 {cid}")
        view_items = rcv.get("view", [])
        view_ids = [item.get("capability_id") for item in view_items]
        if set(view_ids) != set(required) or len(view_ids) != len(set(view_ids)):
            err(f"接收方 {rid}：视图能力清单必须与岗位所需能力完全一致")
        for item in view_items:
            cid = item.get("capability_id")
            extra = set(item) - RECEIVER_VIEW_FIELDS
            if extra:
                err(f"接收方 {rid}：视图出现最小披露范围外的字段 {sorted(extra)}")
            cert = certs.get(cid)
            if cert is None:
                continue
            if item.get("current_status") != cert.get("current_status"):
                err(f"接收方 {rid}：能力 {cid} 视图状态与档案当前结论不一致")
            latest_pass = _latest_decision(cert, "pass")
            if latest_pass is not None:
                if item.get("decided_at") != latest_pass["decided_at"]:
                    err(f"接收方 {rid}：能力 {cid} 视图决定日期不是最近一次通过日期")
                eid = item.get("latest_evidence_id")
                if eid not in latest_pass.get("evidence_ids", []):
                    err(f"接收方 {rid}：能力 {cid} 视图证据不是最近一次通过决定挂载的证据")
                allowed = pass_scenarios.get(cid, set())
                for scenario in item.get("tested_scenarios", []):
                    if scenario not in allowed:
                        err(f"接收方 {rid}：能力 {cid} 视图测试场景 {scenario} 无证据支撑")

    # ---- 时间线：同一身份、只追加、按发生时间排序 ----
    _validate_timeline(timeline, robot_id, d, err)

    return errors


def _index(items: list, key: str, label: str, errors: list[str]) -> dict:
    index: dict[str, dict] = {}
    for item in items:
        kid = item.get(key)
        if kid in index:
            errors.append(f"{label} {kid}：编号重复")
        index[kid] = item
    return index


def _latest_decision(cert: dict, dtype: str | None = None) -> dict | None:
    decisions = cert.get("decisions", [])
    if dtype is not None:
        decisions = [dec for dec in decisions if dec.get("type") == dtype]
    return decisions[-1] if decisions else None


def _expected_status(cert: dict, changes: dict) -> str | None:
    """按最新决定与之后发生的变更推导能力当前应有的结论。"""
    latest = _latest_decision(cert)
    if latest is None:
        return None
    if latest["type"] == "revoke":
        return "revoked"
    latest_pass_at = latest["decided_at"]
    cid = cert["capability_id"]
    pending = any(
        ch.get("occurred_at", "") > latest_pass_at
        for ch in changes.values()
        if cid in ch.get("affected_capabilities", [])
    )
    return "reverifying" if pending else "passed"


def _check_config_snapshot(cid, at, snapshot, d: dict, when: str, err) -> None:
    """通过决定记录的配置快照必须与决定当时的在装部件/生效版本一致。"""
    if not snapshot:
        return
    for slot, serial in snapshot.get("hardware", {}).items():
        installed = [
            p
            for p in d.get("hardware_config", [])
            if p.get("slot") == slot and p.get("serial") == serial
        ]
        if not installed:
            err(f"能力 {cid}：{at} 配置快照引用了不存在的部件 {slot}/{serial}")
            continue
        p = installed[0]
        if p.get("installed_at", "") > when:
            err(f"能力 {cid}：{at} 决定时部件 {serial} 尚未安装")
        if p.get("removed_at") and p["removed_at"] <= when:
            err(f"能力 {cid}：{at} 决定时部件 {serial} 已被更换")
    for component, version in snapshot.get("software", {}).items():
        matches = [
            v
            for v in d.get("software_versions", [])
            if v.get("component") == component and v.get("version") == version
        ]
        if not matches:
            err(f"能力 {cid}：{at} 配置快照引用了不存在的软件 {component}/{version}")
            continue
        v = matches[0]
        if v.get("effective_at", "") > when:
            err(f"能力 {cid}：{at} 决定时 {component} {version} 尚未生效")
        if v.get("superseded_at") and v["superseded_at"] <= when:
            err(f"能力 {cid}：{at} 决定时 {component} {version} 已被替代")


def _validate_timeline(timeline: list, robot_id, d: dict, err) -> None:
    seen_ids: set[str] = set()
    last_at = ""
    certs = {c["capability_id"]: c for c in d.get("certifications", [])}
    changes = {c["change_id"]: c for c in d.get("change_events", [])}
    incidents = {i["incident_id"]: i for i in d.get("incidents", [])}
    field_records = {f["record_id"]: f for f in d.get("field_records", [])}
    receivers = {r["receiver_id"]: r for r in d.get("receivers", [])}
    evidence = {e["evidence_id"]: e for e in d.get("evidence", [])}
    code_ids = {q["code_id"]: q for q in d.get("qr_codes", [])}

    decisions_on: dict[tuple, str] = {}
    for cid, cert in certs.items():
        for dec in cert.get("decisions", []):
            decisions_on[(cid, dec["type"], dec["decided_at"])] = dec["type"]

    for ev in timeline:
        eid, at, etype = ev.get("event_id"), ev.get("occurred_at"), ev.get("type")
        if eid in seen_ids:
            err(f"时间线事件 {eid}：编号重复")
        seen_ids.add(eid)
        if at and at < last_at:
            err(f"时间线事件 {eid}：发生时间 {at} 早于前一事件 {last_at}，时间线必须按发生时间追加")
        last_at = at or last_at
        if robot_id and ev.get("robot_id") != robot_id:
            err(f"时间线事件 {eid}：机器人身份 {ev.get('robot_id')} 与档案身份 {robot_id} 不一致")

        refs = ev.get("refs", {})

        def check_refs(field: str, index: dict, label: str) -> list[str]:
            ids = refs.get(field, [])
            for rid in ids:
                if rid not in index:
                    err(f"时间线事件 {eid}：引用了不存在的{label} {rid}")
            return ids

        check_refs("capabilities", certs, "能力")
        check_refs("changes", changes, "变更事件")
        check_refs("incidents", incidents, "故障记录")
        check_refs("field_records", field_records, "实战记录")
        check_refs("receivers", receivers, "接收方")
        check_refs("evidence", evidence, "证据")
        check_refs("codes", code_ids, "毕业码")

        if etype in ("cert_decision", "cert_revoked"):
            for cid in refs.get("capabilities", []):
                want = "revoke" if etype == "cert_revoked" else "pass"
                if (cid, want, at) not in decisions_on:
                    err(f"时间线事件 {eid}：找不到能力 {cid} 在 {at} 的对应认证决定")
        if etype in _TIMELINE_TYPE_BY_CHANGE.values():
            for xid in refs.get("changes", []):
                ch = changes.get(xid)
                if ch and ch.get("occurred_at") != at:
                    err(f"时间线事件 {eid}：变更 {xid} 的登记日期与事件日期不一致")
        if etype == "trial_feedback":
            rids = refs.get("receivers", [])
            if not rids:
                err(f"时间线事件 {eid}：试岗反馈必须关联接收方")
            for rid in rids:
                deployed = any(
                    prev.get("type") == "deployment_change"
                    and rid in prev.get("refs", {}).get("receivers", [])
                    and (prev.get("occurred_at") or "") <= (at or "")
                    for prev in timeline
                )
                if not deployed:
                    err(f"时间线事件 {eid}：接收方 {rid} 尚无在先部署记录，不能登记试岗反馈")
        if etype == "return_repair":
            iids = refs.get("incidents", [])
            if not iids:
                err(f"时间线事件 {eid}：退回返修必须关联故障记录")
            for iid in iids:
                inc = incidents.get(iid)
                if inc and inc.get("related_change"):
                    xid = inc["related_change"]
                    has_part_change = any(
                        prev.get("type") == "hardware_change"
                        and xid in prev.get("refs", {}).get("changes", [])
                        for prev in timeline
                    )
                    if not has_part_change:
                        err(f"时间线事件 {eid}：故障 {iid} 的返修部件更换 {xid} 未登记硬件变更事件")
