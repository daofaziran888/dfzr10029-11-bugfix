"""分析实验服务：按显著阈值版本重算分组差异显著性与基准区间。

加工链路（加工步骤落在读取既有数据之后）：
1. 读取既有数据：实验方案、分组观测值、显著阈值版本；
2. 阈值版本与基准区间一起参与计算，做两比例 z 检验；
3. 规则校验：p 值超出显著阈值、指标值落在基准区间外时记录原因；
4. 阈值判定记录按唯一键 upsert，重复提交只保留一份有效结果。

阈值判定记录同时保留业务输入快照（样本量、转化数、阈值、基准区间），
保证详情页看到的结论可以完整回溯到判定时使用的口径，而不是引用旧区间。
"""
from __future__ import annotations

import math
import random
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "experiment"
VERSION_MODULE = "experiment_threshold_version"
OBSERVATION_MODULE = "experiment_group_observation"
JUDGEMENT_MODULE = "experiment_threshold_judgement"

REQUIRED_FIELDS = ["方案编号", "实验名称", "分析数据集"]
STATUS_ORDER = ["草稿", "已设计", "运行中", "已完成"]
ACTION_RULES = {"提交设计": "已设计", "运行实验": "运行中", "完成实验": "已完成"}
NEGATIVE_ACTIONS: list[str] = []

# 阈值版本冲突时的两种处理范围：
# frozen  -> 按实验设计阶段冻结的显著阈值判定；
# runtime -> 按运行期修改后当前生效的显著阈值判定。
SCOPE_FROZEN = "设计冻结值"
SCOPE_RUNTIME = "运行期生效值"
SCOPES = (SCOPE_FROZEN, SCOPE_RUNTIME)

# 判定记录唯一键：同一键重算只覆盖同一份记录，杜绝重复提交产生多份结果。
JUDGEMENT_KEY = ("实验方案id", "分组", "阈值版本id", "处理范围")


def _norm(value: Any) -> float:
    """把种子里的数值/字符串统一转成 float，无法解析时抛出可读错误。"""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"数值字段无法解析：{value!r}") from exc


class ExperimentService:
    # ------------------------------------------------------------------
    # 阈值版本与既有数据读取
    # ------------------------------------------------------------------
    def list_threshold_versions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in store.rows(VERSION_MODULE)]

    def get_threshold_version(self, version_id: int) -> dict[str, Any] | None:
        return store.find(VERSION_MODULE, version_id)

    def _resolve_version(self, version_id: int | None, scope: str) -> dict[str, Any]:
        """按处理范围解析阈值版本；显式传了版本号时以版本号为准。"""
        rows = store.rows(VERSION_MODULE)
        if version_id is not None:
            version = store.find(VERSION_MODULE, version_id)
            if version is None:
                raise ValueError(f"显著阈值版本 {version_id} 不存在")
            return dict(version)
        if scope == SCOPE_FROZEN:
            candidates = [row for row in rows if row.get("版本来源") == "设计冻结"]
        else:
            candidates = [row for row in rows if row.get("生效状态") == "生效中"]
        if not candidates:
            raise ValueError(f"处理范围「{scope}」下没有可用的显著阈值版本")
        return dict(candidates[0])

    def _observations(self, experiment_id: int | None) -> list[dict[str, Any]]:
        rows = store.rows(OBSERVATION_MODULE)
        if experiment_id is not None:
            rows = [row for row in rows if int(row.get("实验方案id", 0)) == experiment_id]
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 核心加工：阈值版本 + 基准区间一起参与显著性计算
    # ------------------------------------------------------------------
    def _judge_one(
        self,
        observation: dict[str, Any],
        version: dict[str, Any],
        scope: str,
    ) -> dict[str, Any]:
        """对单个分组按指定阈值版本重算显著性与基准区间判定。"""
        n_t = _norm(observation["实验组样本量"])
        x_t = _norm(observation["实验组转化数"])
        n_c = _norm(observation["对照组样本量"])
        x_c = _norm(observation["对照组转化数"])
        if n_t <= 0 or n_c <= 0:
            raise ValueError(f"分组 {observation.get('分组')} 样本量必须为正数")
        if not (0 <= x_t <= n_t and 0 <= x_c <= n_c):
            raise ValueError(f"分组 {observation.get('分组')} 转化数超出样本量范围")

        p_t = x_t / n_t
        p_c = x_c / n_c
        diff = round(p_t - p_c, 6)
        pooled = (x_t + x_c) / (n_t + n_c)
        # 合并比例为 0 或 1 时标准误为 0，两组完全一致即不显著，其余视为显著。
        se_pooled = math.sqrt(pooled * (1.0 - pooled) * (1.0 / n_t + 1.0 / n_c))
        if se_pooled == 0:
            z_score = 0.0
            p_value = 1.0
        else:
            z_score = (p_t - p_c) / se_pooled
            # 双侧检验 p 值：erf(|z|/sqrt(2)) 即标准正态落在 |z| 内的概率。
            p_value = math.erfc(abs(z_score) / math.sqrt(2.0))
        z_score = round(z_score, 6)
        p_value = round(p_value, 6)

        alpha = _norm(version["显著阈值"])
        lower = _norm(version["基准区间下限"])
        upper = _norm(version["基准区间上限"])
        if not (0 < alpha < 1):
            raise ValueError(f"阈值版本 {version.get('版本编号')} 的显著阈值超出 (0,1) 标准")
        if lower > upper:
            raise ValueError(f"阈值版本 {version.get('版本编号')} 的基准区间下限大于上限")

        # 规则校验：超出标准时逐条记录原因，而不是只给一个布尔结论。
        reasons: list[str] = []
        threshold_pass = p_value <= alpha
        if not threshold_pass:
            reasons.append(f"p值 {p_value:.4f} 超出显著阈值 {alpha:g}（{version['版本编号']}）")
        if not (lower <= p_t <= upper):
            reasons.append(
                f"实验组指标值 {p_t:.4f} 超出基准区间 {version['基准区间']}（{version['版本编号']}）")
        significant = threshold_pass and lower <= p_t <= upper

        return {
            "实验方案id": int(observation["实验方案id"]),
            "方案编号": observation["方案编号"],
            "分组": observation["分组"],
            "阈值版本id": int(version["id"]),
            "版本编号": version["版本编号"],
            "版本名称": version["版本名称"],
            "处理范围": scope,
            "显著阈值": alpha,
            "基准区间": version["基准区间"],
            "基准区间下限": lower,
            "基准区间上限": upper,
            "实验组样本量": int(n_t),
            "实验组转化数": int(x_t),
            "实验组指标值": round(p_t, 6),
            "对照组样本量": int(n_c),
            "对照组转化数": int(x_c),
            "对照组指标值": round(p_c, 6),
            "分组差异": diff,
            "z统计量": z_score,
            "p值": p_value,
            "阈值判定": "显著" if significant else "不显著",
            "是否显著": significant,
            "不显著原因": "；".join(reasons),
            "判定条件": (
                f"双侧两比例z检验 | p值={p_value:.4f} <= 显著阈值 {alpha:g}；"
                f"实验组指标值 {p_t:.4f} 落在基准区间 {version['基准区间']} 内"
            ),
            "业务输入": {
                "实验组样本量": int(n_t),
                "实验组转化数": int(x_t),
                "对照组样本量": int(n_c),
                "对照组转化数": int(x_c),
                "显著阈值": alpha,
                "基准区间": version["基准区间"],
            },
            "重算时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _upsert_judgement(self, record: dict[str, Any]) -> dict[str, Any]:
        """按唯一键写入阈值判定记录；已存在则覆盖，重复提交只留一份有效结果。"""
        rows = store.rows(JUDGEMENT_MODULE)
        for existing in rows:
            if all(existing.get(k) == record.get(k) for k in JUDGEMENT_KEY):
                record_id = existing.get("id")
                existing.clear()
                existing.update(record)
                existing["id"] = record_id
                return dict(existing)
        record = dict(record)
        record["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append(record)
        return dict(record)

    def recalculate(
        self,
        *,
        experiment_id: int | None = None,
        version_id: int | None = None,
        scope: str = SCOPE_RUNTIME,
    ) -> list[dict[str, Any]]:
        """读取既有数据后按阈值版本重算显著性与基准区间，并落阈值判定记录。"""
        if scope not in SCOPES:
            raise ValueError(f"处理范围「{scope}」不合法，仅支持：{'、'.join(SCOPES)}")
        version = self._resolve_version(version_id, scope)
        observations = self._observations(experiment_id)
        if not observations:
            raise ValueError("没有可重算的实验分组观测数据")
        records: list[dict[str, Any]] = []
        for observation in observations:
            judged = self._judge_one(observation, version, scope)
            records.append(self._upsert_judgement(judged))
        return records

    # ------------------------------------------------------------------
    # 阈值判定记录查询：列表与详情都从同一份持久化记录按键读取，
    # 避免列表错位、详情残留旧记录（根因相同：曾经按数组下标关联）。
    # ------------------------------------------------------------------
    def list_judgements(
        self,
        *,
        experiment_id: int | None = None,
        version_id: int | None = None,
        scope: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(JUDGEMENT_MODULE)
        result = [dict(row) for row in rows]
        if experiment_id is not None:
            result = [row for row in result if row.get("实验方案id") == experiment_id]
        if version_id is not None:
            result = [row for row in result if row.get("阈值版本id") == version_id]
        if scope is not None:
            result = [row for row in result if row.get("处理范围") == scope]
        return result

    def get_judgement(self, judgement_id: int) -> dict[str, Any] | None:
        return store.find(JUDGEMENT_MODULE, judgement_id)

    # ------------------------------------------------------------------
    # 实验结果接口编排：实验方案 + 阈值判定记录一起返回给消费方验证
    # ------------------------------------------------------------------
    def experiment_results(
        self,
        *,
        experiment_id: int | None = None,
        version_id: int | None = None,
        scope: str = SCOPE_RUNTIME,
    ) -> dict[str, Any]:
        """结果页数据：按当前阈值版本给出与规则一致的分组差异判定。"""
        version = self._resolve_version(version_id, scope)
        judgements = self.list_judgements(
            experiment_id=experiment_id, version_id=version["id"], scope=scope)
        if not judgements:
            # 首次查看尚无判定记录时，先重算再返回，保证消费方拿到的就是当前口径结论。
            judgements = self.recalculate(
                experiment_id=experiment_id, version_id=version["id"], scope=scope)
        experiments = store.rows(MODULE)
        if experiment_id is not None:
            experiments = [row for row in experiments if int(row.get("id", 0)) == experiment_id]
        return {
            "阈值版本": dict(version),
            "处理范围": scope,
            "实验方案": [dict(row) for row in experiments],
            "items": judgements,
            "显著分组数": sum(1 for row in judgements if row.get("是否显著")),
            "不显著分组数": sum(1 for row in judgements if not row.get("是否显著")),
        }

    # ------------------------------------------------------------------
    # 兼容既有分析看板边界：保留原随机形态的运行分析与汇总
    # ------------------------------------------------------------------
    def list_results(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        results: list[dict] = []
        for row in rows:
            d = dict(row)
            d["指标值"] = round(random.uniform(0.65, 0.98), 3)
            d["对比基准"] = round(random.uniform(0.60, 0.92), 3)
            d["差异"] = round(d["指标值"] - d["对比基准"], 3)
            results.append(d)
        return results

    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        count = int(params.get("样本范围", params.get("并行数量", "6")))
        results: list[dict] = []
        for i in range(count):
            results.append({
                "id": i + 1,
                "分组": f"分组{(i % 3) + 1}",
                "指标值": round(random.uniform(0.62, 1.02), 3),
                "对比基准": round(random.uniform(0.58, 0.95), 3),
                "差异": round(random.uniform(-0.2, 0.3), 3),
            })
        return results

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)
