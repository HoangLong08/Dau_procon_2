"""Bảng chi phí và hằng số luật (Bảng 1 của đề bài).

Chi phí được tính theo ô ĐANG ĐỨNG (ô nguồn) khi phát lệnh rời đi (Q&A Q10, Q25).
"""

PLAIN = 0
ROAD = 1
MOUNTAIN = 2
POND = 3

SMOOTH = 0
CONGESTED = 1
JAMMED = 2

PATROL = 0
REFUEL = 1

# số bước để rời một ô
STEP_PLAIN = 2
STEP_MOUNTAIN = 3
STEP_ROAD = (1, 2, 4)  # theo trạng thái: thông thoáng, đông đúc, ùn tắc

# nhiên liệu xe tuần tra tiêu thụ khi rời một ô
FUEL_PLAIN = 1
FUEL_MOUNTAIN = 2
FUEL_ROAD = 2

IMPASSABLE = -1


def step_cost(terrain, road_status=SMOOTH):
    """Số bước để rời một ô; IMPASSABLE nếu là ao."""
    if terrain == PLAIN:
        return STEP_PLAIN
    if terrain == ROAD:
        return STEP_ROAD[road_status]
    if terrain == MOUNTAIN:
        return STEP_MOUNTAIN
    return IMPASSABLE


def fuel_cost(terrain):
    """Nhiên liệu để rời một ô (xe tuần tra); IMPASSABLE nếu là ao."""
    if terrain == PLAIN:
        return FUEL_PLAIN
    if terrain == ROAD:
        return FUEL_ROAD
    if terrain == MOUNTAIN:
        return FUEL_MOUNTAIN
    return IMPASSABLE


def passable(terrain):
    return terrain != POND


def road_status_for(volume, busy_threshold, jammed_threshold):
    """Trạng thái đường từ lưu lượng đã chia cho số đội."""
    if volume >= jammed_threshold:
        return JAMMED
    if volume >= busy_threshold:
        return CONGESTED
    return SMOOTH


# mã lỗi, khớp với server
E_NOT_ADJACENT = "E_NOT_ADJACENT"
E_POND = "E_POND"
E_STEP_OVERFLOW = "E_STEP_OVERFLOW"
E_NO_FUEL = "E_NO_FUEL"
E_BAD_FORMAT = "E_BAD_FORMAT"
