"""采集量指标迁移领域：旧站点累计 + 小时级补报 -> 可持久化时间窗口版本。

核心不变量（也是上线核验/回滚复验的断言）：

1. 旧累计（如站点 K 的 37）没有时间细分，只进入站点总量与 ``baseline``，
   绝不写入任何小时窗口；
2. 小时级补报（如审计窗口迟到的 +2）落在由审计时间确定的正确小时窗；
3. 同一 ``report_key`` 的补报，无论重试、跨切换还是重启，只统计一次
   （receipt 主键 + 小时窗唯一约束 + 旧累计投影幂等）；
4. 旧读端 legacy-v1 始终能读到正确站点累计；新版读端 window-v2 给出
   “无时间基线 + 小时窗合计”的同口径总量；
5. 切换有 fence 乐观锁，写入与切换确认冲突时由 fence 仲裁，值班员可凭
   同一个 report_key 继续发布，不丢不重；
6. 回滚只改写入资格/相位，不删除任何已确认补报，切换后确认的新增在
   新旧读端都仍然可见，站点总量保持 39。

持久化使用标准库 sqlite3，数据库文件路径可用环境变量
``METRICS_DB_PATH`` 覆盖；默认落在 ``backend/data/metrics.db``，
因此跨进程重启状态不丢。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from app.store import store

# ---- 读端 / 写端版本与迁移相位 ----
READER_LEGACY = "legacy-v1"
READER_WINDOW = "window-v2"
WRITER_LEGACY = "legacy-cumulative-writer-v1"
WRITER_WINDOW = "windowed-metrics-writer-v2"

PHASE_BACKFILLING = "legacy_backfilling"   # 旧累计回填中/新版已部署，切换未确认
PHASE_SWITCHED = "switched_to_window"      # 已确认切换，窗口端有写入资格
PHASE_ROLLED_BACK = "rolled_back_legacy"   # 已回滚，旧累计端重新有写入资格

# ---- 演练剧本的规范身份与数值 ----
CANONICAL_SITE_KEY = "K"
CANONICAL_SITE_LABEL = "站点 K"
SITE_ALIASES = {"K", "k", "站点K", "站点 K"}
CANONICAL_LEGACY_TOTAL = 37
CANONICAL_WINDOW_START = "2026-11-26T09:00:00"
CANONICAL_AUDIT_TIME = "2026-11-26 09:00"
CANONICAL_WINDOW_LABEL = "2026-11-26 09:00–10:00"
CANONICAL_AMOUNT = 2
CANONICAL_REPORT_KEY = "audit-backfill:K:2026-11-26T09:00:+2"
CANONICAL_AUDIT_ID = "parse_audit:k-late-backfill"

_LOCK = threading.RLock()


class MigrationConflict(Exception):
    """写入携带的 fence 已过期：切换确认抢先提交。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("message", "迁移 fence 冲突"))
        self.payload = payload


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db_path() -> Path:
    override = os.environ.get("METRICS_DB_PATH", "").strip()
    if override:
        path = Path(override)
    else:
        # app/services/metrics_migration.py -> backend/
        path = Path(__file__).resolve().parents[2] / "data" / "metrics.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def normalize_site(site: str | None) -> str:
    raw = str(site or CANONICAL_SITE_KEY).strip()
    compact = raw.replace(" ", "")
    if raw in SITE_ALIASES or compact == "站点K" or raw.upper() == "K":
        return CANONICAL_SITE_KEY
    return compact or CANONICAL_SITE_KEY


def site_label(site: str) -> str:
    return CANONICAL_SITE_LABEL if site == CANONICAL_SITE_KEY else f"站点 {site}"


def report_key_for(site: str, iso_start: str, amount: int) -> str:
    """统一的幂等键格式（分钟精度），保证默认键与规范键一致。"""
    minute = iso_start[:16]  # YYYY-MM-DDTHH:MM
    return f"audit-backfill:{site}:{minute}:+{amount}"


def hour_window(value: str | None) -> tuple[str, str]:
    """把审计时间/窗口时间折算成整点小时窗。

    只有日期没有小时时不允许编造某一真实小时，调用方应直接给整点窗口。
    """
    text = str(value or "").strip()
    if not text:
        return CANONICAL_WINDOW_START, CANONICAL_WINDOW_LABEL
    normalized = text.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    dt: datetime | None = None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
        "%Y-%m-%dT%H",
    ):
        try:
            dt = datetime.strptime(normalized, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"无法识别的时间窗口：{text}") from exc
    start = dt.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    iso_start = start.strftime("%Y-%m-%dT%H:00:00")
    label = f"{start.strftime('%Y-%m-%d %H:00')}–{end.strftime('%H:00')}"
    return iso_start, label


class MetricsMigrationService:
    """所有方法线程安全；状态全部落 SQLite。"""

    # ---------- 基础设施 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path()), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """串行化所有写/事务；成功提交、异常回滚、最终关闭连接。"""
        with _LOCK:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS legacy_cumulative (
                site TEXT PRIMARY KEY,
                baseline INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                time_known INTEGER NOT NULL DEFAULT 0,
                note TEXT DEFAULT '',
                source_key TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hourly_window (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_label TEXT NOT NULL,
                delta INTEGER NOT NULL,
                report_key TEXT NOT NULL UNIQUE,
                audit_id TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(site, window_start, report_key)
            );
            CREATE TABLE IF NOT EXISTS receipt (
                report_key TEXT PRIMARY KEY,
                site TEXT NOT NULL,
                window_start TEXT DEFAULT '',
                window_label TEXT DEFAULT '',
                amount INTEGER NOT NULL,
                audit_id TEXT DEFAULT '',
                source TEXT DEFAULT 'parse_audit',
                write_side TEXT NOT NULL,
                fence_version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                replay_count INTEGER NOT NULL DEFAULT 0,
                legacy_applied INTEGER NOT NULL DEFAULT 1,
                legacy_seen_at TEXT,
                window_seen_at TEXT,
                created_at TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS migration_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                phase TEXT NOT NULL,
                fence_version INTEGER NOT NULL,
                deployed_version TEXT NOT NULL,
                switched_at TEXT DEFAULT '',
                rolled_back_at TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site TEXT NOT NULL,
                fence_version INTEGER NOT NULL,
                phase TEXT NOT NULL,
                legacy_total INTEGER NOT NULL,
                baseline_total INTEGER NOT NULL,
                hourly_total INTEGER NOT NULL,
                receipt_total INTEGER NOT NULL,
                window_total INTEGER NOT NULL,
                difference INTEGER NOT NULL,
                matched INTEGER NOT NULL,
                report_count INTEGER NOT NULL,
                statement TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                kind TEXT NOT NULL,
                site TEXT DEFAULT '',
                message TEXT NOT NULL,
                detail TEXT DEFAULT ''
            );
            """
        )
        row = conn.execute("SELECT id FROM migration_state WHERE id=1").fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO migration_state
                   (id, phase, fence_version, deployed_version, updated_at)
                   VALUES (1, ?, 1, ?, ?)""",
                (PHASE_BACKFILLING, READER_WINDOW, now_iso()),
            )

    def init_db(self) -> None:
        with self._db():
            pass

    def seed_canonical(self) -> None:
        """幂等接收站点 K 的旧累计 37（无时间细分）。"""
        with self._db() as conn:
            row = conn.execute(
                "SELECT site FROM legacy_cumulative WHERE site=?",
                (CANONICAL_SITE_KEY,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO legacy_cumulative
                       (site, baseline, total, time_known, note, source_key, updated_at)
                       VALUES (?, ?, ?, 0, ?, ?, ?)""",
                    (
                        CANONICAL_SITE_KEY,
                        CANONICAL_LEGACY_TOTAL,
                        CANONICAL_LEGACY_TOTAL,
                        "旧站点累计：迁移前数据库直接接收，无小时/时间细分",
                        "legacy-site-cumulative:K",
                        now_iso(),
                    ),
                )
                self._event(
                    conn,
                    "legacy_received",
                    CANONICAL_SITE_KEY,
                    "旧站点累计 37 接收入库：只进站点总量，不归属任何真实小时",
                )
        self.project_store()
        self.ensure_audit_row()

    # ---------- 事件 ----------

    def _event(
        self,
        conn: sqlite3.Connection,
        kind: str,
        site: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO event_log (at, kind, site, message, detail) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), kind, site, message, json.dumps(detail or {}, ensure_ascii=False)),
        )

    def events(self, site: str, limit: int = 30) -> list[dict[str, Any]]:
        site = normalize_site(site)
        with self._db() as conn:
            rows = conn.execute(
                "SELECT at, kind, site, message, detail FROM event_log "
                "WHERE site=? OR site='' ORDER BY id DESC LIMIT ?",
                (site, limit),
            ).fetchall()
        items = [dict(r) for r in rows]
        for item in items:
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except json.JSONDecodeError:
                item["detail"] = {}
        return list(reversed(items))

    # ---------- 相位 / fence ----------

    def migration_info(self) -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM migration_state WHERE id=1").fetchone()
        info = dict(row) if row else {}
        phase = info.get("phase", PHASE_BACKFILLING)
        info["write_side"] = (
            WRITER_WINDOW if phase == PHASE_SWITCHED else WRITER_LEGACY
        )
        info["write_side_label"] = (
            "新版时间窗口写入端 window-v2（切换后有写入资格）"
            if phase == PHASE_SWITCHED
            else "旧累计写入端 legacy-v1（切换前/回滚后有写入资格）"
        )
        phase_labels = {
            PHASE_BACKFILLING: "旧累计回填中 · 新版已部署 · 切换待确认",
            PHASE_SWITCHED: "已切换到时间窗口版",
            PHASE_ROLLED_BACK: "已回滚到旧累计读端（新增数据保留）",
        }
        info["phase_label"] = phase_labels.get(phase, phase)
        return info

    # ---------- 旧累计接收入口 ----------

    def receive_legacy_total(self, site: str, total: int) -> dict[str, Any]:
        site = normalize_site(site)
        total = int(total)
        if total < 0:
            return {"ok": False, "message": "旧累计不能为负数"}
        with self._db() as conn:
            receipt_sum = self._receipt_sum(conn, site)
            conn.execute(
                """INSERT INTO legacy_cumulative
                   (site, baseline, total, time_known, note, source_key, updated_at)
                   VALUES (?, ?, ?, 0, '旧站点累计：无时间细分', ?, ?)
                   ON CONFLICT(site) DO UPDATE SET
                     baseline=excluded.baseline,
                     total=excluded.baseline + ?,
                     time_known=0,
                     updated_at=excluded.updated_at""",
                (
                    site, total, total + receipt_sum,
                    f"legacy-site-cumulative:{site}", now_iso(), receipt_sum,
                ),
            )
            self._event(
                conn, "legacy_received", site,
                f"旧站点累计 {total} 接收入库（幂等覆盖基线），不写入任何小时窗",
            )
        self.project_store()
        return {"ok": True, "message": f"旧累计 {total} 已入库", **self.read_legacy(site)}

    # ---------- 补报写入（幂等优先 + fence 仲裁） ----------

    def publish_backfill(
        self,
        *,
        site: str | None = None,
        amount: int | str = CANONICAL_AMOUNT,
        window_start: str | None = None,
        report_key: str | None = None,
        audit_id: str | None = None,
        expected_fence: int | None = None,
        source: str = "parse_audit",
    ) -> dict[str, Any]:
        site = normalize_site(site)
        try:
            amount = int(str(amount).strip().lstrip("+"))
        except (TypeError, ValueError):
            return {"ok": False, "message": f"补报量必须是整数，收到：{amount!r}"}
        if amount <= 0:
            return {"ok": False, "message": "迟到补报量必须为正整数"}
        iso_start, label = hour_window(window_start or CANONICAL_WINDOW_START)
        key = (report_key or "").strip() or report_key_for(site, iso_start, amount)
        audit_id = audit_id or CANONICAL_AUDIT_ID

        with self._db() as conn:
            state_row = conn.execute(
                "SELECT phase, fence_version FROM migration_state WHERE id=1"
            ).fetchone()
            current_fence = int(state_row["fence_version"])
            phase = state_row["phase"]

            # 幂等检查优先于 fence：同一审计补报的任何重试都必须返回首次结果，
            # 即使它携带的是旧 fence（跨切换/重启场景）。
            existing = conn.execute(
                "SELECT * FROM receipt WHERE report_key=?", (key,)
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE receipt SET replay_count=replay_count+1 WHERE report_key=?",
                    (key,),
                )
                self._event(
                    conn, "duplicate_ignored", site,
                    f"同一审计补报 {key} 重复送达，幂等忽略，仍然只统计一次",
                    {"report_key": key},
                )
                duplicated = True
            elif expected_fence is not None and int(expected_fence) != current_fence:
                # 过期 fence 且从未入库：拒绝，等待值班员用新 fence 继续发布。
                self._event(
                    conn, "write_conflict", site,
                    f"补报 {key} 携带 fence={expected_fence}，当前 fence={current_fence}，"
                    "写入与迁移确认冲突，已拒绝本次过期写入（未落库）",
                    {"report_key": key},
                )
                raise MigrationConflict(self._conflict_payload(
                    site=site, key=key, amount=amount, iso_start=iso_start, label=label,
                    audit_id=audit_id, expected_fence=int(expected_fence),
                    current_fence=current_fence, phase=phase,
                ))
            else:
                write_side = (
                    WRITER_WINDOW if phase == PHASE_SWITCHED else WRITER_LEGACY
                )
                ts = now_iso()
                conn.execute(
                    """INSERT INTO receipt
                       (report_key, site, window_start, window_label, amount, audit_id,
                        source, write_side, fence_version, status, replay_count,
                        legacy_applied, created_at, confirmed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', 0, 1, ?, ?)""",
                    (key, site, iso_start, label, amount, audit_id, source,
                     write_side, current_fence, ts, ts),
                )
                conn.execute(
                    """INSERT INTO hourly_window
                       (site, window_start, window_label, delta, report_key, audit_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (site, iso_start, label, amount, key, audit_id, ts),
                )
                # 旧累计读端投影：receipt 主键保证同一 key 只加一次。
                conn.execute(
                    """INSERT INTO legacy_cumulative
                       (site, baseline, total, time_known, note, source_key, updated_at)
                       VALUES (?, 0, ?, 0, '由已确认小时补报投影', 'migration', ?)
                       ON CONFLICT(site) DO UPDATE SET
                         total=legacy_cumulative.total + excluded.total,
                         updated_at=excluded.updated_at""",
                    (site, amount, ts),
                )
                self._event(
                    conn, "backfill_confirmed", site,
                    f"迟到补报 +{amount} 已持久化：{label}；写入资格端={write_side}，"
                    f"fence={current_fence}，幂等键 {key}",
                    {"report_key": key, "window": label, "write_side": write_side},
                )
                duplicated = False

        self.project_store()
        receipt = self.receipt_info(key)
        return {
            "ok": True,
            "duplicate": duplicated,
            "message": (
                "该审计补报已确认过，重复发布已幂等忽略，只统计一次"
                if duplicated
                else f"补报 +{amount} 已确认并持久化到{label}"
            ),
            "receipt": receipt,
            # 内嵌读模型不标记“读端已收到”；只有真正查询对应读端才标记。
            "legacy": self.read_legacy(site, mark_seen=False),
            "windows": self.read_windows(site, mark_seen=False),
        }

    def conflict_drill(self, **kwargs: Any) -> dict[str, Any]:
        """确定性复现“写入与迁移确认冲突”。

        先让切换确认按当前 fence 抢先提交（fence+1），随后调用方按旧
        fence 提交补报会得到 409。补报本身绝不落库；前端拿 retry 载荷
        让值班员“继续发布”。
        """
        site = normalize_site(kwargs.get("site"))
        amount = int(str(kwargs.get("amount", CANONICAL_AMOUNT)).lstrip("+"))
        iso_start, label = hour_window(kwargs.get("window_start"))
        key = (kwargs.get("report_key") or "").strip() or report_key_for(
            site, iso_start, amount
        )
        with self._db() as conn:
            row = conn.execute(
                "SELECT phase, fence_version FROM migration_state WHERE id=1"
            ).fetchone()
            old_fence = int(row["fence_version"])
            self._commit_switch(conn)
            new_fence = old_fence + 1
            self._event(
                conn, "write_conflict", site,
                "【冲突演练】补报写入与迁移确认同时提交，切换确认先行，"
                f"补报的旧 fence={old_fence} 被拒（当前 fence={new_fence}），"
                "补报未落库，等待值班员继续发布",
                {"report_key": key},
            )
        # 留一份冲突发生时（尚无新补报）的对账单：37 == 37。
        self.reconcile(site)
        raise MigrationConflict(self._conflict_payload(
            site=site, key=key, amount=amount, iso_start=iso_start, label=label,
            audit_id=kwargs.get("audit_id") or CANONICAL_AUDIT_ID,
            expected_fence=old_fence, current_fence=new_fence,
            phase=PHASE_SWITCHED, drill=True,
        ))

    def _conflict_payload(
        self,
        *,
        site: str,
        key: str,
        amount: int,
        iso_start: str,
        label: str,
        audit_id: str,
        expected_fence: int,
        current_fence: int,
        phase: str,
        drill: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "migration_conflict",
            "conflict": True,
            "drill": drill,
            "message": (
                "写入与迁移确认发生冲突：补报携带的切换版本 "
                f"fence={expected_fence} 已过期，当前 fence={current_fence}"
            ),
            "site": site,
            "expected_fence": expected_fence,
            "current_fence": current_fence,
            "current_phase": phase,
            "retry": {
                "site": site,
                "amount": amount,
                "window_start": iso_start,
                "report_key": key,
                "audit_id": audit_id,
                "expected_fence": current_fence,
            },
        }

    # ---------- 读端 ----------

    def _receipt_sum(self, conn: sqlite3.Connection, site: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM receipt WHERE site=?",
            (site,),
        ).fetchone()
        return int(row["s"])

    def read_legacy(self, site: str | None = None, mark_seen: bool = True) -> dict[str, Any]:
        site = normalize_site(site)
        ts = now_iso()
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM legacy_cumulative WHERE site=?", (site,)
            ).fetchone()
            baseline = int(row["baseline"]) if row else 0
            total = int(row["total"]) if row else baseline
            if mark_seen:
                conn.execute(
                    "UPDATE receipt SET legacy_seen_at=? "
                    "WHERE site=? AND legacy_seen_at IS NULL",
                    (ts, site),
                )
            receipts = [dict(r) for r in conn.execute(
                "SELECT report_key, amount, window_label, legacy_seen_at, confirmed_at "
                "FROM receipt WHERE site=? ORDER BY confirmed_at, rowid",
                (site,),
            ).fetchall()]
        for item in receipts:
            item["reader_received"] = bool(item.get("legacy_seen_at"))
        basis: list[dict[str, Any]] = []
        if baseline:
            basis.append({
                "kind": "legacy_baseline",
                "label": "旧站点累计（无时间细分，不计入任何小时）",
                "amount": baseline,
                "time_scoped": False,
            })
        for item in receipts:
            basis.append({
                "kind": "confirmed_backfill",
                "label": f"已确认小时补报 {item['window_label']}",
                "amount": int(item["amount"]),
                "time_scoped": True,
                "report_key": item["report_key"],
            })
        return {
            "ok": True,
            "reader": "legacy",
            "reader_version": READER_LEGACY,
            "reader_label": "旧累计读端 legacy-v1",
            "site": site,
            "site_label": site_label(site),
            "cumulative_total": total,
            "baseline_total": baseline,
            "received_reports": receipts,
            "basis": basis,
            "read_at": ts,
            # 旧版接口形状，方便原读端调用方直接消费
            "legacy_row": {
                "统计时间": "站点累计口径（旧累计无时间细分）",
                "站点名称": site_label(site),
                "抓取量": str(total),
                "解析量": str(total),
                "产出量": str(total),
                "累计总量": total,
            },
        }

    def read_windows(self, site: str | None = None, mark_seen: bool = True) -> dict[str, Any]:
        site = normalize_site(site)
        ts = now_iso()
        with self._db() as conn:
            row = conn.execute(
                "SELECT baseline FROM legacy_cumulative WHERE site=?", (site,)
            ).fetchone()
            baseline = int(row["baseline"]) if row else 0
            if mark_seen:
                conn.execute(
                    "UPDATE receipt SET window_seen_at=? "
                    "WHERE site=? AND window_seen_at IS NULL",
                    (ts, site),
                )
            win_rows = conn.execute(
                """SELECT window_start, window_label, SUM(delta) AS delta,
                           COUNT(*) AS report_count,
                           GROUP_CONCAT(report_key) AS report_keys
                    FROM hourly_window WHERE site=?
                    GROUP BY window_start, window_label
                    ORDER BY window_start""",
                (site,),
            ).fetchall()
            receipts = [dict(r) for r in conn.execute(
                "SELECT report_key, amount, window_label, window_seen_at, confirmed_at "
                "FROM receipt WHERE site=? ORDER BY confirmed_at, rowid",
                (site,),
            ).fetchall()]
        windows: list[dict[str, Any]] = []
        hourly_total = 0
        for r in win_rows:
            delta = int(r["delta"])
            hourly_total += delta
            windows.append({
                "window_start": r["window_start"],
                "window_label": r["window_label"],
                "delta": delta,
                "report_count": int(r["report_count"]),
                "report_keys": (r["report_keys"] or "").split(","),
                "time_scoped": True,
            })
        for item in receipts:
            item["reader_received"] = bool(item.get("window_seen_at"))
        return {
            "ok": True,
            "reader": "window",
            "reader_version": READER_WINDOW,
            "reader_label": "新版时间窗口读端 window-v2",
            "site": site,
            "site_label": site_label(site),
            "baseline_total": baseline,
            "baseline": {
                "label": "迁移前旧累计：无时间细分，放在总量基线，不分配到任何小时",
                "amount": baseline,
                "time_scoped": False,
                "appears_in_hourly_windows": False,
            },
            "windows": windows,
            "hourly_total": hourly_total,
            "window_total": baseline + hourly_total,
            "total": baseline + hourly_total,
            "received_reports": receipts,
            "read_at": ts,
        }

    # ---------- 对账单 / 切换 / 回滚 ----------

    def _converge_legacy(self, conn: sqlite3.Connection, site: str) -> int:
        """按 receipt 幂等校准旧累计 = baseline + 已确认补报合计。"""
        row = conn.execute(
            "SELECT baseline FROM legacy_cumulative WHERE site=?", (site,)
        ).fetchone()
        baseline = int(row["baseline"]) if row else 0
        expected = baseline + self._receipt_sum(conn, site)
        if row is not None:
            conn.execute(
                "UPDATE legacy_cumulative SET total=?, updated_at=? WHERE site=?",
                (expected, now_iso(), site),
            )
        else:
            conn.execute(
                """INSERT INTO legacy_cumulative
                   (site, baseline, total, time_known, note, updated_at)
                   VALUES (?, 0, ?, 0, '由对账单补齐', ?)""",
                (site, expected, now_iso()),
            )
        return expected

    def reconcile(self, site: str | None = None, persist: bool = True) -> dict[str, Any]:
        site = normalize_site(site)
        with self._db() as conn:
            self._converge_legacy(conn, site)
            m = conn.execute(
                "SELECT fence_version, phase FROM migration_state WHERE id=1"
            ).fetchone()
            fence = int(m["fence_version"])
            phase = m["phase"]
            legacy_row = conn.execute(
                "SELECT baseline, total FROM legacy_cumulative WHERE site=?", (site,)
            ).fetchone()
            baseline = int(legacy_row["baseline"]) if legacy_row else 0
            legacy_total = int(legacy_row["total"]) if legacy_row else 0
            hourly_total = int(conn.execute(
                "SELECT COALESCE(SUM(delta),0) s FROM hourly_window WHERE site=?",
                (site,),
            ).fetchone()["s"])
            receipt_total = self._receipt_sum(conn, site)
            report_count = int(conn.execute(
                "SELECT COUNT(*) c FROM receipt WHERE site=?", (site,)
            ).fetchone()["c"])
            distinct_keys = int(conn.execute(
                "SELECT COUNT(DISTINCT report_key) c FROM receipt WHERE site=?", (site,)
            ).fetchone()["c"])

            window_total = baseline + hourly_total
            difference = legacy_total - window_total
            matched = (
                difference == 0
                and hourly_total == receipt_total
                and report_count == distinct_keys
            )
            statement = "\n".join([
                f"站点：{site_label(site)}",
                f"旧读端站点累计（含已确认补报）：{legacy_total}",
                f"无时间旧累计基线：{baseline}（只在总量，不计入任何小时窗）",
                f"小时窗口补报合计：{hourly_total}（{report_count} 笔，去重后 {distinct_keys} 个幂等键）",
                f"新版窗口总量：{baseline} + {hourly_total} = {window_total}",
                f"新旧差异：{difference}",
                "对账结论：账实相符，累计可复验" if matched else f"对账不平：差异 {difference}",
                f"fence={fence}；相位={phase}；生成时间={now_iso()}",
            ])
            created = now_iso()
            if persist:
                conn.execute(
                    """INSERT INTO reconciliation
                       (site, fence_version, phase, legacy_total, baseline_total,
                        hourly_total, receipt_total, window_total, difference,
                        matched, report_count, statement, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (site, fence, phase, legacy_total, baseline, hourly_total,
                     receipt_total, window_total, difference, 1 if matched else 0,
                     report_count, statement, created),
                )
                self._event(
                    conn, "reconcile", site,
                    "切换对账单复验：" + ("账实相符" if matched else f"存在差异 {difference}"),
                    {"legacy_total": legacy_total, "window_total": window_total},
                )

        return {
            "ok": True,
            "site": site,
            "fence_version": fence,
            "phase": phase,
            "legacy_total": legacy_total,
            "baseline_total": baseline,
            "hourly_total": hourly_total,
            "receipt_total": receipt_total,
            "window_total": window_total,
            "difference": difference,
            "matched": matched,
            "report_count": report_count,
            "statement": statement,
            "created_at": created,
            "message": (
                "切换对账单复验通过：累计一致、无重复统计"
                if matched else "切换对账单复验不平，请检查差异"
            ),
        }

    def latest_reconciliation(self, site: str | None = None) -> dict[str, Any] | None:
        site = normalize_site(site)
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM reconciliation WHERE site=? ORDER BY id DESC LIMIT 1",
                (site,),
            ).fetchone()
        return dict(row) if row else None

    def _commit_switch(self, conn: sqlite3.Connection) -> int:
        """调用方已在 _db 事务内：执行切换确认。返回新 fence。"""
        row = conn.execute("SELECT fence_version FROM migration_state WHERE id=1").fetchone()
        new_fence = int(row["fence_version"]) + 1
        ts = now_iso()
        conn.execute(
            """UPDATE migration_state
               SET phase=?, fence_version=?, switched_at=?, updated_at=? WHERE id=1""",
            (PHASE_SWITCHED, new_fence, ts, ts),
        )
        self._event(
            conn, "switch_confirmed", CANONICAL_SITE_KEY,
            f"迁移切换已确认：时间窗口版成为写入资格端（window-v2），fence={new_fence}",
        )
        return new_fence

    def confirm_switch(self, expected_fence: int | None = None) -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT fence_version, phase FROM migration_state WHERE id=1"
            ).fetchone()
            current = int(row["fence_version"])
            if expected_fence is not None and int(expected_fence) != current:
                raise MigrationConflict(self._conflict_payload(
                    site=CANONICAL_SITE_KEY, key="", amount=0,
                    iso_start="", label="", audit_id="",
                    expected_fence=int(expected_fence), current_fence=current,
                    phase=row["phase"],
                ))
            self._converge_legacy(conn, CANONICAL_SITE_KEY)
            new_fence = self._commit_switch(conn)
        reconciliation = self.reconcile(CANONICAL_SITE_KEY)
        self.project_store()
        return {
            "ok": True,
            "message": "已确认切换到可持久化时间窗口版，切换对账单已生成",
            "migration": self.migration_info(),
            "reconciliation": reconciliation,
        }

    def rollback(self) -> dict[str, Any]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT fence_version FROM migration_state WHERE id=1"
            ).fetchone()
            new_fence = int(row["fence_version"]) + 1
            ts = now_iso()
            conn.execute(
                """UPDATE migration_state
                   SET phase=?, fence_version=?, rolled_back_at=?, updated_at=? WHERE id=1""",
                (PHASE_ROLLED_BACK, new_fence, ts, ts),
            )
            # 刻意不删除 hourly_window / receipt / legacy 增量：
            # 回滚不得舍弃切换后已确认的新增。
            self._converge_legacy(conn, CANONICAL_SITE_KEY)
            self._event(
                conn, "rollback", CANONICAL_SITE_KEY,
                f"已模拟回滚：旧累计读端恢复，fence={new_fence}；"
                "已确认补报与小时窗全部保留，不丢弃切换后新增",
            )
        reconciliation = self.reconcile(CANONICAL_SITE_KEY)
        self.project_store()
        return {
            "ok": True,
            "message": "已回滚：新旧读端数据保留，已确认新增不丢失",
            "migration": self.migration_info(),
            "reconciliation": reconciliation,
        }

    # ---------- 单据 / 总状态 / 重置 ----------

    def receipt_info(self, report_key: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM receipt WHERE report_key=?", (report_key,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["legacy_reader_received"] = bool(data.get("legacy_seen_at"))
        data["window_reader_received"] = bool(data.get("window_seen_at"))
        data["read_versions_received"] = [
            v for v, flag in (
                (READER_LEGACY, data["legacy_reader_received"]),
                (READER_WINDOW, data["window_reader_received"]),
            ) if flag
        ]
        return data

    def list_receipts(self, site: str | None = None) -> list[dict[str, Any]]:
        site = normalize_site(site)
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM receipt WHERE site=? ORDER BY confirmed_at, rowid",
                (site,),
            ).fetchall()
        items = [dict(r) for r in rows]
        for data in items:
            data["legacy_reader_received"] = bool(data.get("legacy_seen_at"))
            data["window_reader_received"] = bool(data.get("window_seen_at"))
            data["read_versions_received"] = [
                v for v, flag in (
                    (READER_LEGACY, data["legacy_reader_received"]),
                    (READER_WINDOW, data["window_reader_received"]),
                ) if flag
            ]
        return items

    def state(self, site: str | None = None) -> dict[str, Any]:
        site = normalize_site(site)
        return {
            "ok": True,
            "site": site,
            "site_label": site_label(site),
            "canonical_report_key": CANONICAL_REPORT_KEY,
            "canonical_window": {
                "window_start": CANONICAL_WINDOW_START,
                "window_label": CANONICAL_WINDOW_LABEL,
                "audit_time": CANONICAL_AUDIT_TIME,
                "amount": CANONICAL_AMOUNT,
            },
            "migration": self.migration_info(),
            "legacy": self.read_legacy(site, mark_seen=False),
            "windows": self.read_windows(site, mark_seen=False),
            "reconciliation": self.latest_reconciliation(site),
            "receipts": self.list_receipts(site),
            "events": self.events(site),
        }

    def reset_scenario(self, site: str | None = None) -> dict[str, Any]:
        site = normalize_site(site)
        with self._db() as conn:
            for table in ("receipt", "hourly_window", "reconciliation", "event_log"):
                conn.execute(f"DELETE FROM {table} WHERE site=?", (site,))
            conn.execute("DELETE FROM legacy_cumulative WHERE site=?", (site,))
            conn.execute(
                "UPDATE migration_state SET phase=?, fence_version=1, "
                "switched_at='', rolled_back_at='', updated_at=? WHERE id=1",
                (PHASE_BACKFILLING, now_iso()),
            )
            conn.execute(
                """INSERT INTO legacy_cumulative
                   (site, baseline, total, time_known, note, source_key, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (
                    site, CANONICAL_LEGACY_TOTAL, CANONICAL_LEGACY_TOTAL,
                    "旧站点累计：迁移前数据库直接接收，无小时/时间细分",
                    f"legacy-site-cumulative:{site}", now_iso(),
                ),
            )
            self._event(
                conn, "reset", site,
                "演练已重置：恢复为旧累计 37、无补报、fence=1、切换待确认",
            )
        self.project_store()
        return self.state(site)

    # ---------- 与内存存量模块的同一站点身份投影 ----------

    def project_store(self) -> None:
        """把持久化口径投影进通用 collect_metrics 列表，保持原读端/通用页一致。"""
        try:
            legacy = self.read_legacy(CANONICAL_SITE_KEY, mark_seen=False)
            windows = self.read_windows(CANONICAL_SITE_KEY, mark_seen=False)
            info = self.migration_info()
        except Exception:
            return
        rows = store.rows("collect_metrics")
        row = next(
            (r for r in rows
             if r.get("site_key") == CANONICAL_SITE_KEY
             or str(r.get("站点名称", "")).replace(" ", "") == "站点K"
             or r.get("站点名称") in SITE_ALIASES),
            None,
        )
        total = legacy["cumulative_total"]
        latest_window = windows["windows"][-1]["window_label"] if windows["windows"] else ""
        time_cell = (
            f"旧累计无时间细分；最新补报窗 {latest_window}"
            if latest_window else "站点累计口径（旧累计无时间细分）"
        )
        payload = {
            "统计时间": time_cell,
            "站点名称": CANONICAL_SITE_LABEL,
            "抓取量": str(total),
            "解析量": str(total),
            "产出量": str(total),
            "status": "正常",
            "pending": False,
            "abnormal": False,
            "site_key": CANONICAL_SITE_KEY,
            "累计总量": total,
            "旧累计基线": legacy["baseline_total"],
            "小时补报合计": windows["hourly_total"],
            "migration_phase": info.get("phase"),
            "write_side": info.get("write_side"),
            "reader_versions": [READER_LEGACY, READER_WINDOW],
        }
        if row is None:
            mid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            row = {"id": mid}
            rows.append(row)
        row.update(payload)

    def ensure_audit_row(self) -> None:
        """在解析质量审计模块放同站点身份的迟到补报审计窗。"""
        rows = store.rows("parse_audit")
        marker = "k-late-backfill"
        if any(r.get("scenario_key") == marker for r in rows):
            return
        mid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": mid,
            "status": "命中率下降",
            "pending": True,
            "abnormal": False,
            "审计时间": CANONICAL_AUDIT_TIME,
            "解析模板": "K站点-商品详情解析模板-v7",
            "样本总数": "128",
            "字段命中率": "88.28%",
            "失败原因分布": "选择器失配:15",
            # 与 collect_metrics 完全相同的站点身份
            "站点名称": CANONICAL_SITE_LABEL,
            "site_key": CANONICAL_SITE_KEY,
            "补报量": str(CANONICAL_AMOUNT),
            "window_start": CANONICAL_WINDOW_START,
            "window_label": CANONICAL_WINDOW_LABEL,
            "report_key": CANONICAL_REPORT_KEY,
            "audit_ref": CANONICAL_AUDIT_ID,
            "补报事由": "审计窗口迟到补报",
            "scenario_key": marker,
        })


migration_service = MetricsMigrationService()
