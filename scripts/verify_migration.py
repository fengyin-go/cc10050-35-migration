#!/usr/bin/env python3
"""采集量迁移：构建/上线核验脚本（仅标准库 + 后端领域服务）。

在临时 SQLite 文件上完整复演题目剧本，不影响正在运行的服务数据库：

    旧累计 37（无时间） -> 审计迟到补报 +2 与切换确认冲突
    -> 值班员继续发布（同一幂等键只计一次）
    -> 旧读端/新窗口读端均为 39，2 落在正确小时窗
    -> 切换对账单相符 -> 回滚后仍为 39 且小时明细保留
    -> “重启”（新服务实例打开同一文件）后状态持久、重放幂等

用法：
    python3 scripts/verify_migration.py
    backend/.venv/bin/python scripts/verify_migration.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# 必须在导入 app 之前指定独立库，避免触碰默认的 backend/data/metrics.db。
_TMP_DB = Path(tempfile.gettempdir()) / "verify_migration_scenario.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(_TMP_DB) + suffix)
    if p.exists():
        p.unlink()
os.environ["METRICS_DB_PATH"] = str(_TMP_DB)

from app.services.metrics_migration import (  # noqa: E402
    CANONICAL_REPORT_KEY,
    MigrationConflict,
    MetricsMigrationService,
)


def main() -> int:
    m = MetricsMigrationService()
    m.seed_canonical()
    failures: list[str] = []

    def expect(name: str, cond: bool, detail: str = "") -> None:
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    st = m.state("K")
    expect("启动即接收旧累计 37", st["legacy"]["cumulative_total"] == 37)
    expect("初始无任何小时窗", st["windows"]["windows"] == [])

    # 冲突：切换确认抢先，旧 fence 写入被拒且未落库。
    conflict_payload = None
    try:
        m.conflict_drill()
    except MigrationConflict as exc:
        conflict_payload = exc.payload
    expect("写入与迁移确认冲突返回 fence 仲裁", conflict_payload is not None
           and conflict_payload["code"] == "migration_conflict")
    expect("冲突时补报未落库（仍 37）", m.read_legacy(mark_seen=False)["cumulative_total"] == 37)

    # 值班员“继续发布”：用 retry 载荷（同一 report_key + 新 fence）。
    result = m.publish_backfill(**conflict_payload["retry"])
    expect("继续发布成功且非重复单", result["ok"] and result["duplicate"] is False)
    expect("补报持久化到切换后有资格的 window-v2 端",
           result["receipt"]["write_side"] == "windowed-metrics-writer-v2")

    legacy = m.read_legacy()
    windows = m.read_windows()
    expect("旧读端累计 = 39", legacy["cumulative_total"] == 39)
    expect("新窗口读端总量 = 39", windows["total"] == 39)
    expect("无时间基线仍为 37", windows["baseline"]["amount"] == 37
           and windows["baseline"]["appears_in_hourly_windows"] is False)
    expect("恰好一个小时窗且 +2", len(windows["windows"]) == 1
           and windows["windows"][0]["delta"] == 2)
    expect("37 未进入任何小时 delta",
           all(w["delta"] != 37 for w in windows["windows"]))
    expect("2 落在审计确定的 09:00–10:00 窗",
           windows["windows"][0]["window_label"] == "2026-11-26 09:00–10:00")

    receipt = m.receipt_info(CANONICAL_REPORT_KEY)
    expect("两读端查询后可查接收版本",
           receipt["read_versions_received"] == ["legacy-v1", "window-v2"],
           str(receipt["read_versions_received"]))

    # 同一补报跨切换/带旧 fence 重放：幂等且仍 39。
    replay = m.publish_backfill(expected_fence=1)
    expect("同一审计补报重放被幂等识别", replay["duplicate"] is True)
    expect("重放后总量仍 39", replay["legacy"]["cumulative_total"] == 39)

    rec = m.reconcile()
    expect("切换对账单相符（差异 0）", rec["matched"] is True
           and rec["legacy_total"] == 39 and rec["window_total"] == 39)
    print("\n===== 切换对账单 =====")
    print(rec["statement"])

    rb = m.rollback()
    expect("回滚后对账单仍相符", rb["reconciliation"]["matched"] is True)
    expect("回滚后旧读端仍为 39", m.read_legacy()["cumulative_total"] == 39)
    w2 = m.read_windows()
    expect("回滚后新版窗口仍为 39", w2["total"] == 39)
    expect("回滚不丢弃 +2 小时明细",
           len(w2["windows"]) == 1 and w2["windows"][0]["delta"] == 2)

    # 模拟重启：新服务实例 + 同一数据库文件。
    m2 = MetricsMigrationService()
    expect("重启后旧读端持久为 39", m2.read_legacy(mark_seen=False)["cumulative_total"] == 39)
    expect("重启后窗口明细持久保留", len(m2.read_windows(mark_seen=False)["windows"]) == 1)
    replay2 = m2.publish_backfill()
    expect("重启后同一补报仍只统计一次（duplicate）", replay2["duplicate"] is True
           and replay2["legacy"]["cumulative_total"] == 39)

    print()
    if failures:
        print(f"核验失败：{len(failures)} 项 -> {failures}")
        return 1
    print("全部核验通过：37 只在总量，2 在正确小时窗，切换/回滚/重启后站点累计均为 39，且只统计一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
