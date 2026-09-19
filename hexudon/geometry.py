"""Hình học lưới lục giác even-r (hàng chẵn lệch phải).

Quy ước lấy từ Q&A chính thức của ban tổ chức (Q1, Q47):
  - ô đánh số theo hàng: pos = row * width + col
  - hàng CHẴN (0, 2, 4, ...) lệch sang phải nửa ô
  - hướng 0..5 theo chiều kim đồng hồ từ trên-trái:
      0 trên-trái, 1 trên-phải, 2 phải, 3 dưới-phải, 4 dưới-trái, 5 trái
"""

# offset (dcol, drow) theo hướng, cho hàng chẵn và hàng lẻ
DIRS_EVEN = ((0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 0))
DIRS_ODD = ((-1, -1), (0, -1), (1, 0), (0, 1), (-1, 1), (-1, 0))

NUM_DIRS = 6


def opposite(d):
    """Hướng ngược lại."""
    return (d + 3) % 6


def row_col(pos, width):
    return divmod(pos, width)


def to_pos(row, col, width):
    return row * width + col


def neighbor(pos, d, width, height):
    """Ô kề theo hướng d; -1 nếu ra ngoài bản đồ (map không phải torus)."""
    row, col = divmod(pos, width)
    dcol, drow = (DIRS_ODD if row & 1 else DIRS_EVEN)[d]
    ncol = col + dcol
    nrow = row + drow
    if ncol < 0 or ncol >= width or nrow < 0 or nrow >= height:
        return -1
    return nrow * width + ncol


def build_neighbor_table(width, height):
    """Bảng kề: table[pos][d] -> ô kề hoặc -1. Tính một lần cho cả trận."""
    n = width * height
    table = []
    for pos in range(n):
        row = pos // width
        col = pos - row * width
        offsets = DIRS_ODD if row & 1 else DIRS_EVEN
        row_entry = []
        for dcol, drow in offsets:
            ncol = col + dcol
            nrow = row + drow
            if ncol < 0 or ncol >= width or nrow < 0 or nrow >= height:
                row_entry.append(-1)
            else:
                row_entry.append(nrow * width + ncol)
        table.append(row_entry)
    return table


def build_direction_index(neighbor_table):
    """dir_index[u][v] -> hướng đi từ u sang v (dict cho mỗi ô)."""
    index = []
    for u, row_entry in enumerate(neighbor_table):
        d = {}
        for direction, v in enumerate(row_entry):
            if v >= 0:
                d[v] = direction
        index.append(d)
    return index


def hex_distance(a, b, width):
    """Khoảng cách lục giác lý thuyết (bỏ qua địa hình), dùng để cắt nhánh."""
    ar, ac = divmod(a, width)
    br, bc = divmod(b, width)
    # even-r offset -> toạ độ cube
    ax = ac - (ar + (ar & 1)) // 2
    az = ar
    ay = -ax - az
    bx = bc - (br + (br & 1)) // 2
    bz = br
    by = -bx - bz
    return max(abs(ax - bx), abs(ay - by), abs(az - bz))
