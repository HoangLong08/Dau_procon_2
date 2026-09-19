# HEXUDON — bot thi đấu Procon Việt Nam 2026

Bot chơi **HEXUDON** (Tối ưu hoá lộ trình HEXUDON), phần thi Tranh tài của Procon
Việt Nam 2026 / NAPROCK PROCON lần 18. Python thuần, **không cần thư viện ngoài**.

## Chạy nhanh

```bash
# thi đấu thật qua HTTP (ban tổ chức cấp URL, mã trận và token)
python -m hexudon play --url http://<host>:<port> --match <MATCH_ID> --token <TOKEN> \
                       --search-seconds 3.0 --role-seconds 20 --replay replays/match.jsonl

# thi đấu qua stdin/stdout (JSON Lines), khi ban tổ chức chấm bằng sandbox
python -m hexudon sandbox --search-seconds 3.0

# server luyện tập tại chỗ, nói đúng giao thức thi đấu
python -m hexudon serve --map data/sample_maps/sample_16.json --teams 1 --bots greedy,sample
# rồi ở cửa sổ khác: python -m hexudon play --url http://127.0.0.1:8099 --match m-local --token tok-0

# chạy một trận offline để so chiến thuật
python -m hexudon sim --map data/sample_maps/sample_24.json --bots search,greedy,sample

# benchmark nhiều map và seed
python -m hexudon bench --maps sample_16,sample_24,random20 --count 5 --out bench.json

# xem riêng bước chọn vai trò
python -m hexudon roles --map data/sample_maps/sample_32.json
```

Yêu cầu: Python 3.11 trở lên. Không cài gì thêm.

## Hạn 5 giây mỗi ngày

Bot gửi **bài an toàn** trong khoảng 10 ms sau khi nhận trạng thái, rồi tiếp tục
tìm kiếm tới hạn `--search-seconds` và gửi lại bài tốt hơn. Bài hợp lệ cuối cùng
trước hạn được server áp dụng, nên gửi hai lần là an toàn. Lề `--net-margin`
(mặc định 1.5 s) được chừa cho mạng.

Ở chế độ sandbox mỗi khung chỉ được trả lời một dòng, nên bot chỉ gửi một lần,
sau khi tìm kiếm xong.

## Chiến thuật

Điểm số xét theo thứ tự từ điển: số **loại** udon cả trận, tổng **loại theo ngày**,
tổng **số phần**, rồi thời gian phản hồi. Với tham số map chính thức (32–48 bước,
3–7 xe), tiêu chí một bão hoà nhanh còn tiêu chí hai là chiến trường chính.

1. **Hàm mục tiêu** thưởng chuỗi mới trong ngày mạnh hơn số phần, cộng giá trị
   cuối ngày: chuỗi chưa ghé mà sắp không kịp bị phạt nặng, kết thúc trên spot và
   gần chuỗi chưa ghé được thưởng, xe sắp kẹt xăng bị phạt theo số ngày còn lại.
2. **Chọn vai trò bằng mô phỏng** trước ngày 1: thử 0, 1, 2 xe tiếp nhiên liệu ở
   các vị trí trung tâm khác nhau, chơi thử cả trận rồi chọn phương án điểm cao nhất.
3. **Tiếp nhiên liệu bằng điểm hẹn** đặt trên chính lộ trình xe tuần tra; xe tiếp
   đến trước và đợi. Không kịp thì bỏ điểm hẹn, bài vẫn hợp lệ.
4. **Dự báo giao thông**: ước lượng ô đường nào sẽ đông đúc hoặc ùn tắc ngày mai
   dựa trên độ phổ biến của hành lang, rồi dùng dự báo đó cho giá trị cuối ngày.
   Ngày 1 đường còn thông thoáng nên bot khai thác đường mạnh.
5. **Tìm kiếm ALNS** trên nghiệm "lộ trình spot + điểm hẹn", có bộ giải mã toàn
   phần nên **mọi nghiệm đều ra bài hợp lệ**.

## Cấu trúc

| Thư mục | Vai trò |
|---|---|
| `hexudon/geometry.py`, `rules.py`, `model.py` | hình học even-r, bảng chi phí, dữ liệu |
| `hexudon/simulator.py` | **nguồn chân lý về luật**: 5 pha mỗi bước, kiểm hợp lệ |
| `hexudon/pathing.py`, `traffic.py` | Dijkstra trọng số đỉnh, dự báo giao thông |
| `hexudon/planner/` | nghiệm, bộ giải mã, hàm mục tiêu, dựng nghiệm, ALNS, vai trò, bộ nhớ trận |
| `hexudon/client/` | HTTP, sandbox, vòng đời trận |
| `hexudon/offline/` | bot đối thủ, runner nhiều đội, sinh map, server luyện tập, benchmark |
| `tests/` | kiểm thử, gồm golden test tái hiện ví dụ từng bước của ban tổ chức |

## Ngày thi

Trước trận, chạy một lần để chắc chắn máy và mạng đều ổn:

```bash
python -m unittest discover -s tests            # phải xanh toàn bộ
python -m hexudon serve --size 16 --teams 1 &   # server luyện tập tại chỗ
python -m hexudon play --url http://127.0.0.1:8099 --match m-local --token tok-0                        --search-seconds 3.0 --role-seconds 20
```

Khi vào trận thật, chỉ cần đổi `--url`, `--match`, `--token`. Luôn bật
`--replay replays/<mã trận>.jsonl` để có nhật ký đối chiếu nếu có tranh chấp.

Bot tự xử lý các tình huống sau mà không cần can thiệp:

| Tình huống | Bot làm gì |
|---|---|
| Server chưa mở (HTTP 425) | tiếp tục hỏi, 250 ms một lần |
| Poll quá nhanh (HTTP 429) | chờ rồi thử lại |
| Mất mạng, timeout, lỗi 5xx | thử lại với giãn cách tăng dần, giữ nguyên bài đã dựng |
| Bài bị từ chối | gửi bài an toàn; nếu vẫn bị từ chối thì gửi bài đứng yên (luôn hợp lệ) |
| Server đòi kiểu bọc JSON khác | tự thử `[...]`, `{"actions": ...}`, `{"types": ...}` rồi nhớ kiểu đúng |
| Server đánh số ngày từ 1 | nhận ra qua lỗi số bước, tự lùi chỉ số ngày cho cả trận |
| Planner lỗi bất ngờ | bài an toàn đã gửi trước đó vẫn có hiệu lực |

Chỉnh ngân sách theo hạn thật của ban tổ chức: `--search-seconds` nên bằng hạn
mỗi ngày trừ khoảng 2 giây. Với hạn 5 giây thì để mặc định 3.0.

## Kiểm thử

```bash
python -m unittest discover -s tests
```

Bài kiểm quan trọng nhất là `tests/test_simulator_golden.py`: tái hiện đúng ví dụ
8 tác nhân trong tài liệu bổ sung của ban tổ chức Nhật, kiểm tới từng ô, từng đơn
vị nhiên liệu, từng phần udon và cả bốn con số lưu lượng cuối ngày.

`tests/test_decoder.py` kiểm tính chất quan trọng nhất của planner: **mọi nghiệm,
kể cả nghiệm vô lý sinh ngẫu nhiên, đều ra kế hoạch hợp lệ** — vì một lệnh sai làm
hỏng cả ngày của đội.
