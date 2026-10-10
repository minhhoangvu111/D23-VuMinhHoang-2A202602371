# Postmortem — DR Drill Lab 23

**Phạm vi:** Drill 2 ngày 2026-10-10, Region A bị `netblock`, failover phục hồi sang B. Phân tích tập trung vào hành vi hệ thống và quy trình, không quy lỗi cho cá nhân.

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-10 11:38:32.290 | Chaos bắt đầu outage Region A; B còn alive, không ép outage kép | `chaos/chaos-events.jsonl:3` |
| 2026-10-10 11:38:35.808 | Request đầu tiên có timestamp sau outage lỗi; các request bắt đầu trước outage được tính theo timestamp start của loadgen | `reports/drill-2-withdr.jsonl:5` |
| 2026-10-10 11:38:57.497 | Health checker đổi A sang `UNHEALTHY` sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| 2026-10-10 11:39:23.961 | Runbook xác nhận outage, ghi mốc incident/operator và bắt đầu clock trong runbook | `reports/runbook-run.jsonl:1`; `reports/runbook-run.jsonl:2` |
| 2026-10-10 11:39:28.898 | Snapshot restore hoàn tất; RPO 4,02 s / 2 docs | `reports/failover-events.jsonl:2` |
| 2026-10-10 11:39:42.121 | Region B đạt `/readyz`; pool full | `reports/failover-events.jsonl:4` |
| 2026-10-10 11:39:46.538 | Edge cutover sang B sau khi B ready | `reports/failover-events.jsonl:5` |
| 2026-10-10 11:39:47.978 | Loadgen ghi request thành công đầu tiên từ B; incident phục hồi theo định nghĩa RTO | `reports/drill-2-withdr.jsonl:17` |

## 2. RTO/RPO đo được so với mục tiêu — khoảng cách nằm ở đâu?

- **RTO:** mục tiêu 300 s; đo được **75,7 s**; gap tới giới hạn mục tiêu là **224,3 s headroom**. Kết quả `PASS`, log hợp lệ và warnings rỗng: `reports/measure-drill-2.json:20`; `reports/measure-drill-2.json:22`.
- **RPO:** mục tiêu tối đa 300 s; đo **4,02 s**, **2 documents** mất tại thời điểm restore: `reports/failover-events.jsonl:2`. Chênh lệch so với giới hạn thời gian là 295,98 s; số document vẫn cần được đánh giá theo mức ảnh hưởng dữ liệu.
- **Khoảng thời gian lớn nhất trong breakdown:** incident/runbook confirmation + restore đến scale pool, **31,4 s**. Đây là khoảng detector→pool scale; bao gồm độ trễ bắt đầu/xác nhận runbook và restore. Log không đóng dấu lúc bắt đầu thao tác copy snapshot nên không thể quy toàn bộ 31,4 s cho snapshot I/O. Runbook incident timestamp là +51,7 s trong khi detector là +25,2 s, để lại khoảng 26,5 s trước khi runbook bắt đầu: `reports/health-events.jsonl:2`, `reports/runbook-run.jsonl:2`.
- Pool warm-up tới readiness mất **13,219 s**; thêm khoảng **4,418 s** từ readiness đến cutover. TTL/cutover đến request thành công đầu tiên là **1,440 s**: `reports/failover-events.jsonl:3`; `reports/failover-events.jsonl:4`; `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:17`.

## 3. Root cause — 5 whys

1. **Vì sao client gặp lỗi trong 75,7 s?** A bị pause và edge tiếp tục gửi request vào A cho đến khi runbook restore B, xác nhận readiness rồi cutover. Loadgen ghi 11 request lỗi trước lần thành công đầu tiên từ B: `reports/measure-drill-2.json:20`; `reports/measure-drill-2.json:22`.
2. **Vì sao còn khoảng 26,5 s sau khi detector phát hiện trước khi runbook ghi incident?** Health checker chỉ ghi transition; nó không tự gọi hoặc page runbook. Khoảng cách timestamp quan sát được giữa detector (+25,2 s) và incident (+51,7 s) là 26,5 s: `reports/health-events.jsonl:2`, `reports/runbook-run.jsonl:2`.
3. **Vì sao detector không kích hoạt hành động có kiểm soát?** Thành phần health-check và runbook tách rời; runbook được thiết kế bán tự động để giữ điểm xác nhận trước failover và chống flapping.
4. **Vì sao quy trình không giảm khoảng chờ mà vẫn giữ xác nhận?** Chưa có tích hợp alert/incident có SLA, circuit breaker và cơ chế xác nhận có thời hạn; operator phải khởi chạy runbook riêng.
5. **Vì sao còn thiếu cơ chế đó?** Lab chỉ mô phỏng endpoint và log cục bộ, chưa có lớp paging/orchestration production. Cần kiểm tra cả latency nhận alert lẫn quyết định failover trong game day, thay vì chỉ đo thời gian code chạy sau khi được gọi.

## 4. Action items

| # | Action | Owner | Deadline | Tác động RTO/RPO dự kiến |
|---|---|---|---|---|
| 1 | Nối transition `UNHEALTHY` tới paging/incident launcher; giữ IC approval hoặc policy gate; đặt mục tiêu detector→incident ≤10 s và log đủ timestamp. Kiểm chứng qua drill tiếp theo. | SRE on-call / DR automation owner | 2026-10-17 | Có thể giảm phần chờ quan sát được khoảng 26,5 s xuống ≤10 s, tiết kiệm tối đa khoảng 16,5 s nếu các pha sau không đổi; đây là mục tiêu cần đo lại, không phải kết quả đã đạt. |
| 2 | Thêm timestamp bắt đầu/kết thúc cho snapshot restore, scale request, ready và cutover; xuất breakdown các khoảng không chồng lấn từ một script. | DR tooling owner | 2026-10-17 | Không hứa giảm RTO ngay; loại bỏ vùng chưa phân tách trong breakdown để xác định chính xác bottleneck ở drill tới. |
| 3 | Đánh giá chu kỳ replication 30 s; nếu chấp nhận thêm snapshot I/O, thử chu kỳ 15 s và đo RPO cùng tài nguyên trước khi áp dụng. | Data platform owner | 2026-10-24 | Giới hạn lag lý thuyết giảm từ chu kỳ 30 s xuống 15 s; mất document thực tế phụ thuộc tốc độ ingest và thời điểm restore, cần đo lại. Không làm giảm RTO trực tiếp. |

## 5. Ba câu hỏi bắt buộc

1. **Detection floor là bao nhiêu và chiếm bao nhiêu RTO?** `interval × threshold = 5 s × 3 = 15 s`. Floor chiếm **19,8%** RTO đo được (`15 / 75,7`). Detector thực tế ghi nhận ở +25,2 s, tương đương khoảng 33,3% RTO, do lịch poll/độ trễ quan sát: `reports/health-events.jsonl:2`, `reports/measure-drill-2.json:20`; `reports/measure-drill-2.json:22`.
2. **Nếu hạ interval xuống 1 s thì giảm bao nhiêu và trả giá gì?** Với threshold giữ ở 3, floor lý thuyết từ 15 s xuống 3 s, nên phần floor có thể giảm tối đa **12 s**; nếu mọi phần khác giữ nguyên thì RTO ước tính theo phép trừ là khoảng **63,7 s**. Đây không phải kết quả đo. Poll 5 lần thường xuyên hơn, tăng tải probe; một chuỗi lỗi mạng ngắn dễ chạm ngưỡng hơn và tạo flapping/false alert nếu timeout hoặc jitter không được hiệu chỉnh.
3. **`docs_lost=2` có nghĩa gì với khách hàng?** Hai document đã được ingest vào A sau mốc dữ liệu của snapshot cuối cùng nhưng không có trong bản B được restore. Nếu đó là ticket/hội thoại khách hàng, thông tin có thể mất khỏi truy vấn sau failover, gây câu trả lời thiếu ngữ cảnh hoặc cần replay từ nguồn gốc. Con số này đếm bản ghi mất trong lần restore, không tự cho biết danh tính/giá trị nghiệp vụ của chúng; cần đối chiếu ID và quy trình replay.
