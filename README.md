# HEXUDON — bot thi đấu Procon Việt Nam 2026

Bot chơi **HEXUDON** (Tối ưu hoá lộ trình HEXUDON), phần thi Tranh tài của Procon
Việt Nam 2026 / NAPROCK PROCON lần 18.

Python thuần, **không cài gì thêm** — chỉ thư viện chuẩn. Yêu cầu Python 3.10 trở lên.
Có hai phần tuỳ chọn; thiếu thì bot tự bỏ qua. Một là scipy, cho bước MILP. Hai là bộ giải tour bằng Rust (`rust/tour`), build bằng
`cargo build --release --offline` (mục 3).

```bash
git clone <repo> && cd procon-2026
python -m unittest discover -s tests     # 405 test, phải xanh toàn bộ (Windows, macOS, Linux)
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

`TOKEN` chỉ dùng được khi có `--live`; không chỉ định gì thì bot dùng `TEST_DAU_01` (đội DAU 1 của mình).
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
       --token-env TEST_DAU_01                                    # thi thử có giờ, một token
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
nhất sau khi tìm kiếm xong, thay vì gửi tới ba lần như chế độ HTTP.

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

# như trên nhưng tất định và song song: ngân sách theo số vòng ALNS, 11 trận một lúc
python -m hexudon bench --maps sample_16,sample_24,sample_32,random24 --count 6        --search-iterations 1000 --role-iterations 100 --jobs 11 --out bench.json

# A/B: hai bộ trọng số của bot chơi CÙNG trận nhiều đội, so từng cặp, có khoảng tin cậy
python -m hexudon ab --b fuel_bonus=2 --count 6 --jobs 11 --out ab.json

# chỉ xem bước chọn vai trò quyết định thế nào
python -m hexudon roles --map data/sample_maps/sample_32.json
```

---

## 2. Cách dùng

### Các lệnh

| Lệnh | Dùng khi nào |
|---|---|
| `api-check` | so `/openapi.json` của server thi với 12 endpoint bot dùng (không cần token); báo endpoint mới |
| `games` | liệt kê trận/câu trên server thi, kèm ID và trạng thái |
| `web` | chơi trên server thi: một câu, nhiều đội (`--token-env` lặp lại), hoặc cả trận (`--follow`) |
| `web-bench` | chạy loạt câu practice trên server thi, so điểm với đội khác |
| `viz` | web trực quan hoá trên 127.0.0.1: tạo n bot thi với nhau, xem suy luận, tua lại |
| `arena` | trận offline n bot ghi sự kiện (web `viz` gọi lệnh này) |
| `serve --web` | server giả nói giao thức của server thi, để tập dượt |
| `play` | thi đấu thật qua HTTP |
| `sandbox` | thi đấu khi ban tổ chức chấm qua stdin/stdout |
| `serve` | dựng server luyện tập tại chỗ |
| `sim` | chạy một trận offline, so các chiến thuật |
| `bench` | chạy nhiều map × nhiều seed, ra bảng thắng/thua; `--jobs` chạy song song, `--search-iterations` cho kết quả tất định |
| `ab` | hai bộ trọng số của bot chơi cùng trận nhiều đội; so từng cặp trận, báo ΔL, ΔD, ΔQ kèm khoảng tin cậy 95% |
| `roles` | xem riêng bước chọn vai trò xe |

### Tham số thời gian (quan trọng nhất trong ngày thi)

Lệnh `web` và `web-bench` (server thi hexudon.hairbui76.id.vn) — **để nguyên mặc định**:

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--search-seconds` | `auto` | ngân sách tìm kiếm mỗi ngày: hạn của ngày (contest) hoặc độ dài ngày BTC cấu hình (practice), trừ lề mạng |
| `--role-seconds` | `auto` | ngân sách chọn vai trò: theo cửa sổ chọn xe server báo, đã chừa giờ để gửi vai trò tới nơi |
| `--search-jobs` | `auto` | số seed ALNS mỗi ngày, mỗi seed một tiến trình (R7); `auto` = min(4, số lõi / 2); `1` = một tiến trình như cũ |
| `--net-margin` | `2.0` | lề chừa cho mạng, trừ khỏi hạn của server |
| `--timeout` | `5.0` | hạn cứng **mỗi cuộc gọi HTTP**; riêng `/result` và `/replay` sau trận được chờ 30 s mỗi lần, thử tối đa 3 lần |
| `--poll-seconds` | `0.5` | nhịp hỏi `/state` |
| `--give-up-seconds` | `120` | mất liên lạc, hoặc token bị từ chối, quá lâu thì thoát thay vì quay vô hạn |
| `--pipeline` | bật | lập trước: lập sẵn bài ngày d+1 trong lúc ngày d chạy (ngày 0 trong cửa sổ chọn xe), gửi ngay khi ngày mở; `--no-pipeline` để tắt |
| `--rust-seconds` | `3.0` | mỗi ngày tối đa ngần này giây cho bộ giải tour bằng Rust (mục 3); `0` = tắt; chưa build `rust/tour` thì tự bỏ qua |

`auto` đọc độ dài ngày và cửa sổ chọn xe từ chính server, nên BTC đổi thông số thì bot
đổi theo. Bình thường **đừng đặt tay** `--search-seconds` hay `--role-seconds` cho `web`.

Lệnh `play` và `sandbox` (server kiểu PTIT):

| Tham số | Mặc định | Ý nghĩa |
|---|---|---|
| `--search-seconds` | `3.0` | ngân sách tìm kiếm mỗi ngày |
| `--net-margin` | `1.5` | lề chừa cho mạng, trừ khỏi hạn của server |
| `--role-seconds` | `3.0` | ngân sách chọn vai trò trước ngày 0 |
| `--search-jobs` | `auto` | số seed ALNS mỗi ngày (R7), như lệnh `web` |
| `--rust-seconds` | `3.0` | bộ giải tour bằng Rust, như lệnh `web` |
| `--timeout` | `2.0` | hạn cứng **mỗi cuộc gọi HTTP** |
| `--poll-seconds` | `0.25` | nhịp hỏi `/state` (dưới 0,2 s có thể bị HTTP 429) |
| `--give-up-seconds` | `120` | mất liên lạc quá lâu thì thoát thay vì quay vô hạn |
| `--seed` | `0` | cùng seed + cùng đầu vào ⇒ cùng kế hoạch, để tái hiện khi gỡ lỗi |

**Quy tắc đặt `--search-seconds` cho `play`**: bằng hạn mỗi ngày của ban tổ chức trừ
khoảng 2 giây. Hạn 5 s → để mặc định 3.0. Hạn 60 s → đặt 58. Bước MILP orienteering chỉ
chạy khi ngày có từ 10 s trở lên (`or_min_seconds`); ngắn hơn thì bot chỉ dùng ALNS như cũ.

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
bộ dựng nghiệm, không tìm kiếm), `sample` (bot mẫu), `wait` (đứng yên). Thêm các kiểu
đội khác giả lập theo repo công khai của các đội (`hexudon/offline/opponents.py`):
`nearest` (spot gần nhất, toàn xe tuần tra), `chain_greedy` (tham lam theo chuỗi, xe
tiếp đuổi xe ít xăng nhất), `escort` (mỗi xe tiếp đi kèm một xe tuần tra), `hub` (một
xe tiếp đứng ở trạm trung tâm), `road_block` (xe tiếp đỗ chặn ô đường, chỉ để thử
phòng thủ), `servings_alns` (ALNS gần như chỉ tối đa số phần) và `all_patrol` (ALNS
toàn xe tuần tra).

Lưu lượng đường chia cho **số đội thật trong trận**, tức số bot, không phải `players`
ghi trong map mẫu (ba map mẫu ghi 10). Với `sim`, `--players` lớn hơn số bot giả lập
thêm các đội có tên nhưng đứng yên cả trận. Xếp hạng theo L, D, Q rồi tổng thời gian
phản hồi (thời điểm gửi bài cuối cùng mỗi ngày), như đề.

Map ngẫu nhiên phủ đủ phạm vi của đề: số bước từng ngày không nhất thiết tăng dần,
bình xăng có thể nhỏ hơn một ngày đi, hai ngưỡng giao thông có thể bằng nhau. Tên map:
`random24` là 24×24, `random8x32` là rộng 8 cao 32 (map dài và hẹp), `random` bốc ngẫu
nhiên cả rộng lẫn cao.

---

## 3. Chiến thuật

### Điểm số quyết định điều gì

Xét theo **thứ tự từ điển** — hơn ở tiêu chí trước là thắng, bất kể tiêu chí sau:

1. **L** — số *loại* udon (brand) khác nhau thu được cả trận
2. **D** — tổng theo ngày của số loại khác nhau thu trong ngày
3. **Q** — tổng số phần udon
4. thời gian phản hồi thấp hơn

Trên ba map mẫu, mọi bot mạnh thường lấy đủ mọi chuỗi nên L hay hoà và D phân thắng
thua. Nhưng đó không phải luật: map có chuỗi hiếm (chỉ một điểm, ở xa, sau núi hoặc
ao) thì sót một chuỗi là thua dù D cao (`cases.md` 6.3, 10.8). Vì vậy bot giữ đúng thứ
tự của đề: **một loại mới cả trận luôn hơn mọi số loại trong ngày**, một loại mới
trong ngày luôn hơn mọi số phần.

### Năm trụ cột

**1. Hàm mục tiêu theo đúng thứ tự từ điển.** Thưởng cho một loại mới cả trận là
`max(0,5; số chuỗi + 1) × B`, lớn hơn mọi phần D một ngày có thể thêm, nên bot lấy
chuỗi hiếm sớm thay vì để tới ngày cuối. Một chuỗi *mới trong ngày* (B) hơn hẳn mọi số
phần udon. Cộng thêm giá trị cuối ngày: chuỗi chưa ghé mà sắp không còn kịp thì bị
phạt nặng; kết thúc ngày trên spot hoặc gần chuỗi chưa ghé thì được thưởng; xe sắp
kẹt xăng (xăng ít hơn mức cần để rời **ô đang đứng**) bị phạt theo số ngày còn lại.

**L cứng (từ 16/11/2026).** Spot của chuỗi chưa từng thu luôn được xét khi chèn, dù
xa hơn 10 spot gần nhất. Chuỗi mới được chèn trước, chuỗi khó chèn nhất đi đầu
(regret). Sau tìm kiếm còn một bước vá L: chuỗi mới tới được hôm nay mà bài bỏ qua thì
được chèn lại. Chuỗi chưa thu mà xe gần nhất đã xa hơn nửa số bước còn lại của trận
thì bị phạt bằng một chuỗi mới. Trước đó, giải đấu giả lập đo được bot bỏ sót chuỗi
chỉ có một spot ở góc bản đồ (2/30 trận trên bản đồ tập dượt Nhật).

**2. Chọn vai trò bằng mô phỏng, trước ngày 0.** Thử các phương án 0, 1, 2, 3 xe
tiếp nhiên liệu ở những vị trí trung tâm khác nhau, **chơi thử trọn trận** từng
phương án rồi chọn phương án điểm cao nhất. Phương án nào chưa chơi đủ số ngày thì
bị loại, thay vì đem so với phương án chơi đủ — so lẫn lộn làm việc *tăng* ngân
sách đôi khi lại cho kết quả *tệ hơn*.

Từ 17/11/2026 (R5), mỗi phương án được chơi thử trên **hai kịch bản giao thông**:
dự báo cũ, và "nhiều đội" (mọi đội cùng chơi, một nửa số đội khác đi như ta). Điểm
hai kịch bản được cộng lại. Chơi một mình thì đường luôn thông, nên rollout từng đòi
2 xe tiếp ở bản đồ Nhật trận 3. Không chọn 0 xe tiếp, trừ khi xăng ≥ 3 × số bước ngày
đầu và trận không quá 5 ngày. Cửa sổ chọn xe còn từ 6 giây thì 3 phương án dẫn đầu
được tinh chỉnh song song trên nhiều tiến trình. Đo trên 120 cặp ở sảnh 10 đội: D
+0,16, Q +0,6 mỗi trận, không trận nào mất L (`role_traffic = 0` là như cũ).

**3. Tiếp nhiên liệu bằng điểm hẹn.** Điểm hẹn đặt ngay trên lộ trình xe tuần tra;
xe tiếp nhiên liệu tới trước và đợi. Trong cùng một bước, simulator **trừ xăng trước**
rồi mới **nạp sau** (theo ví dụ 8 tác nhân của BTC Nhật), nên xe đi kèm xe tiếp luôn
đầy bình cuối mỗi bước. Xe bám sau một ô chỉ gặp được xe tiếp khi hai ô tốn số bước
khác nhau (ví dụ núi 3 bước, đồng bằng 2), nên bot không trông vào cách đó mà hẹn
cùng một ô. Câu chữ của đề còn nói *ra lệnh đi khi thiếu xăng* là bài không hợp lệ,
nên xe tiếp phải tới điểm hẹn **trước lúc xe tuần tra nhận lệnh rời ô**; planner kiểm
mọi bài theo cả hai cách hiểu (`strict_fuel`). Xe tiếp tới muộn không quá 8 bước thì
xe tuần tra chờ thêm; muộn hơn thì bỏ điểm hẹn — bài vẫn hợp lệ. Hết điểm hẹn, xe
tiếp không đỗ trên ô đường tới cuối ngày.

**4. Dự báo giao thông.** Lưu lượng ô đường tính từ stay-step của **mọi đội trong
hai ngày trước** chia cho số đội. Bot ước lượng ô nào sẽ đông đúc hoặc ùn tắc ngày
mai dựa trên độ phổ biến của hành lang, cộng vết của chính mình **cả hôm nay lẫn hôm
qua**, rồi dùng dự báo đó cho giá trị cuối ngày. Ở ngày 2, dự báo cho ngày 3 không
bao giờ tốt hơn tình trạng hôm nay, vì lưu lượng ngày 3 gồm cả vết ngày 1 lẫn ngày 2.
Ngày 0 mọi đường đều thông thoáng nên bot khai thác đường mạnh; các ngày sau nó tránh
dần những hành lang đang xấu đi.

Có sẵn **dự báo tự hiệu chỉnh** (R6, `traffic_calib = 1`). Mỗi sáng, bot so 18 giả
định lưu lượng của các đội khác với đường thật rồi chọn giả định đúng nhất. Giả định
gồm: "như ta" theo nhiều tỉ lệ, theo độ phổ biến, "mai như hôm nay", và công thức cũ.
Bot cũng tính vết xe đỗ của đối thủ. Ở sảnh 10 đội, dự báo này đúng 72,9% số ô so với
63,8% của công thức cũ. Trước khi có hàm giá trị học được (B4), nó không thêm điểm (D
−0,33). Khi đã có B4 thì có lợi: D +0,56 ± 0,27, Q +0,9 trên 120 cặp. Vì vậy **bật mặc
định** từ 17/11/2026.

**5. Tìm kiếm ALNS trên nghiệm "lộ trình spot + điểm hẹn".** Bộ giải mã là **toàn
phần**: mọi nghiệm, kể cả nghiệm vô lý, đều ra kế hoạch mà simulator chấp nhận theo
cả hai cách hiểu luật xăng — đã fuzz khoảng 1.900 ca đối kháng, 0 bài bị simulator
từ chối (server thật có thể khác ở chỗ simulator chưa mô tả đúng; xem đối chiếu
replay). Nghiệm nào không sửa nổi thành hợp lệ thì bị chấm điểm tệ nhất chứ không
bao giờ được chọn. ALNS bắt đầu từ chính bài an toàn, nên bài cuối không kém nó, và
chỉ nhận nghiệm kém nghiệm tốt nhất không quá 0,25 B. Từ 16/11/2026, 30% ngân sách
cuối ngày là **pha Q**: khoá (chuỗi mới, D) hôm nay, chỉ nhận nghiệm kém không quá 2
phần, và chỉ gửi lại khi (L, D, Q) hôm nay tăng ngặt.

**Rollout cuối ngày (R4, từ 16/11/2026).** Trước pha Q, bot giữ tới 8 nghiệm khác nhau
về trạng thái cuối ngày (cả nghiệm kém một chuỗi hôm nay), chạy thêm hai lượt ALNS
ngắn với trọng số "tiết kiệm xăng" và "cuối ngày × 2", rồi chơi tiếp 3 ngày bằng bộ
dựng tham lam cho từng nghiệm. Nghiệm có (L, D, Q) của hôm nay cộng các ngày sau lớn
nhất được chọn và gửi ở lượt cuối, kể cả khi điểm trong ngày của nó thấp hơn. Không bao
giờ chọn nghiệm ít chuỗi mới hơn. Đo trên giải đấu giả lập: D +0,5, Q +0,9 mỗi trận ở
300 vòng/ngày; Q +2,0 ở 2000 vòng. Ngày cuối, đạt cận trên của hôm nay thì dừng (R3).

**Hàm giá trị học được (B4, từ 17/11/2026).** Giá trị cuối ngày đặt tay được cộng thêm
một mô hình tuyến tính học từ tự chơi (`planner/value.py`, `planner/value_model.json`).
Mô hình nhận 30 đặc trưng của trạng thái cuối ngày (khoảng cách tới chuỗi chưa thu,
xăng, xe tiếp, độ phủ spot...) và dự đoán phần thu các ngày sau so với các nghiệm khác
của cùng ngày. Dữ liệu là 9.420 nghiệm ứng viên của R4 trên 400 bản đồ ngẫu nhiên, mỗi
nghiệm được chơi tiếp bằng ALNS tới hết trận; không dùng bản đồ của giải đấu. Đo trên 120
cặp ở sảnh 10 đội: D +0,62, Q +2,5 mỗi trận, không mất L, thời gian lập kế hoạch tăng
khoảng 6%. `value_weight = 0` là như cũ. Học lại bằng
`thi_nghiem_ai/du_lieu_v.py` rồi `hoc_v.py --xuat`. Biến môi trường `HEXUDON_VALUE_MODEL`
chọn một file mô hình khác mà không ghi đè file mặc định. Vòng tự chơi thứ hai (V2, học
từ bot đã có V1) tốt hơn khi đo offline nhưng thiệt trong giải đấu (D −0,54), nên vẫn
dùng V1.

**Nhiều seed song song (R7, từ 17/11/2026).**
- Mỗi ngày, ngoài seed của tiến trình chính, bot chạy thêm 3 seed ALNS trên các tiến
  trình con (`--search-jobs`, mặc định `auto` = 4 trên máy 8 lõi trở lên). Mỗi seed chạy
  trọn ngày, kể cả rollout R4 và pha Q.
- Hết giờ, bài có (L, D, Q) của hôm nay cộng các ngày sau (chơi tiếp tham lam) lớn nhất
  được gửi ở lượt cuối.
- Pool tiến trình mở một lần lúc chọn vai trò. Bot chỉ dùng Pool sau khi mọi tiến trình
  con trả lời; nếu không, bot chạy một seed như cũ chứ không bao giờ chờ.
- Đo trên 120 cặp ở sảnh 10 đội: D +1,08 ± 0,26, Q +2,3 ± 0,6 mỗi trận, không mất L.
- Giải đấu và `bench` (theo số vòng) chạy các seed tuần tự để kết quả tất định.

**Sửa sau khi thua DAU 2 cả 4 trận contest (từ 18/11/2026).** Trên server VN, L và D
chạm trần mọi ngày, nên số phần Q quyết định. Ta thua DAU 2 37–135 phần mỗi trận. Báo cáo
`de-xuat-chien-thuat/phan-tich-va-de-xuat-chien-thuat.md`, mục 4.11.
- **Hạng mục cuối ngày mềm tính bằng đơn vị phần** (`soft_unit = 20`, `soft_auto = 1`).
  - Các hạng mục này là: kết thúc trên spot, chuỗi gần, xăng còn lại, tự gây tắc, và V. Trước đây chúng tính theo B = 1000, nên bot đổi hàng chục phần lấy bình xăng đầy.
  - Nay mỗi đơn vị bằng 20 phần, nhưng chỉ khi D đã an toàn: đủ L và hôm qua đạt trần D. Ngày khác vẫn theo B.
  - Luật giữ L và D tương lai (chuỗi không kịp ghé, hạn chót, xe kẹt xăng) vẫn theo B.
- **Vá D hôm nay** (`today_chain_first = 1`): chuỗi thiếu hôm nay mà một xe tới được thì
  được chèn vào, có thể bỏ điểm trùng chuỗi hoặc cắt đuôi tour để lấy chỗ. Trước đây ngày
  69 bước ta hụt 1 chuỗi; nay từ đúng trạng thái của DAU 2 thì 0/40 ngày thiếu D.
- **Pha Q chiếm 50% ngân sách, với B = 0,05** (`q_share = 0.5`, `q_B = 0.05`).
- **Chọn vai trò:**
  - thử mọi vị trí cho 1 xe tiếp;
  - D của lần chơi thử trong khoảng 2% coi như hoà rồi so Q;
  - vòng 2 có 4 phương án (`role_*`).
  - Khi theo giây, vòng 1 chạy song song trên Pool và vòng 2 vừa đúng một lượt tiến trình.
- **Kết quả đo:**
  - Cả trận trên 4 bản đồ contest, bot tự chọn vai trò: Q +132, D +2,7 so với bản 17/11.
  - Sảnh 10 đội kiểu Nhật: D trung tính, không mất L.
  - Bản đồ VN: Q +20.
- Mọi núm đặt về 0 (`role_seeds = 1`, `q_share = 0.3`) thì tái hiện đúng hành vi trước 18/11.
  Biến thể `cu_17_11` trong `thi_nghiem_ai/doi_chieu_dau2.py` ghim bản đó.
- Có code nhưng để tắt, vì đo không thấy lợi: tìm kiếm cục bộ ủ mô phỏng
  (`planner/local.py`, `ls_share`) và kẹp phần mềm (`soft_cap`).

**Tăng tốc ALNS (từ 19/11/2026), bài ra giống hệt:**
- Bảng spot gần nhất dựng một lần cho mỗi ngữ cảnh ngày (`construct.neighbours`). Trước đây bảng bị dựng lại sau mỗi vòng.
- `InsertionState` lưu sẵn ô của từng tour.
- Bộ mô phỏng, khi không ghi vết, nhảy thẳng tới bước có lệnh kết thúc tiếp theo.
- Trên bản đồ VN, số vòng mỗi giây tăng 1,3–1,45 lần: 224 → 325, 299 → 394, 419 → 539.
- `tests/test_speed_golden.py` giữ đúng từng bài như trước khi tăng tốc. Nếu cố ý đổi hành vi của planner thì chụp lại bằng `python -m tests.test_speed_golden --tao`.

**Hàm giá trị học được theo số phần, V-VN (bật từ 19/11/2026):**
- Dùng ở ngày D an toàn, tức những ngày hạng mục cuối ngày tính bằng phần.
- Phần mềm của giá trị cuối ngày = `value_q_weight × V_vn`, cộng hạng mục "chuỗi gần" (`value_q_keep_near`).
  - Các hạng mục mềm tự viết khác và V1 bị bỏ.
  - Luật giữ L, D (xe kẹt xăng…) vẫn theo B.
- `V_vn` là cây GBDT dự đoán số phần 2 ngày sau, đọc từ `planner/value_model_vn.json`.
  - Học trên 400 bản đồ sinh theo phân bố server VN.
  - Ngoài tập học, cách chấm cũ bỏ lỡ 8.661 phần so với lựa chọn tốt nhất; chỉ xét hôm nay bỏ lỡ 7.584; V-VN bỏ lỡ 7.204.
- **Kết quả đo:**
  - Cả trận, 4 bản đồ contest và 12 bản đồ sinh, 3 seed: Q +5,5 ± 2,3 mỗi trận (riêng contest +19,7), tổng D −1 trên 48 trận.
  - Số trận không đạt trần D giảm từ 4/45 xuống 1/45.
  - Sảnh 10 đội kiểu Nhật: ΔD +0,16, không mất L.
- `value_q_weight = 0` tái hiện đúng hành vi 18/11. Biến thể đó là `mac_dinh_18_11` trong `thi_nghiem_ai/doi_chieu_dau2.py`.
- Học lại: `thi_nghiem_ai/du_lieu_v_vn.py`, rồi `hoc_v_vn.py --goc hom_nay --xuat --loai cay --h 2 --K 0`.

**Orienteering chính xác bằng MILP, và luật D cứng của rollout (bật từ 19/11/2026):**
- Mỗi xe chỉ thu một phần ở mỗi spot mỗi ngày, và trên bản đồ kiểu VN kho mỗi spot ≥ số xe tuần tra. Vì vậy bài trong ngày tách thành một bài orienteering cho mỗi xe: ghé nhiều spot nhất trong số bước của ngày.
- `planner/orienteering.py` giải từng xe bằng MILP (scipy `milp` / HiGHS, mỗi xe một luồng), rồi giải mã, thêm điểm hẹn xe tiếp xăng và vá L/D. ALNS xuất phát từ bài này nếu nó tốt hơn theo `score_day`.
  - Từ đúng trạng thái của DAU 2, lời giải tối ưu bằng đúng số phần DAU 2 đạt ở 5/6 ngày thử.
  - Núm: `or_seconds` 20, `or_share` 0,75, `or_k_near` 10, `or_fuel` 0.
  - Theo giây, ngày còn dưới `or_min_seconds` (10 s) thì bỏ MILP: ở 3–5 s/ngày MILP làm thiệt (−47, −31 Q trên 4 trận), từ 15 s thì có lợi (+28; 30 s +61).
  - Chọn vai trò và các seed phụ của R7 tắt MILP.
  - Không có scipy thì bot bỏ qua bước này và chạy như trước.
- **Luật D cứng của rollout** (`rollout_d_hard` 1, `rollout_d_streak` 2):
  - Khi đã chơi ít nhất 2 ngày, ngày nào cũng đạt trần D, và có bài hôm nay đạt trần, rollout R4 và chọn seed R7 không được đổi D hôm nay lấy D dự báo của các ngày sau.
  - Trên bản đồ VN, những lần đổi như vậy là nhiễu của lượt chơi tiếp, và từng làm mất 1 D (chuỗi 20 → 19).
  - Trên bản đồ thiếu D (bản đồ mẫu), đổi như vậy là đúng. Bản áp dụng ở mọi ngày an toàn từng làm mất 0,5 D mỗi trận ở đó, nên luật chỉ áp khi D "dễ".
- **Kết quả đo:**
  - Cả trận, 4 bản đồ contest và 12 bản đồ sinh, 3 seed: Q +16,5 ± 2,6 mỗi trận (contest +21,4), thắng Q 43/48 cặp, tổng D +1.
  - Sảnh 10 đội kiểu Nhật: ΔD +0,36 trên 75 cặp không phải VN, không mất L. Bản đồ mẫu (48 cặp): ΔD +0,29.
- Biến thể `mac_dinh_19_11` trong `thi_nghiem_ai/doi_chieu_dau2.py` (và `TRUOC_E`) tái hiện hành vi trước đó.

**Bộ giải tour bằng Rust (`rust/tour`, 25/11/2026):**
- Cùng bài toán với MILP nhưng cho cả đội một lúc: team orienteering có phủ chuỗi, thứ tự (chuỗi mới, D, Q, ít bước). Thuật toán LNS: chèn tham lam ưu tiên chuỗi còn thiếu, 2-opt, or-opt, ủ mô phỏng, khởi động lại khi kẹt.
- Python gọi file nhị phân qua stdin/stdout (`planner/rust_tours.py`). Bài Rust được giải mã, thêm điểm hẹn và vá L/D như MILP, rồi làm điểm xuất phát của ALNS nếu tốt hơn.
- Bài Rust chỉ được nhận khi thoả hai điều kiện. Thứ nhất (`rs_ready`), không để ít xăng cho ngày mai hơn bài đang có. Thứ hai (`rs_look`), không kém theo lượt chơi tiếp các ngày sau. Lý do: bộ giải chỉ tối đa hôm nay. Bản đầu trên bản đồ vòng 1 Nhật lấy thêm 1 chuỗi ở ngày 0 nhưng để hai xe còn 1 và 6 xăng cho ngày 1 có 36 bước, nên D ngày 1 tụt từ 9 xuống 5.
- Từ đúng trạng thái đầu ngày của DAU 2: trong 1 s, bộ giải đạt bằng số phần của DAU 2 ở hầu hết các ngày. Bản đầu (chưa có hai điều kiện nhận) cho tổng số phần như sau: 3 s/ngày từ 3139 lên 3320; 30 s/ngày từ 3248 lên 3335 (DAU 2 đạt 3341).
- **Nới hai điều kiện (27/11):** ở 8 trận contest 25–26/11 (thua DAU 2 cả 8 vì Q), hai điều kiện trên bỏ khoảng 80 phần, vì trên bản đồ VN D luôn chạm trần.
  - `rs_safe_ld` 1: ở ngày D an toàn (đã đủ L, hôm qua đạt trần D), `rs_look` chỉ so (chuỗi mới, D) của hôm nay cộng chơi tiếp, không so Q dự báo. `rs_ready` bị bỏ khi một bình đủ cho mọi ngày còn lại (`fuel_limits` ≥ số bước lớn nhất của các ngày sau).
  - `rs_day0_lex` 1: ngày 0 nhận bài Rust khi (chuỗi mới, D, Q) hôm nay cao hơn, thay vì so `score_day` (ngày 0 các hạng mục cuối ngày tính theo B và át số phần).
  - `rs_safe_free` (mặc định 0) bỏ hẳn hai điều kiện ở ngày D an toàn. Nó mất D ở ngày ngắn kế sau (`gen:1000` ngày 7, 64 bước) và dao động mạnh trên bản đồ thiếu xăng, nên không dùng.
- Núm: `rs_seconds` (giây mỗi ngày, 0 = tắt), `rs_share` 0,3, `rs_fuel` 0, `rs_look` 1, `rs_ready` 1, `rs_safe_ld` 1, `rs_day0_lex` 1, `rs_safe_free` 0. Ba núm sau chỉ có tác dụng khi `rs_seconds` > 0.
- Bật ở các lệnh thi đấu (`web`, `web-bench`, `play`, `sandbox`) bằng `--rust-seconds` (mặc định 3). `Weights()` giữ `rs_seconds` 0, nên test, `sim`, `bench` không tốn thêm 3 s mỗi ngày và vẫn tất định. Bộ giải chạy theo giây nên bài ra phụ thuộc tốc độ máy.
- **Kết quả đo** (25/11, 3 s/ngày, bản có hai điều kiện nhận):
  - Cả trận, gồm 4 bản đồ contest và 12 bản đồ sinh, mỗi bản đồ 3 seed (48 cặp): Q +13,6 ± 2,6 mỗi trận (contest +6,0, bản đồ sinh +16,2), thắng Q ở 46/48 cặp, tổng D +11 (chỉ 1 trận giảm), không mất L.
  - Sảnh 10 đội kiểu Nhật: trên 75 cặp không phải VN, tổng ΔD −4 (−0,05 mỗi cặp, trong khoảng nhiễu). Cộng 30 seed thêm trên bản đồ mẫu thì tổng ΔD +11 trên 105 cặp. Không mất L. 15 cặp VN: Q +14,3 mỗi trận.
  - Server giả, bản đồ `f06e9b26`, có lập trước: L20 D200 Q685 (không Rust Q670), thời gian phản hồi 15,6 s, replay khớp.
- **Kết quả đo bản nới** (27/11, `rs_safe_ld` + `rs_day0_lex`, so với bản 25/11 chiều):
  - Cả trận 60 cặp (8 bản đồ contest, 12 bản đồ sinh, 3 seed): Q +3,4 ± 1,2 mỗi trận, thắng Q 41/12, tổng D +1, không trận nào giảm D, không mất L.
  - Sảnh 10 đội kiểu Nhật 90 cặp: tổng D +14, Q +39, không mất L. 30 seed thêm trên bản đồ mẫu: D +10, Q +36.
  - Lập lại từng ngày từ đúng trạng thái của 8 trận contest (27 s mỗi ngày): +70 phần, không ngày nào thấp D hơn, không ngày nào quá hạn.
  - Bản đồ thiếu xăng `1addebd7` dao động khoảng 120 Q theo seed ở cả hai bản; chưa sửa.
  - Biến thể `rs3v` / `ta_rs2` trong `thi_nghiem_ai` ghim `rs_safe_ld` 0, `rs_day0_lex` 0 để tái hiện bản 25/11.
  - Bản đầu chưa có điều kiện nhận: Q +21,5 mỗi trận, nhưng mất D trên bản đồ vòng 1 Nhật (−0,5 và −1,4 mỗi trận).
- Build một lần (Rust edition 2021, đã thử với cargo 1.99.1; crate không có thư viện ngoài nên không tải gì từ mạng): `cd rust/tour && cargo build --release --offline`. Không có file `rust/tour/target/release/tour(.exe)` thì bot bỏ qua bước này.

### Cách chia thời gian trong một ngày

Bot gửi **bài an toàn** trong khoảng 10 ms sau khi nhận trạng thái, rồi tiếp tục
tìm kiếm tới `--search-seconds`. Theo luật, bài hợp lệ cuối cùng trước hạn là bài
được áp dụng, và **thời gian phản hồi (tiêu chí 4) tính theo đúng bài đó**. Vì vậy:

- gửi tối đa **3 lần mỗi ngày**: bài an toàn, một bài giữa chừng, bài cuối;
- chỉ gửi lại khi L, D hoặc Q của hôm nay tăng, hoặc điểm tăng ít nhất 0,25 B; chênh
  lệch nhỏ hơn chỉ làm chậm thời gian phản hồi;
- bài tốt hơn rõ rệt được gửi **ngay khi tìm ra**, không đợi hết giờ tìm kiếm;
- bài cuối trùng bài đã gửi thì không gửi lại.

Kể cả máy treo hay mạng đứt giữa chừng, ngày đó vẫn có bài hợp lệ đã nằm ở server. Bộ
nhớ trận (các loại đã thu, vị trí dự đoán, vết giao thông) ghi theo đúng bài server đã
nhận, không theo bài tốt nhất bot nghĩ ra.

**Lập kế hoạch trước (`--pipeline`, mặc định bật từ 25/11/2026).** Thời gian phản hồi là
thời điểm gửi bài hợp lệ cuối cùng mỗi ngày, cộng qua các ngày. Trước đây bot tìm kiếm tới
gần hết ngày nên mất 245–273 s mỗi trận (DAU 2: 186–240 s). Trên server VN, đường không bao
giờ tắc, kho đặt lại mỗi ngày và simulator khớp server, nên trạng thái đầu ngày mai biết
trước. Vì vậy:

- Ngày 0: chọn vai trò dùng `ROLE_SHARE` (0,6) cửa sổ chọn xe, phần còn lại lập sẵn ngày 0.
- Trong lúc ngày d chạy, bot lập sẵn ngày d+1 từ trạng thái dự đoán (`strategist.predict_next`),
  dùng trọn ngân sách của ngày.
- Gần giờ mở ngày, bot hỏi `/state` mỗi 0,2 s, dựng trạng thái thẳng từ đó (bỏ lượt
  `GET /day`). Nếu khớp dự đoán thì gửi bài lập sẵn ngay, khoảng 0,02 s sau khi ngày mở.
- Chỉ lệch đường thì bot mô phỏng lại bài lập sẵn trên đường thật. Bài vẫn hợp lệ và không kém thì
  gửi luôn.
- Lệch về xe (vị trí, xăng), hoặc bài lập sẵn hỏng vì đường, thì ngày đó bot chơi trực tiếp với
  khoảng một nửa ngân sách. Phần còn lại dùng để lập trước ngày sau từ trạng thái thật. Chỉ khi
  lệch hai ngày liền (`MISS_LIMIT`) bot mới tắt lập trước cho phần còn lại của trận. Ngày lệch vì
  client khác nộp đè thì không tính, vì đó không phải lỗi dự đoán. Trên server giả, client lạ đè
  ngày 4 cùng hai ngày tắc đường: bot vẫn lập trước lại từ ngày 7, cả trận 41 s (trước khi sửa: 129 s).
- Đo trên server giả (bản đồ `f06e9b26`, 30 s/ngày): 15,6 s cả trận, so với 260,8 s khi tắt;
  L20 D200, Q không thấp hơn, replay khớp. 15,6 s gần như hết vào một ngày server giả có tắc
  đường.
- Trên server thật (contest `f06e9b26`, 26/11): mỗi ngày server ghi đúng 1 bài, cả trận
  **12 s**, replay khớp. DAU 2 mất 57 s.
- **Mỗi đội chỉ một client nộp bài.** Trận `1addebd7` ngày 25/11 mất 254 s vì có một client khác
  dùng cùng token: server ghi 7 bài ở ngày 0 trong khi bot chỉ gửi 1. Server dùng bài cuối
  cùng nó nhận nên bài của client kia đè bài của bot. Nay bot so `submit_count` của `/state`
  với số lần chính nó POST. Nếu server ghi nhiều hơn, bot in `CẢNH BÁO: server ghi N bài …`
  và ghi `foreign_submits` vào file tổng kết.

Mọi cuộc gọi mạng đều bị chặn cứng bởi hạn của ngày. `timeout` của socket chỉ áp
cho *mỗi lần đọc*, không phải cả cuộc gọi: một server nhả dữ liệu nhỏ giọt từng kéo
dài một request tới **58 giây** dù `--timeout 2`. Quá hạn thì bot bỏ cuộc gọi và
**không gửi nữa** — gửi muộn có thể rơi sang ngày sau và ghi đè bài tốt của ngày đó.

### Kết quả đo gần nhất

Đối chứng trước/sau đợt sửa điểm hẹn và định giá xăng (07/10/2026, Windows): `bench`
trên 9 map × 6 seed (3 map mẫu, `random16`–`random32`, bản đồ trận thật `f06e9b26`),
1.000 vòng ALNS mỗi ngày (`--search-iterations 1000 --role-iterations 100`), đối thủ
2 `search` + `greedy` + `sample`, `--jobs 11`. Cùng map, cùng seed nên so được từng cặp:

| Phiên bản | L | D | Q | bài sai |
|---|---|---|---|---|
| trước (đợt sửa 03/10) | 1002 | 6009 | 10859 | 0 |
| điểm hẹn đi theo điểm dừng | 1003 | 6108 | 11003 | 0 |
| điểm hẹn + `fuel_bonus` 1,0 (hiện tại) | 1003 | 6206 | 11594 | 0 |

Bản hiện tại hơn bản trước ở 38/54 cặp trận, kém 15 (kiểm định dấu p = 0,002); mỗi
trận D +3,65 ± 2,47 và Q +13,6 ± 6,4 (khoảng tin cậy 95%). Lợi dồn vào map thiếu xăng
(`random28`, `random32`, `sample_16`); trên bản đồ trận thật, D đã sát trần 200 nên gần
như không đổi. A/B riêng trên bản đồ đó (20 seed, mẫu số 41 đội): `fuel_bonus` 1,0 và 0,2
mỗi bên hơn 10 trận, ΔD −0,40 ± 0,70, ΔQ +1,0 ± 5,7, tức không khác biệt. Thời gian mỗi
ngày không tăng.

`fuel_bonus` được chọn bằng `ab` (54 trận mỗi lần, hai biến thể chơi cùng trận): 1,0 hơn
0,2 ở 37 trận, kém 14, ΔD +1,50 ± 1,04; 2,0 hơn 0,2 ở 38 trận, kém 15, ΔD +1,93 ± 2,10
nhưng có trận thua 34 D; 1,0 đấu thẳng 2,0 thì ngang nhau (28/4/22, ΔD −0,81 ± 2,45). Luật
xếp D trước Q nên chọn 1,0, mức tăng D ổn định hơn.

Đối chứng trước/sau đợt sửa theo `cases.md` (03/10/2026, Windows): 3 map mẫu × 6 seed,
cùng loại xe (1 xe tiếp ở vị trí trung tâm nhất), `--search-seconds 1`, đối thủ
`greedy` + 2 `sample`, lưu lượng chia cho 4 đội. Tổng của 18 trận:

| Phiên bản | L | D | Q | bài sai |
|---|---|---|---|---|
| trước khi sửa (commit `1f8dbee`) | 322 | 1300 | 1763 | 0 |
| sau khi sửa | 324 | 1298 | 1771 | 0 |

Chênh lệch D và Q nằm trong độ nhiễu giữa các lần chạy (khoảng ±1%). L hơn 2: trên
`sample_24` bản mới lấy đủ 18 chuỗi ở cả 6 seed, đúng tác dụng của việc ưu tiên L.

Số đo cũ hơn, trước đợt sửa: `bench --maps sample_16,sample_24,sample_32 --count 3
--search-seconds 3.0` ra 9 thắng / 0 hoà / 0 thua, 0 bài sai (khi đó xếp hạng chưa
xét thời gian phản hồi và lưu lượng chia cho 10 đội).

---

## 4. Cấu trúc

| Thư mục | Vai trò |
|---|---|
| `hexudon/geometry.py`, `rules.py`, `model.py` | hình học even-r, bảng chi phí, đọc/ghi JSON |
| `hexudon/simulator.py` | **nguồn chân lý về luật**: 5 pha mỗi bước, kiểm hợp lệ (thêm `strict_fuel` cho planner), bài dự phòng `idle_plan`, gộp lệnh chờ `compact_plan` |
| `hexudon/procs.py` | tiến trình con chạy được trên cả Windows lẫn Linux/macOS (nhóm tiến trình, dừng mềm, UTF-8) |
| `hexudon/pathing.py`, `traffic.py` | Dijkstra trọng số đỉnh, dự báo giao thông |
| `hexudon/planner/` | nghiệm, bộ giải mã, hàm mục tiêu, dựng nghiệm, ALNS, vai trò, bộ nhớ trận; `rust_tours.py` gọi bộ giải Rust |
| `rust/tour/` | bộ giải tour bằng Rust (chỉ thư viện chuẩn, tuỳ chọn): team orienteering có phủ chuỗi, LNS + ủ mô phỏng |
| `hexudon/client/` | HTTP, sandbox, vòng đời trận |
| `hexudon/offline/` | bot đối thủ, runner nhiều đội, sinh map, server luyện tập, benchmark |
| `tests/` | kiểm thử, gồm golden test tái hiện ví dụ từng bước của ban tổ chức |

---

## 5. Ngày thi

### Trước giờ thi

1. Nếu định đổi mật khẩu trên website thì đổi **trước**, rồi mới lấy token (icon 🔑).
2. Dán token vào `TOKEN=` trong `.env`.
3. `python -m unittest discover -s tests` — phải xanh toàn bộ.
4. `python -m hexudon api-check` — phải in "đủ endpoint"; báo THIẾU hoặc LỆCH thì sửa client trước khi thi.
5. `python -m hexudon games --live` — thấy trận thi, ID trận và các câu.

### Trong giờ thi

```bash
python -m hexudon web --live --match <ID hoặc tên trận BTC công bố> --follow
```

Bot tự chờ cửa sổ chọn xe, chọn vai trò, chơi từng ngày và bắt mọi câu của trận kể cả
câu chạy gối đầu. Trận chưa được công bố thì bot hỏi lại mỗi 15 s cho tới khi thấy, nên
chạy lệnh sớm trước giờ cũng được. Ctrl-C **một lần** chỉ làm bot thôi bắt câu mới, các
câu đang chơi vẫn chơi nốt; Ctrl-C lần hai mới dừng hết. Trên Windows cũng vậy: mỗi bot
con chạy trong nhóm tiến trình riêng (`CREATE_NEW_PROCESS_GROUP`), nên Ctrl-C ở terminal
chỉ tới tiến trình cha. Nếu manager API lỗi, chạy từng câu bằng
`python -m hexudon web --live --game <ID câu>`.

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
| Bài bị từ chối | gửi bài dự phòng: xe nào đang ở ô đường thì bước sang ô đồng bằng kề bên rồi đứng yên (không để lại vết cả ngày trên đường); vẫn bị từ chối thì gửi bài đứng yên thuần (luôn hợp lệ) |
| Bị từ chối bằng HTTP 200 kèm `reason: E_*` | vẫn hiểu là bị từ chối, không phải thành công |
| Server đòi kiểu bọc JSON khác | tự thử `[...]`, `{"actions": ...}`, `{"types": ...}` rồi nhớ kiểu đúng |
| Server đánh số ngày từ 1 | nhận ra qua lỗi số bước, tự lùi chỉ số ngày cho cả trận — chỉ khi server không báo số bước của ngày; server web báo `steps_today` nên bot dùng luôn số đó, không đoán (theo Formats, ngày đầu có `day` = 0) |
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
python -m unittest discover -s tests      # phải ra: Ran 343 tests ... OK
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
       --match 1 --follow --token-env TEST_DAU_01
python -m hexudon web-bench --url http://127.0.0.1:8099 --env experiments/local.env --match 2
```

Tập sự cố: thêm `--faults 429,5xx,401` khi mở server giả. Với `401`, sau `--revoke-after`
giây (mặc định 60) server thu hồi token giữa trận và ghi token mới vào
`experiments/local.env` — bot phải tự đọc lại và chơi tiếp.

### 6.3. Test trên server thật bằng token test

Không cần `--live`: mặc định bot dùng `TEST_DAU_01` (đội DAU 1).

| Mục đích | Lệnh | Thời gian |
|---|---|---|
| Xem trận, câu, trạng thái | `python -m hexudon games` | vài giây |
| Practice nhanh, kiểm kết nối | `python -m hexudon web --match "Practice Match" --question Q01 --search-seconds 3 --role-seconds 3` | ~1 phút |
| Practice đúng nhịp thi thật | `python -m hexudon web --match "Practice Match" --question Q02` | ~5–6 phút |
| **Thi thử có giờ — giống ngày thi nhất** | `python -m hexudon web --match "Mock Contest" --follow --token-env TEST_DAU_01` | tới khi dừng |
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
- `TEST_DAU_01` (mặc định) trùng `TOKEN`: test bằng nó là chơi dưới tên đội DAU 1, nên bot
  luôn in cảnh báo `trùng TOKEN thi thật` — đó là bình thường. Practice được
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

Phần đối chiếu với `cases.md` (thêm ngày 03/10/2026):

- **`tests/test_simulator_rules.py`** — kiểm xăng theo hai cách hiểu (ví dụ chính thức
  và câu chữ của đề), gộp lệnh chờ, bài dự phòng không đứng cả ngày trên ô đường.
- **`tests/test_submit_policy.py`** — chính sách gửi lại (tối đa 3 lần, chỉ gửi khi
  đáng), bộ nhớ trận theo bài server đã nhận, số bước theo `steps_today` của server.
- **`tests/test_offline_rules.py`** — xếp hạng có thời gian phản hồi, mọi đội thấy
  cùng ảnh vị trí đầu ngày, map ngẫu nhiên phủ đủ phạm vi tham số của đề.

Thêm ngày 07/10/2026:

- **`tests/test_meets_and_budget.py`** — điểm hẹn tiếp nhiên liệu đi theo đúng điểm
  dừng khi ALNS phá và chèn (chạy qua mọi toán tử phá), ngân sách theo số vòng cho kết
  quả tất định, `bench --jobs` ra đúng số như chạy tuần tự, `ab` chạy lại ra cùng kết quả.

---

## 7. Web trực quan hoá

```bash
python -m hexudon viz --open        # mở http://127.0.0.1:8765
```

- **Offline (n bot):** chọn map, thêm bot (`search` kèm số giây mỗi ngày, `greedy`, `sample`,
  `wait`), bấm ▶ Chạy. Trọng tài là simulator của repo; các bot chỉ ảnh hưởng nhau qua giao thông.
- **Server giả:** bấm "Mở server giả" (hoặc tự chạy `python -m hexudon serve --web`), "Tải danh sách
  câu", chọn câu và token, ▶ Chạy.
- **Server thật:** chỉ Practice và Mock Contest, chỉ token `TEST_*`, không có `--live`. `TEST_DAU_01`
  trùng `TOKEN`, nên bài web gửi mang tên đội DAU 1. **Giờ thi thật vẫn dùng
  `python -m hexudon web --live --match … --follow`, không dùng web.**
- **Xem:** bản đồ (đường vàng/đỏ là đông/tắc, nét đứt là lộ trình bot đang nghĩ), bảng điểm, khung
  Suy luận (vai trò, biểu đồ ALNS, các lần gửi bài, dự báo giao thông), nhật ký.
- **Tua:** thanh ngày/bước và nút ▶ phát; "● TRỰC TIẾP" để bám trận đang chạy; "Trận cũ" để mở lại.
  Dữ liệu nằm ở `replays/viz/<trận>/`.
- Bot `web` hoặc `arena` chạy từ dòng lệnh cũng ghi được sự kiện bằng `--events <file.jsonl>`; file này
  là JSONL thuần (đọc lại bằng `hexudon.viz.events.read_events`) và **không** hiện trong "Trận cũ" — mục
  đó chỉ liệt kê các trận mở từ chính web.
- **Dừng:** bấm một lần để bot dừng (Ctrl-C; trên Windows là Ctrl-Break gửi tới nhóm tiến trình của bot),
  lần hai để dừng hẳn; tắt web (Ctrl-C hoặc đóng terminal) cũng dừng mọi bot web đã mở.

---

## 8. Việc còn lại

Đợt rà soát theo `cases.md` (03/10/2026) đã sửa ba trong bốn điểm từng ghi ở đây: ALNS
nay bắt đầu từ bài an toàn, `V_fuel` xét đúng ô đang đứng, và dải chấp nhận của ALNS
là 0,25 B cố định. Điểm thứ tư (kế hoạch tiếp nhiên liệu rụng dần trong lúc tìm kiếm)
được sửa ngày 07/10/2026: điểm hẹn và lệnh đợi nay đi theo điểm dừng khi phá
(`_remove_stops`) và khi chèn (`InsertionState.apply`), thay vì bị xoá cả tour. Còn lại:

1. Đối thủ nội bộ của `serve` (server kiểu PTIT) chỉ lập kế hoạch lúc chốt ngày, nên
   luôn gửi bài an toàn chứ không tìm kiếm. Chỉ ảnh hưởng server luyện tập.
2. Hai điểm luật mà đề chưa nói rõ, simulator đang làm theo ví dụ chính thức của BTC
   Nhật: xe đứng sẵn trên spot đầu ngày vẫn thu ở bước 1, và xe được nạp giữa lúc đang
   đi vẫn hợp lệ. Lần đầu chơi practice trên server thật có xe như vậy, xem dòng đối
   chiếu replay: `khớp hoàn toàn` nghĩa là server cũng làm như ví dụ.
3. Lộ trình tiếp theo (R3–R7) và số liệu giải đấu giả lập với 10 kiểu đội khác nằm ở
   báo cáo `de-xuat-chien-thuat/phan-tich-va-de-xuat-chien-thuat.md` (thư mục gốc của
   dự án, ngoài repo này). Ngày 16/11/2026, practice Q1 và một câu Mock Contest trên
   server thật đều khớp replay 10/10 ngày.
