"""Hình học even-r: hàng chẵn lệch phải, hướng 0..5 theo chiều kim đồng hồ."""

import unittest

from hexudon.geometry import (build_direction_index, build_neighbor_table,
                              hex_distance, neighbor, opposite)


class GeometryTest(unittest.TestCase):
    W, H = 6, 6

    def test_even_row_offsets(self):
        # ô 14 ở hàng 2 (chẵn), cột 2
        pos = 2 * self.W + 2
        got = [neighbor(pos, d, self.W, self.H) for d in range(6)]
        expect = [
            1 * self.W + 2,  # trên-trái
            1 * self.W + 3,  # trên-phải
            2 * self.W + 3,  # phải
            3 * self.W + 3,  # dưới-phải
            3 * self.W + 2,  # dưới-trái
            2 * self.W + 1,  # trái
        ]
        self.assertEqual(got, expect)

    def test_odd_row_offsets(self):
        # ô 20 ở hàng 3 (lẻ), cột 2
        pos = 3 * self.W + 2
        got = [neighbor(pos, d, self.W, self.H) for d in range(6)]
        expect = [
            2 * self.W + 1,
            2 * self.W + 2,
            3 * self.W + 3,
            4 * self.W + 2,
            4 * self.W + 1,
            3 * self.W + 1,
        ]
        self.assertEqual(got, expect)

    def test_map_is_not_a_torus(self):
        self.assertEqual(neighbor(0, 5, self.W, self.H), -1)      # trái từ cột 0
        self.assertEqual(neighbor(0, 0, self.W, self.H), -1)      # lên từ hàng 0
        last = self.W * self.H - 1
        self.assertEqual(neighbor(last, 2, self.W, self.H), -1)   # phải từ cột cuối
        self.assertEqual(neighbor(last, 3, self.W, self.H), -1)   # xuống từ hàng cuối

    def test_neighbour_relation_is_symmetric(self):
        table = build_neighbor_table(self.W, self.H)
        for u in range(self.W * self.H):
            for d, v in enumerate(table[u]):
                if v >= 0:
                    self.assertEqual(table[v][opposite(d)], u,
                                     f"ô {u} hướng {d} -> {v} nhưng chiều ngược lại sai")

    def test_direction_index_matches_table(self):
        table = build_neighbor_table(self.W, self.H)
        index = build_direction_index(table)
        for u in range(self.W * self.H):
            for d, v in enumerate(table[u]):
                if v >= 0:
                    self.assertEqual(index[u][v], d)

    def test_hex_distance_is_a_metric(self):
        self.assertEqual(hex_distance(0, 0, self.W), 0)
        table = build_neighbor_table(self.W, self.H)
        for u in range(self.W * self.H):
            for v in table[u]:
                if v >= 0:
                    self.assertEqual(hex_distance(u, v, self.W), 1)


if __name__ == "__main__":
    unittest.main()
