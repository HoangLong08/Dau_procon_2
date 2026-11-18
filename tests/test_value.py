"""B4 (17/11/2026): hàm giá trị học được V(trạng thái cuối ngày)."""

import dataclasses
import unittest
from unittest import mock

from hexudon.model import AgentState
from hexudon.offline.mapgen import load_or_generate
from hexudon.planner import objective, value
from hexudon.planner.engine import SOFT_END_TERMS, make_context, plan_day, q_phase_weights, safe_solution
from hexudon.planner.objective import end_value
from hexudon.planner.roles import candidates
from hexudon.planner.solution import Weights
from hexudon.planner.value import FEATURE_NAMES, ValueModel, features, hard_terms


def sample_states(name="sample_16", days=(0, 1, 2)):
    cfg = load_or_generate(name)
    kinds = candidates(cfg)[1].kinds
    agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
    visited = set()
    out = []
    for day in range(max(days) + 1):
        ctx = make_context(cfg, day, agents, {}, [s.stocks for s in cfg.spots], set(visited), forecast_status={})
        dec = plan_day(ctx, seed=day, max_iterations=20)
        if day in days:
            out.append((ctx, dec))
        visited |= dec.result.brands
        agents = [AgentState(kinds[i], dec.result.end_pos[i], dec.result.end_fuel[i]) for i in range(cfg.n_agents)]
    return out


def stump_model(feature: int, threshold: float, low: float, high: float, init=0.5, rate=1.0) -> dict:
    """Một cây một lần tách: x[feature] <= threshold -> low, ngược lại high."""
    return {"features": FEATURE_NAMES, "init": init, "learning_rate": rate,
            "trees": [{"f": [feature, -2, -2], "t": [threshold, -2.0, -2.0], "l": [1, -1, -1],
                       "r": [2, -1, -1], "v": [0.0, low, high]}]}


class FeatureTest(unittest.TestCase):
    def test_features_have_the_declared_length_and_are_deterministic(self):
        for ctx, dec in sample_states():
            x = features(ctx, dec.result)
            self.assertEqual(len(x), len(FEATURE_NAMES))
            self.assertEqual(x, features(ctx, dec.result))
            self.assertTrue(all(v == v and abs(v) < 1e9 for v in x), "không NaN, không vô cực")
            self.assertEqual(x[0], ctx.days_left)

    def test_hard_terms_equal_end_value_without_its_soft_terms(self):
        no_soft = {"urgency": 0.0, "end_on_spot": 0.0, "near_chain": 0.0, "fuel_bonus": 0.0, "road_park": 0.0,
                   "value_weight": 0.0}
        for ctx, dec in sample_states():
            w = dataclasses.replace(ctx.weights, **no_soft)
            legacy = end_value(dataclasses.replace(ctx, weights=w), dec.result)
            on_spot = sum(1 for i in ctx.patrols if dec.result.end_pos[i] in ctx.config.spot_index_at)
            # end_value cũ cộng thêm w.serving cho mỗi xe đứng trên spot (không theo end_on_spot)
            self.assertAlmostEqual(hard_terms(ctx, dec.result, w), legacy - w.serving * on_spot)


class ModelTest(unittest.TestCase):
    def test_tree_walk_matches_the_definition(self):
        m = ValueModel(stump_model(feature=0, threshold=2.5, low=-1.0, high=3.0, init=0.5, rate=0.1))
        x = [0.0] * len(FEATURE_NAMES)
        x[0] = 2.0
        self.assertAlmostEqual(m.predict(x), 0.5 + 0.1 * -1.0)
        x[0] = 3.0
        self.assertAlmostEqual(m.predict(x), 0.5 + 0.1 * 3.0)

    def test_a_model_with_other_features_is_refused(self):
        data = stump_model(0, 1.0, 0.0, 0.0)
        data["features"] = FEATURE_NAMES[:-1]
        with self.assertRaises(ValueError):
            ValueModel(data)

    def test_environment_variable_selects_another_model_file(self):
        import json
        import os
        import tempfile
        coef = [0.0] * len(FEATURE_NAMES)
        coef[0] = 1.0
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v2.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"features": FEATURE_NAMES, "kind": "tuyen_tinh", "mode": "phan_du", "coef": coef}, fh)
            with mock.patch.dict(os.environ, {value.ENV_MODEL: path}):
                m = value.default_model()
                self.assertEqual(m.coef[0], 1.0)
                self.assertIs(value.default_model(), m, "nạp một lần mỗi đường dẫn")
        self.assertIsNot(value.default_model(), m, "bỏ biến môi trường thì về mô hình đi kèm")

    def test_shipped_model_loads(self):
        m = value.default_model()
        self.assertIsNotNone(m, "value_model.json phải đi kèm package")
        ctx, dec = sample_states(days=(1,))[0]
        self.assertIsInstance(m.predict(features(ctx, dec.result)), float)


class ObjectiveTest(unittest.TestCase):
    def test_value_weight_zero_never_touches_the_learned_value(self):
        self.assertEqual(Weights().value_weight, 1.0, "B4 bật mặc định từ 17/11/2026")
        ctx, dec = sample_states(days=(1,))[0]
        c = dataclasses.replace(ctx, weights=Weights(value_weight=0.0))
        with mock.patch.object(objective, "learned_parts") as spy,                 mock.patch.object(objective, "features") as feats:
            end_value(c, dec.result)
        spy.assert_not_called()
        feats.assert_not_called()

    def test_value_weight_replaces_the_soft_terms(self):
        ctx, dec = sample_states(days=(1,))[0]
        m = ValueModel(stump_model(feature=0, threshold=100.0, low=2.0, high=0.0, init=0.0))
        w = Weights(value_weight=0.5)
        c = dataclasses.replace(ctx, weights=w)
        with mock.patch.object(objective, "default_model", return_value=m):
            got = end_value(c, dec.result)
        # phần mềm theo đơn vị Weights.soft (B khi soft_unit = 0, soft_unit từ 18/11/2026)
        self.assertAlmostEqual(got, hard_terms(ctx, dec.result, w) + 0.5 * w.soft * 2.0)
        w0 = Weights(value_weight=0.5, soft_unit=0.0)
        with mock.patch.object(objective, "default_model", return_value=m):
            got0 = end_value(dataclasses.replace(ctx, weights=w0), dec.result)
        self.assertAlmostEqual(got0, hard_terms(ctx, dec.result, w0) + 0.5 * w0.B * 2.0)

    def test_linear_residual_model_adds_to_the_hand_tuned_value(self):
        ctx, dec = sample_states(days=(1,))[0]
        coef = [0.0] * len(FEATURE_NAMES)
        coef[0] = 0.25          # ngay_con_lai
        m = ValueModel({"features": FEATURE_NAMES, "kind": "tuyen_tinh", "mode": "phan_du", "coef": coef})
        self.assertTrue(m.residual)
        x = features(ctx, dec.result)
        self.assertAlmostEqual(m.predict(x), 0.25 * x[0])
        c = dataclasses.replace(ctx, weights=Weights(value_weight=0.5))
        hand = end_value(dataclasses.replace(ctx, weights=Weights(value_weight=0.0)), dec.result)
        with mock.patch.object(objective, "default_model", return_value=m):
            got = end_value(c, dec.result)
        self.assertAlmostEqual(got, hand + 0.5 * Weights().soft * 0.25 * x[0])

    def test_missing_model_falls_back_to_the_old_end_value(self):
        ctx, dec = sample_states(days=(1,))[0]
        c = dataclasses.replace(ctx, weights=Weights(value_weight=1.0))
        hand = end_value(dataclasses.replace(ctx, weights=Weights(value_weight=0.0)), dec.result)
        with mock.patch.object(objective, "default_model", return_value=None):
            self.assertEqual(end_value(c, dec.result), hand)

    def test_last_day_is_still_zero(self):
        cfg = load_or_generate("sample_16")
        kinds = candidates(cfg)[1].kinds
        agents = [AgentState(kinds[i], cfg.agent_starts[i], cfg.fuel_limits) for i in range(cfg.n_agents)]
        ctx = make_context(cfg, cfg.n_days - 1, agents, {}, [s.stocks for s in cfg.spots], set(),
                           weights=Weights(value_weight=1.0))
        _, dec = safe_solution(ctx, 0)
        self.assertEqual(end_value(ctx, dec.result), 0.0)

    def test_q_phase_scales_the_learned_value_like_other_soft_terms(self):
        self.assertIn("value_weight", SOFT_END_TERMS)
        q = q_phase_weights(Weights(value_weight=1.0, q_end_scale=0.1))
        self.assertAlmostEqual(q.value_weight, 0.1)

    def test_plan_day_with_the_learned_value_stays_valid(self):
        ctx, _ = sample_states(days=(1,))[0]
        c = dataclasses.replace(ctx, weights=Weights(value_weight=1.0))
        dec = plan_day(c, seed=0, max_iterations=40)
        self.assertTrue(dec.result.ok)


if __name__ == "__main__":
    unittest.main()


def mlp_model(seed=0, hidden=(5, 3)) -> dict:
    import random
    rng = random.Random(seed)
    n = len(FEATURE_NAMES)
    sizes = [n, *hidden, 1]
    layers = [{"W": [[rng.uniform(-1, 1) for _ in range(b)] for _ in range(a)],
               "b": [rng.uniform(-0.5, 0.5) for _ in range(b)]} for a, b in zip(sizes, sizes[1:])]
    return {"features": FEATURE_NAMES, "kind": "mlp", "mode": "phan_du", "don_vi": "phan",
            "mean": [rng.uniform(-1, 1) for _ in range(n)], "scale": [rng.uniform(0.5, 2) for _ in range(n)],
            "layers": layers, "y_mean": 3.0, "y_scale": 2.0}


class MlpModelTest(unittest.TestCase):
    """V-VN (18/11/2026): mạng nơ-ron chạy thuần Python phải ra đúng như tính bằng numpy."""

    def test_forward_matches_numpy(self):
        import numpy as np
        data = mlp_model(seed=3)
        m = ValueModel(data)
        self.assertEqual(m.unit, "phan")
        self.assertTrue(m.residual)
        rng = np.random.default_rng(1)
        for _ in range(20):
            x = rng.normal(size=len(FEATURE_NAMES))
            h = (x - np.array(data["mean"])) / np.array(data["scale"])
            for k, layer in enumerate(data["layers"]):
                h = h @ np.array(layer["W"]) + np.array(layer["b"])
                if k < len(data["layers"]) - 1:
                    h = np.maximum(h, 0.0)
            self.assertAlmostEqual(m.predict(list(x)), 3.0 + 2.0 * float(h[0]), places=9)

    def test_rejects_wrong_input_size(self):
        data = mlp_model()
        data["mean"] = data["mean"][:-1]
        with self.assertRaises(ValueError):
            ValueModel(data)

    def test_vn_model_used_only_on_safe_days_in_serving_units(self):
        ctx, dec = sample_states(days=(1,))[0]
        m = ValueModel(mlp_model(seed=5))
        x = features(ctx, dec.result)
        base_w = Weights(value_weight=0.0)
        base = end_value(dataclasses.replace(ctx, weights=base_w), dec.result)
        with mock.patch.object(objective, "default_model_vn", return_value=m):
            on = end_value(dataclasses.replace(ctx, weights=Weights(value_weight=0.0, value_q_weight=0.5)),
                           dec.result)
            off_day = end_value(dataclasses.replace(ctx, weights=Weights(value_weight=0.0, value_q_weight=0.5,
                                                                           soft_unit=0.0)), dec.result)
        self.assertAlmostEqual(on, base + 0.5 * Weights().serving * m.predict(x), places=6)
        base0 = end_value(dataclasses.replace(ctx, weights=Weights(value_weight=0.0, soft_unit=0.0)), dec.result)
        self.assertAlmostEqual(off_day, base0, places=6, msg="ngày chưa an toàn (soft_unit = 0): không dùng V-VN")
