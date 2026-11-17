"""分析实验阈值版本重算的单元测试。

运行：cd backend && .venv/bin/python -m unittest app.tests.test_experiment -v
覆盖：
- 读取既有数据后按阈值版本重算显著性与基准区间；
- 设计冻结值 / 运行期生效值两种处理范围产生不同显著结论；
- 阈值与基准区间一起参与判定，缺一会引用旧区间；
- 规则校验超出标准时记录原因；
- 重复提交只写入一份有效结果（唯一键 upsert）；
- 阈值判定记录保留业务输入与判定条件。
"""
from __future__ import annotations

import unittest

from app.services.experiment import (
    JUDGEMENT_MODULE,
    SCOPE_FROZEN,
    SCOPE_RUNTIME,
    ExperimentService,
)
from app.store import Store


class ExperimentThresholdTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例使用独立的内存仓库，避免用例间相互污染。
        import app.services.experiment as experiment_module
        self._original_store = experiment_module.store
        self.fresh_store = Store()
        experiment_module.store = self.fresh_store
        self.service = ExperimentService()

    def tearDown(self) -> None:
        import app.services.experiment as experiment_module
        experiment_module.store = self._original_store

    def test_runtime_recalculate_matches_current_threshold(self) -> None:
        records = self.service.recalculate(
            experiment_id=1, version_id=2, scope=SCOPE_RUNTIME)
        self.assertEqual(len(records), 3)
        group3 = next(r for r in records if r["分组"] == "分组3")
        # p=0.0444 > 0.01，运行期收紧阈值下不显著，并记录超出原因
        self.assertFalse(group3["是否显著"])
        self.assertEqual(group3["阈值判定"], "不显著")
        self.assertIn("超出显著阈值", group3["不显著原因"])
        self.assertEqual(group3["显著阈值"], 0.01)
        self.assertEqual(group3["基准区间"], "[0.62, 0.78]")

    def test_frozen_and_runtime_scopes_give_different_conclusions(self) -> None:
        frozen = self.service.recalculate(
            experiment_id=1, version_id=1, scope=SCOPE_FROZEN)
        runtime = self.service.recalculate(
            experiment_id=1, version_id=2, scope=SCOPE_RUNTIME)
        f3 = next(r for r in frozen if r["分组"] == "分组3")
        r3 = next(r for r in runtime if r["分组"] == "分组3")
        # 同一分组在两种处理范围下结论翻转
        self.assertTrue(f3["是否显著"])
        self.assertFalse(r3["是否显著"])
        # 两份记录分别引用各自版本的基准区间，没有复用旧区间
        self.assertEqual(f3["基准区间"], "[0.60, 0.75]")
        self.assertEqual(r3["基准区间"], "[0.62, 0.78]")

    def test_baseline_interval_participates_in_judgement(self) -> None:
        # 分组1 的 p 值远小于阈值，但指标值 0.82 超出两个版本的基准区间，
        # 必须判不显著，证明基准区间确实参与了结论计算。
        records = self.service.recalculate(
            experiment_id=1, version_id=2, scope=SCOPE_RUNTIME)
        group1 = next(r for r in records if r["分组"] == "分组1")
        self.assertLessEqual(group1["p值"], group1["显著阈值"])
        self.assertFalse(group1["是否显著"])
        self.assertIn("超出基准区间", group1["不显著原因"])

    def test_repeated_submit_upserts_single_record(self) -> None:
        first = self.service.recalculate(
            experiment_id=1, version_id=2, scope=SCOPE_RUNTIME)
        second = self.service.recalculate(
            experiment_id=1, version_id=2, scope=SCOPE_RUNTIME)
        rows = self.fresh_store.rows(JUDGEMENT_MODULE)
        self.assertEqual(len(rows), len(first))
        self.assertEqual([r["id"] for r in first], [r["id"] for r in second])
        keys = [(r["实验方案id"], r["分组"], r["阈值版本id"], r["处理范围"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_judgement_keeps_business_input_and_condition(self) -> None:
        records = self.service.recalculate(
            experiment_id=2, version_id=1, scope=SCOPE_FROZEN)
        record = records[0]
        self.assertIn("实验组样本量", record["业务输入"])
        self.assertIn("显著阈值", record["业务输入"])
        self.assertIn("基准区间", record["业务输入"])
        self.assertIn("双侧两比例z检验", record["判定条件"])
        # 详情接口按 id 读同一条记录，不残留旧数据
        detail = self.service.get_judgement(record["id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["判定条件"], record["判定条件"])

    def test_invalid_scope_and_missing_version_raise(self) -> None:
        with self.assertRaises(ValueError):
            self.service.recalculate(scope="不存在的范围")
        with self.assertRaises(ValueError):
            self.service.recalculate(version_id=999, scope=SCOPE_RUNTIME)
        with self.assertRaises(ValueError):
            self.service.recalculate(experiment_id=999, scope=SCOPE_RUNTIME)

    def test_results_orchestration_auto_generates_judgements(self) -> None:
        # 首次查看结果时尚无判定记录，结果接口自动按当前口径重算
        self.assertEqual(len(self.fresh_store.rows(JUDGEMENT_MODULE)), 0)
        data = self.service.experiment_results(
            experiment_id=3, version_id=2, scope=SCOPE_RUNTIME)
        self.assertEqual(data["阈值版本"]["版本编号"], "THV-V2")
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["显著分组数"] + data["不显著分组数"], 1)
        self.assertEqual(len(self.fresh_store.rows(JUDGEMENT_MODULE)), 1)


if __name__ == "__main__":
    unittest.main()
