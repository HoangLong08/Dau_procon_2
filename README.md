# HEXUDON — bot thi đấu Procon Việt Nam 2026

Bot chơi **HEXUDON** (Tối ưu hoá lộ trình HEXUDON), phần thi Tranh tài của Procon
Việt Nam 2026 / NAPROCK PROCON lần 18.

Python thuần, **không cài gì thêm** — chỉ thư viện chuẩn. Yêu cầu Python 3.10 trở lên.

```bash
git clone <repo> && cd procon-2026
python -m unittest discover -s tests     # 172 test, phải xanh toàn bộ
```

---

## 1. Cách chạy

### Thi đấu trên server của BTC (hexudon.hairbui76.id.vn)

Server thi của Procon Việt Nam 2026 là `https://hexudon.hairbui76.id.vn`. Token lấy trên
website (icon 🔑), dán vào file `.env` ở gốc repo (file này không bao giờ được commit):

```
TOKEN=<token thi thật>
TEST_DAU_01=<token test>
TEST_DAU_02=<token test>
```

`TOKEN` chỉ dùng được khi có `--live`; không chỉ định gì thì bot dùng `TEST_DAU_02`.
Token sống 48 giờ — lấy token mới trước mỗi buổi thi.

```bash
python -m hexudon games --live                            # trận, câu, ID, trạng thái
python -m hexudon web --live --match <ID|tên> --follow    # thi thật: bắt mọi câu của trận
python -m hexudon web --live --game <ID câu>              # một câu, khi manager API lỗi
```

Luyện tập (không cần `--live`; hướng dẫn test đầy đủ ở [mục 6](#6-test-bot)):

```bash
python -m hexudon web --match "Practice Match" --question Q01     # trọn một câu practice
python -m hexudon web --match "Mock Contest" --follow \
       --token-env TEST_DAU_02                                    # thi thử có giờ, một token
python -m hexudon web-bench --match "Practice Match" --jobs 3     # loạt practice, so điểm
python -m hexudon serve --web                                     # server giả, không cần mạng
```

Mỗi lần chơi ghi vào `replays/`: replay JSON Lines, file `-summary.json` (điểm, hạng, kết
quả đối chiếu simulator) và `-replay.json` (replay của server). Practice được reset sau
khi đã lưu kết quả, để đội khác không đọc được lộ trình qua `/practice/peer`; thêm
`--keep` nếu muốn xem lại trên website.

### Thi đấu trên server kiểu PTIT (HTTP)

Ban tổ chức cấp URL, mã trận và token:

```bash
python -m hexudon play \
    --url http://<host>:<port> \
    --match <MATCH_ID> \
    --token <TOKEN> \
    --search-seconds 3.0 \
    --replay replays/<mã trận>.jsonl
```

Chỉ ba tham số đầu là bắt buộc. **Luôn bật `--replay`**: mọi request, response và
bài đã gửi được ghi lại dạng JSON Lines, là bằng chứng duy nhất nếu có tranh chấp
kết quả.

Bot tự làm trọn vòng đời: `GET /setup` → chọn vai trò → `POST /assignment` → lặp
(`GET /state` → tính → `POST /actions`) → `GET /result`. Không cần can thiệp giữa trận.

### Thi đấu qua sandbox (stdin/stdout)

Khi ban tổ chức chấm bằng cách bơm JSON Lines vào stdin:

```bash
python -m hexudon sandbox --search-seconds 3.0
```

Mỗi khung chỉ được trả lời **đúng một dòng**, nên ở chế độ này bot gửi một lần duy
nhất sau khi tìm kiếm xong, thay vì gửi hai lần như chế độ HTTP.

### Luyện tập tại chỗ

Server luyện tập nói đúng giao thức thi đấu, kèm đối thủ nội bộ:

```bash
# cửa sổ 1
python -m hexudon serve --map data/sample_maps/sample_16.json --teams 1 --bots greedy,sample

# cửa sổ 2 (server in sẵn dòng lệnh này khi khởi động)
python -m hexudon play --url http://127.0.0.1:8099 --match m-local --token tok-0 \
                       --search-seconds 3.0
```

Server chờ **đúng hạn mỗi ngày** rồi mới đóng ngày, giống luật thật, nên chế độ
gửi-lại-bài-tốt-hơn được kiểm chứng thật sự chứ không phải chỉ trên giấy.

### Đo chiến thuật offline

```bash
# một trận, so ba chiến thuật
python -m hexudon sim --map data/sample_maps/sample_24.json --bots search,greedy,sample

# nhiều map × nhiều seed, ghi báo cáo JSON
python -m hexudon bench --maps sample_16,sample_24,sample_32 --count 3 --out bench.json

# chỉ xem bước chọn vai trò quyết định thế nào
python -m hexudon roles --map data/sample_maps/sample_32.json
```

---

## 2. Cách dùng

### Các lệnh

| Lệnh | Dùng khi nào |
|---|---|
| `games` | liệt kê trận/câu trên server thi, kèm ID và trạng thái |
| `web` | chơi trên server thi: một câu, nhiều đội (`--token-env` lặp lại), hoặc cả trận (`--follow`) |
| `web-bench` | chạy loạt câu practice trên server thi, so điểm với đội khác |
| `serve --web` | server giả nói giao thức của server thi, để tập dượt |
| `play` | thi đấu thật qua HTTP |
| `sandbox` | thi đấu khi ban tổ chức chấm qua stdin/stdout |
| `serve` | dựng server luyện tập tại chỗ |
| `sim` | chạy một trận offline, so các chiến thuật |
| `bench` | chạy nhiều map × nhiều seed, ra bảng thắng/thua |
| `roles` | xem riêng bước chọn vai trò xe |

### Tham số thời gian (quan trọng nhất trong ngày thi)

Lệnh `web` và `web-bench` (server thi hexudon.hairbui76.id.vn) — **để nguyên mặc định**:

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--search-seconds` | `auto` | ngân sách tìm kiếm mỗi ngày: hạn của ngày (contest) hoặc độ dài ngày BTC cấu hình (practice), trừ lề mạng |
| `--role-seconds` | `auto` | ngân sách chọn vai trò: theo cửa sổ chọn xe server báo, đã chừa giờ để gửi vai trò tới nơi |
| `--net-margin` | `2.0` | lề chừa cho mạng, trừ khỏi hạn của server |
| `--timeout` | `5.0` | hạn cứng **mỗi cuộc gọi HTTP**; riêng `/result` và `/replay` sau trận được chờ 30 s mỗi lần, thử tối đa 3 lần |
| `--poll-seconds` | `0.5` | nhịp hỏi `/state` |
| `--give-up-seconds` | `120` | mất liên lạc, hoặc token bị từ chối, quá lâu thì thoát thay vì quay vô hạn |

`auto` đọc độ dài ngày và cửa sổ chọn xe từ chính server, nên BTC đổi thông số thì bot
đổi theo. Bình thường **đừng đặt tay** `--search-seconds` hay `--role-seconds` cho `web`.

Lệnh `play` và `sandbox` (server kiểu PTIT):

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--search-seconds` | `3.0` | ngân sách tìm kiếm mỗi ngày |
| `--net-margin` | `1.5` | lề chừa cho mạng, trừ khỏi hạn của server |
| `--role-seconds` | `3.0` | ngân sách chọn vai trò trước ngày 0 |
| `--timeout` | `2.0` | hạn cứng **mỗi cuộc gọi HTTP** |
| `--poll-seconds` | `0.25` | nhịp hỏi `/state` (dưới 0,2 s có thể bị HTTP 429) |
| `--give-up-seconds` | `120` | mất liên lạc quá lâu thì thoát thay vì quay vô hạn |
| `--seed` | `0` | cùng seed + cùng đầu vào ⇒ cùng kế hoạch, để tái hiện khi gỡ lỗi |

**Quy tắc đặt `--search-seconds` cho `play`**: bằng hạn mỗi ngày của ban tổ chức trừ
khoảng 2 giây. Hạn 5 s → để mặc định 3.0. Hạn 60 s → đặt 58.

**Đừng tự tăng `--role-seconds`.** Nó tự rút lại theo `startsAt` của server, hoặc
theo độ dài ngày 0 nếu server không báo giờ mở. Đây là bảo hiểm cho một lỗi đã đo
được: `--role-seconds 20` từng làm bot **mất trắng ngày 0, 1, 2 và thua cả trận**,
vì việc chọn vai trò chạy trong khi các ngày đó mở rồi đóng.

### Chọn map cho `sim` / `bench`

- `sample_16`, `sample_24`, `sample_32` — ba map mẫu chính thức trong `data/sample_maps/`
- `random16`, `random20`, `random24`… — map sinh ngẫu nhiên theo cỡ, theo `--seed`
- hoặc đường dẫn tới file JSON: nhận cả format `/setup` của server lẫn format
  `{"problem": {...}}` của server giả lập Nhật

Đối thủ nội bộ (`--bots`, `--opponents`): `search` (chính bot này), `greedy` (chỉ
bộ dựng nghiệm, không tìm kiếm), `sample` (bot mẫu), `wait` (đứng yên).

---

## 3. Chiến thuật

### Điểm số quyết định điều gì

Xét theo **thứ tự từ điển** — hơn ở tiêu chí trước là thắng, bất kể tiêu chí sau:

1. **L** — số *loại* udon (brand) khác nhau thu được cả trận
2. **D** — tổng theo ngày của số loại khác nhau thu trong ngày
3. **Q** — tổng số phần udon
4. thời gian phản hồi thấp hơn

Với tham số chính thức (map 8–32, 3–8 xe, 4–10 ngày, mỗi ngày W+H đến 4(W+H) bước),
**L bão hoà rất nhanh**: trong cả 9 trận benchmark gần nhất, L của ta *bằng* L của
đối thủ mạnh nhất ở mọi trận. Nên **D mới là chiến trường thật sự**, và bot được
chỉnh để tối đa D chứ không phải Q.

### Năm trụ cột

**1. Hàm mục tiêu hướng theo D, không theo Q.** Thu một chuỗi *mới trong ngày* được
thưởng mạnh hơn nhiều so với thêm một phần udon. Cộng thêm giá trị cuối ngày: chuỗi
chưa ghé mà sắp không còn kịp thì bị phạt nặng; kết thúc ngày trên spot hoặc gần
chuỗi chưa ghé thì được thưởng; xe sắp kẹt xăng bị phạt theo số ngày còn lại.

**2. Chọn vai trò bằng mô phỏng, trước ngày 0.** Thử các phương án 0, 1, 2, 3 xe
tiếp nhiên liệu ở những vị trí trung tâm khác nhau, **chơi thử trọn trận** từng
phương án rồi chọn phương án điểm cao nhất. Phương án nào chưa chơi đủ số ngày thì
bị loại, thay vì đem so với phương án chơi đủ — so lẫn lộn làm việc *tăng* ngân
sách đôi khi lại cho kết quả *tệ hơn*.

**3. Tiếp nhiên liệu bằng điểm hẹn.** Điểm hẹn đặt ngay trên lộ trình xe tuần tra;
xe tiếp nhiên liệu tới trước và đợi. Theo luật, trong cùng một bước nhiên liệu bị
**trừ trước** rồi mới **nạp sau**, nên xe đi kèm xe tiếp luôn đầy bình cuối mỗi
bước, còn xe bám sau một ô thì không bao giờ được tiếp. Nếu không kịp hẹn thì bỏ
điểm hẹn — bài vẫn hợp lệ.

**4. Dự báo giao thông.** Lưu lượng ô đường tính từ stay-step của **mọi đội trong
hai ngày trước** chia cho số đội. Bot ước lượng ô nào sẽ đông đúc hoặc ùn tắc ngày
mai dựa trên độ phổ biến của hành lang cộng chính vết chân của mình, rồi dùng dự
báo đó cho giá trị cuối ngày. Ngày 0 mọi đường đều thông thoáng nên bot khai thác
đường mạnh; các ngày sau nó tránh dần những hành lang đang xấu đi.

**5. Tìm kiếm ALNS trên nghiệm "lộ trình spot + điểm hẹn".** Bộ giải mã là **toàn
phần**: mọi nghiệm, kể cả nghiệm vô lý, đều ra kế hoạch mà simulator chấp nhận —
đã fuzz khoảng 1.900 ca đối kháng, 0 bài sai. Nghiệm nào không sửa nổi thành hợp lệ
thì bị chấm điểm tệ nhất chứ không bao giờ được chọn.

### Cách chia thời gian trong một ngày

Bot gửi **bài an toàn** trong khoảng 10 ms sau khi nhận trạng thái, rồi tiếp tục
tìm kiếm tới `--search-seconds` và **gửi lại bài tốt hơn**. Theo luật, bài hợp lệ
cuối cùng trước hạn mới là bài được áp dụng, nên gửi hai lần vừa an toàn vừa mạnh:
kể cả máy treo hay mạng đứt giữa chừng, ngày đó vẫn có bài hợp lệ đã nằm ở server.

Mọi cuộc gọi mạng đều bị chặn cứng bởi hạn của ngày. `timeout` của socket chỉ áp
cho *mỗi lần đọc*, không phải cả cuộc gọi: một server nhả dữ liệu nhỏ giọt từng kéo
dài một request tới **58 giây** dù `--timeout 2`. Quá hạn thì bot bỏ cuộc gọi và
**không gửi nữa** — gửi muộn có thể rơi sang ngày sau và ghi đè bài tốt của ngày đó.

### Kết quả đo gần nhất

`bench --maps sample_16,sample_24,sample_32 --count 3 --search-seconds 3.0`:

```
9 thắng / 0 hoà / 0 thua | bài không hợp lệ: 0 | thời gian ngày p99 3,069 s
sample_32 seed 0:  ta (24, 109, 157)  |  đối thủ tốt nhất (24, 87, 130)
```

Trận luyện tập 16×16 với `greedy` và `sample`: ta (12, 52, 69) so với greedy
(12, 43, 56) — L bằng nhau, thắng nhờ D.

---

## 4. Cấu trúc

| Thư mục | Vai trò |
|---|---|
| `hexudon/geometry.py`, `rules.py`, `model.py` | hình học even-r, bảng chi phí, đọc/ghi JSON |
| `hexudon/simulator.py` | **nguồn chân lý về luật**: 5 pha mỗi bước, kiểm hợp lệ |
| `hexudon/pathing.py`, `traffic.py` | Dijkstra trọng số đỉnh, dự báo giao thông |
| `hexudon/planner/` | nghiệm, bộ giải mã, hàm mục tiêu, dựng nghiệm, ALNS, vai trò, bộ nhớ trận |
| `hexudon/client/` | HTTP, sandbox, vòng đời trận |
| `hexudon/offline/` | bot đối thủ, runner nhiều đội, sinh map, server luyện tập, benchmark |
| `tests/` | kiểm thử, gồm golden test tái hiện ví dụ từng bước của ban tổ chức |

---

## 5. Ngày thi

### Trước giờ thi

1. Nếu định đổi mật khẩu trên website thì đổi **trước**, rồi mới lấy token (icon 🔑).
2. Dán token vào `TOKEN=` trong `.env`.
3. `python -m unittest discover -s tests` — phải xanh toàn bộ.
4. `python -m hexudon games --live` — thấy trận thi, ID trận và các câu.

### Trong giờ thi

```bash
python -m hexudon web --live --match <ID hoặc tên trận BTC công bố> --follow
```

Bot tự chờ cửa sổ chọn xe, chọn vai trò, chơi từng ngày và bắt mọi câu của trận kể cả
câu chạy gối đầu. Trận chưa được công bố thì bot hỏi lại mỗi 15 s cho tới khi thấy, nên
chạy lệnh sớm trước giờ cũng được. Ctrl-C **một lần** chỉ làm bot thôi bắt câu mới, các
câu đang chơi vẫn chơi nốt; Ctrl-C lần hai mới dừng hết. Nếu manager API lỗi, chạy từng
câu bằng `python -m hexudon web --live --game <ID câu>`.

Máy chạy bot (thi thử Mock Contest với hai token — 16 bot trên laptop 8 lõi — đã làm
nhiều lần nộp bị timeout):

- ngày thi chỉ một token: `--live`, không chạy thêm đội test nào trên cùng máy;
- số lõi CPU ≥ số câu chạy cùng lúc: mỗi câu là một bot tìm kiếm dùng trọn một lõi;
- không làm việc nặng khác trên laptop trong giờ thi;
- `--follow` chỉ chạy trên **một** máy: hai máy cùng đội sẽ nộp đè bài của nhau.

Trong lúc bot chạy: **đừng chọn loại xe hay nộp bài trên website** cho câu bot đang chơi
(bài hợp lệ cuối cùng được dùng, hai bên sẽ ghi đè nhau), và đừng mở nhiều tab cùng một
trận (chung giới hạn tần suất của server).

Bot tự xử lý: server chưa mở (409), bản đồ còn giấu, poll quá nhanh (429), mất mạng,
token bị từ chối giữa trận (bot gửi lại mỗi 5 s và tự đọc lại `.env`: lỗi thoáng qua tự
hết, token hết hạn thì dán token mới vào `.env` là bot chơi tiếp), vào muộn hoặc khởi
động lại giữa trận (bỏ qua bước chọn xe, chơi các ngày còn lại).

### Server kiểu PTIT (dự phòng)

```bash
python -m unittest discover -s tests
python -m hexudon serve --size 16 --teams 1 &
python -m hexudon play --url http://127.0.0.1:8099 --match m-local --token tok-0 --search-seconds 3.0
```

Vào trận thật chỉ đổi `--url`, `--match`, `--token`, và nhớ `--replay`.

Bot tự xử lý các tình huống sau, không cần can thiệp — mỗi dòng đều có test khoá lại:

| Tình huống | Bot làm gì |
|---|---|
| Server chưa mở (HTTP 425) | tiếp tục hỏi, 250 ms một lần |
| Sai token (HTTP 401/403) | dừng ngay với thông báo rõ, thay vì hỏi im lặng mãi |
| Poll quá nhanh (HTTP 429) | chờ rồi thử lại |
| Mất mạng, timeout, lỗi 5xx | thử lại giãn cách tăng dần, nhưng không bao giờ quá hạn của ngày |
| Server trả dữ liệu nhỏ giọt | bỏ cuộc gọi khi hết hạn, thay vì treo hàng chục giây |
| Bài bị từ chối | gửi bài an toàn; vẫn bị từ chối thì gửi bài đứng yên (luôn hợp lệ) |
| Bị từ chối bằng HTTP 200 kèm `reason: E_*` | vẫn hiểu là bị từ chối, không phải thành công |
| Server đòi kiểu bọc JSON khác | tự thử `[...]`, `{"actions": ...}`, `{"types": ...}` rồi nhớ kiểu đúng |
| Server đánh số ngày từ 1 | nhận ra qua lỗi số bước, tự lùi chỉ số ngày cho cả trận |
| Trường lạ hoặc thiếu trong `/setup`, `/state` | nhận tên thay thế, hỏi lại, không chết |
| Server ẩn nhiên liệu (`fuel: null`) | coi như bình đầy, không phải bình cạn |
| Server không nhận mảng vai trò | chuyển sang đúng vai trò server đang hiểu |
| Planner lỗi bất ngờ | mất đúng ngày đó, các ngày sau vẫn chơi bình thường |
| Lỗi 5xx thoáng qua | không coi là hết trận; chỉ dừng khi server báo `finished` |
| Server biến mất hẳn | thoát sau `--give-up-seconds`, ghi rõ lý do |

---

## 6. Test bot

Ba mức, từ nhanh tới giống ngày thi nhất. Mọi lệnh chạy ở thư mục gốc repo.

### 6.1. Test tự động (khoảng 2 phút, không cần mạng)

```bash
python -m unittest discover -s tests      # phải ra: Ran 172 tests ... OK
```

### 6.2. Tập dượt trên máy bằng server giả (không cần mạng, không cần token thật)

Server giả nói đúng giao thức của server thi (cửa sổ chọn xe giấu bản đồ, hạn ngày,
practice tự nhịp, replay từng bước). Nó ghi token giả vào `experiments/local.env` và in
sẵn các lệnh để copy:

```bash
# cửa sổ 1: server giả, mỗi ngày 30 s như thi thật
python -m hexudon serve --web --day-seconds 30

# cửa sổ 2: copy lệnh server in ra, ví dụ
python -m hexudon games --url http://127.0.0.1:8099 --env experiments/local.env
python -m hexudon web --url http://127.0.0.1:8099 --env experiments/local.env \
       --match 1 --follow --token-env TEST_DAU_02
python -m hexudon web-bench --url http://127.0.0.1:8099 --env experiments/local.env --match 2
```

Tập sự cố: thêm `--faults 429,5xx,401` khi mở server giả. Với `401`, sau `--revoke-after`
giây (mặc định 60) server thu hồi token giữa trận và ghi token mới vào
`experiments/local.env` — bot phải tự đọc lại và chơi tiếp.

### 6.3. Test trên server thật bằng token test

Không cần `--live`: mặc định bot dùng `TEST_DAU_02`.

| Mục đích | Lệnh | Thời gian |
|---|---|---|
| Xem trận, câu, trạng thái | `python -m hexudon games` | vài giây |
| Practice nhanh, kiểm kết nối | `python -m hexudon web --match "Practice Match" --question Q01 --search-seconds 3 --role-seconds 3` | ~1 phút |
| Practice đúng nhịp thi thật | `python -m hexudon web --match "Practice Match" --question Q02` | ~5–6 phút |
| **Thi thử có giờ — giống ngày thi nhất** | `python -m hexudon web --match "Mock Contest" --follow --token-env TEST_DAU_02` | tới khi dừng |
| Loạt practice, so điểm đội khác | `python -m hexudon web-bench --match "Practice Match" --jobs 3` | ~20–25 phút |

Mock Contest tự chạy lại mỗi câu sau 10 phút nên bot bắt câu mới liên tục. Ctrl-C
**một lần**: thôi bắt câu mới, các câu đang chơi vẫn chơi nốt; **lần hai**: dừng hết.

### 6.4. Đọc kết quả

- Tốt:
  - `KẾT QUẢ …: L=… D=… Q=…, hạng …` sau mỗi câu (với `--follow`: dòng `xong … : L=… D=… Q=… hạng …, lệch simulator 0 ngày`);
  - `đối chiếu … với replay của server: khớp hoàn toàn` — simulator hiểu luật đúng như server.
- Cần xem lại ngay nếu thấy: `LỆCH LUẬT`, `bài bị từ chối`, `gửi vai trò thất bại`,
  `quá hạn, không gửi nữa`, `Cloudflare chặn`, `server từ chối token`.
- File trong `replays/`:
  - `-summary.json`: điểm, hạng, kết quả đối chiếu (`replay_check`);
  - `-replay.json`: replay của server;
  - `.jsonl`: mọi request/response của lần chơi;
  - `.log`: log của từng tiến trình con khi chạy `--follow` hoặc `web-bench`.
- `web-bench` ghi thêm báo cáo `experiments/web-bench-<giờ>.json`.
- Trên Practice, trường `days_submitted` trong điểm server trả không đáng tin (đã thấy
  luôn báo 55); L, D, Q thì đúng cho từng câu.

### 6.5. Lưu ý khi test

- Đừng dùng `--live` khi test — nó dùng `TOKEN` của đội thật.
- `TEST_DAU_01` trùng `TOKEN`: test bằng nó là chơi dưới tên đội DAU 1. Practice được
  bot reset sau khi đã lưu kết quả, nên đội khác không đọc được lộ trình.
- Thi thử Mock Contest nên dùng **một** token: hai token trên 8 câu cùng lúc (16 bot)
  từng làm laptop 8 lõi quá tải và nhiều lần nộp bị timeout.
- Trong lúc bot chạy, đừng chọn loại xe hay nộp bài trên website cho câu bot đang chơi.
- Token test cũng chỉ sống 48 giờ — hết hạn thì lấy token mới rồi dán vào `.env`.

### 6.6. Các nhóm test đáng chú ý

Phần kết nối server thi:

- **`tests/test_web_transport.py`**, **`tests/test_web_runner.py`** — bot chơi trọn trận
  trên server giả qua HTTP thật: practice gửi đúng một lần mỗi ngày, contest chờ cửa sổ
  chọn xe, `--follow` hai đội trên hai câu gối đầu, `--live` không chạm endpoint practice
  và không để lọt token vào file nào.
- **`tests/test_replay_check.py`** — đối chiếu replay: khớp khi server chạy đúng luật của
  ta, và chỉ đúng ngày, bước, xe khi có một giá trị bị sửa.
- **`tests/test_web_api.py`** — đọc response thật của server (fixture chụp ngày
  2026-09-21) bằng parser sẵn có.

Phần lõi:

- **`tests/test_simulator_golden.py`** — tái hiện đúng ví dụ 8 tác nhân trong tài
  liệu bổ sung (補足資料) của ban tổ chức Nhật, kiểm tới từng ô, từng đơn vị nhiên
  liệu, từng phần udon và cả bốn con số lưu lượng cuối ngày. Đây là bài kiểm neo
  toàn bộ phần luật.
- **`tests/test_decoder.py`** — tính chất quan trọng nhất của planner: mọi nghiệm,
  kể cả nghiệm vô lý sinh ngẫu nhiên, đều ra kế hoạch hợp lệ. Một lệnh sai làm hỏng
  cả ngày của đội.
- **`tests/test_resilience.py`** — mỗi test là một cách thua trận **đã quan sát
  được thật**, không phải tình huống tưởng tượng: lỗi planner giết cả trận, cuộc
  gọi mạng kéo 58 giây, lỗi 503 thoáng qua kết thúc trận sớm, `fuel: null` đọc
  thành bình cạn, ngân sách vai trò ăn mất ba ngày đầu.

---

## 7. Việc còn lại

Bốn điểm đã xác định nhưng **chưa sửa** — đều là ăn thêm điểm chứ không phải lỗi
sống còn, và đều đổi hành vi tìm kiếm nên cần benchmark đối chứng trước/sau:

1. ALNS vứt bài an toàn rồi dựng lại từ nghiệm khác, nên có lúc thua chính bài đã
   vứt (đo được 6/45 ngày, tệ nhất mất 6 "chuỗi-ngày" ở ngày cuối `sample_32`).
2. `repair()` không gọi lại `add_fuel_meets`, nên kế hoạch tiếp nhiên liệu rụng dần
   trong lúc tìm kiếm.
3. `V_fuel` xét chi phí ô hàng xóm thay vì ô đang đứng — ngược với luật, bỏ sót
   đúng ca xe còn 1 nhiên liệu trên đường hoặc núi.
4. Băng chấp nhận của ALNS tính theo phần trăm điểm tuyệt đối nên giãn theo cỡ map;
   có lúc cho phép mất trọn một chuỗi-ngày.
