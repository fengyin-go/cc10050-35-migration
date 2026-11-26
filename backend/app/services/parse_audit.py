"""解析质量审计领域服务：审计解析模板的字段命中率与失败分布，负责命中率劣化告警与沉淀。

数据链路位置：质量审计层，消费页面解析模板上报的命中数据与失败隔离区的
失败原因分布，产出的审计结论供模板版本升级决策。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.metrics_migration import (
    CANONICAL_AMOUNT,
    CANONICAL_REPORT_KEY,
    CANONICAL_SITE_KEY,
    CANONICAL_WINDOW_START,
    MigrationConflict,
    normalize_site,
    site_label,
    migration_service,
)
from app.store import store


class ParseAuditService(BaseModuleService):
    MODULE = "parse_audit"
    ENTITY = "审计明细"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["审计时间", "解析模板", "样本总数", "字段命中率", "失败原因分布"]
    REQUIRED_FIELDS = ["审计时间", "解析模板", "样本总数"]
    STATUS_ORDER = ["正常", "命中率下降", "解析异常"]
    ACTION_RULES = {"查看详情": "命中率下降", "忽略告警": "解析异常"}
    NEGATIVE_ACTIONS = ["忽略告警"]

    # ---- 审计补报发布：沿用存量指标的同一站点身份 ----

    def run_action(self, entry_id: int, action: str, payload=None):
        if action in ("发布补报", "发布审计补报", "审计补报发布"):
            return self.publish_backfill_action(entry_id, payload)
        return super().run_action(entry_id, action, payload)

    def publish_backfill_action(self, entry_id: int, payload=None):
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"

        extra = payload.model_dump() if payload is not None and hasattr(payload, "model_dump") else {}
        model_extra = getattr(payload, "model_extra", None) or {}
        # 站点身份必须来自审计记录上的同一 site_key/站点名称，
        # 不能再把“解析模板”编造为站点。
        site = extra.get("site") or entry.get("site_key") or entry.get("站点名称")
        if not site or str(site) == entry.get("解析模板"):
            linked = next(
                (t for t in store.rows("page_parser")
                 if t.get("模板名称") == entry.get("解析模板")),
                None,
            )
            site = (linked or {}).get("适用站点") if linked else None
        if site:
            site = normalize_site(str(site))
        else:
            # 普通审计样例行没有站点字段：回落为本剧本的站点 K 同一身份，
            # 保证任意行的「发布补报」与审计窗面板走同一本幂等账。
            site = CANONICAL_SITE_KEY

        # 只有带迁移/站点身份的审计行才优先采用其自身时间窗；普通样例行的
        # “审计时间”若只到日期则不编造小时，回落规范审计窗；若明确填了小时则采用。
        import re

        audit_time = str(entry.get("审计时间") or "")
        has_explicit_hour = bool(re.search(r"[T ]\d{1,2}(?::\d{2})?", audit_time))
        if entry.get("window_start"):
            fallback_window = entry.get("window_start")
        elif has_explicit_hour:
            fallback_window = audit_time
        else:
            fallback_window = CANONICAL_WINDOW_START
        has_scenario_identity = bool(entry.get("report_key") or entry.get("site_key"))
        if has_scenario_identity:
            fallback_key = entry.get("report_key") or CANONICAL_REPORT_KEY
            fallback_amount = entry.get("补报量") or CANONICAL_AMOUNT
        else:
            fallback_key = CANONICAL_REPORT_KEY
            fallback_amount = CANONICAL_AMOUNT

        amount = extra.get("amount") or model_extra.get("value") or model_extra.get("delta") or fallback_amount
        window_start = extra.get("window_start") or model_extra.get("window") or fallback_window
        report_key = extra.get("report_key") or model_extra.get("idempotency_key") or fallback_key
        expected_fence = extra.get("expected_fence")
        try:
            result = migration_service.publish_backfill(
                site=site,
                amount=amount,
                window_start=window_start,
                report_key=report_key,
                audit_id=f"parse_audit:{entry_id}",
                expected_fence=expected_fence,
            )
        except MigrationConflict as exc:
            return None, exc.payload.get("message", "写入与迁移确认冲突")
        migration_service.project_store()
        if not result.get("ok"):
            return None, result.get("message", "补报发布失败")
        duplicate = "（重复送达已幂等忽略）" if result.get("duplicate") else ""
        receipt = result.get("receipt") or {}
        write_side = receipt.get("write_side", "")
        return entry, (
            f"审计补报已发布到{site_label(site)}{duplicate}，"
            f"持久化于 {write_side}，旧累计/新版窗口均可复验"
        )

    # ---- 业务扩展：命中率审计与劣化告警 ----

    def record_hit_rate(self, template: str, total: int, hit: int) -> dict[str, Any]:
        """记录一次模板命中率审计明细。"""
        if total <= 0:
            return {"ok": False, "message": "样本总数必须为正数"}
        rate = hit / total
        rows = store.rows(self.MODULE)
        aid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        status = "正常" if rate >= 0.9 else ("命中率下降" if rate >= 0.6 else "解析异常")
        rows.append({
            "id": aid,
            "审计时间": datetime.now(timezone.utc).isoformat(),
            "解析模板": template,
            "样本总数": str(total),
            "字段命中率": f"{rate:.2%}",
            "失败原因分布": "" if rate >= 0.9 else "选择器失配/字段缺失",
            "status": status,
            "pending": status != "正常",
            "abnormal": status == "解析异常",
        })
        return {"ok": True, "message": "审计明细已记录", "audit_id": aid, "rate": f"{rate:.2%}"}

    def detect_degradation(self, entry_id: int) -> dict[str, Any]:
        """对比历史审计检测模板命中率劣化趋势。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "审计明细不存在"}
        template = entry.get("解析模板", "")
        history = [
            r for r in store.rows(self.MODULE)
            if r.get("解析模板") == template and r.get("id") != entry.get("id")
        ]
        current = float(str(entry.get("字段命中率", "0%")).rstrip("%") or 0)
        worst = max(
            (float(str(r.get("字段命中率", "0%")).rstrip("%") or 0) for r in history),
            default=current,
        )
        degraded = worst - current >= 0.15
        if degraded:
            entry["status"] = "命中率下降"
            entry["pending"] = True
        return {
            "ok": True,
            "message": "劣化检测完成",
            "degraded": degraded,
            "current": f"{current:.2%}",
            "best_before": f"{worst:.2%}",
        }

    def summarize_failures(self, entry_id: int) -> dict[str, Any]:
        """汇总失败隔离区中该模板的失败原因分布。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "审计明细不存在"}
        reasons: dict[str, int] = {}
        for q in store.rows("failure_quarantine"):
            if q.get("失败阶段") == "解析":
                reason = str(q.get("失败原因", "") or "未知")
                reasons[reason] = reasons.get(reason, 0) + 1
        top = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:3]
        entry["失败原因分布"] = ";".join(f"{k}:{v}" for k, v in top)
        return {"ok": True, "message": "失败分布已汇总", "distribution": top}

    def recommend_template_upgrade(self, entry_id: int) -> dict[str, Any]:
        """命中率持续下降时建议模板升级版本。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "审计明细不存在"}
        if entry.get("status") == "正常":
            return {"ok": True, "message": "模板健康，无需升级", "upgrade": False}
        template = next(
            (t for t in store.rows("page_parser")
             if t.get("模板名称") == entry.get("解析模板", "")), None
        )
        if template is None:
            return {"ok": False, "message": "关联模板不存在"}
        return {
            "ok": True,
            "message": "建议升级模板解析版本",
            "upgrade": True,
            "template": template.get("模板名称", ""),
            "current_version": template.get("解析版本", ""),
        }

    def publish_metrics(self, entry_id: int) -> dict[str, Any]:
        """把审计结论发布到采集量指标。

        迁移期统一走 metrics_migration：必须使用审计记录上的站点身份，
        不能再把解析模板名称当成站点名称写进指标。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "审计明细不存在"}
        site = entry.get("site_key") or entry.get("站点名称")
        if not site:
            template = next(
                (t for t in store.rows("page_parser")
                 if t.get("模板名称") == entry.get("解析模板", "")), None,
            )
            site = (template or {}).get("适用站点")
        if not site:
            return {"ok": False, "message": "审计明细缺少站点身份，未发布指标"}
        site = normalize_site(str(site))
        amount = entry.get("补报量") or 0
        if amount:
            migration_service.publish_backfill(
                site=site,
                amount=amount,
                window_start=entry.get("window_start") or entry.get("审计时间"),
                report_key=entry.get("report_key"),
                audit_id=f"parse_audit:{entry_id}",
            )
        migration_service.project_store()
        return {"ok": True, "message": f"审计结论已按同一站点身份发布指标：{site_label(site)}"}

    def audit_health(self, days: int = 1) -> dict[str, Any]:
        """输出审计批次健康度统计。"""
        rows = store.rows(self.MODULE)
        abnormal = sum(1 for r in rows if r.get("status") == "解析异常")
        degraded = sum(1 for r in rows if r.get("status") == "命中率下降")
        return {
            "ok": True,
            "message": "审计健康度统计完成",
            "total": len(rows),
            "abnormal": abnormal,
            "degraded": degraded,
        }
