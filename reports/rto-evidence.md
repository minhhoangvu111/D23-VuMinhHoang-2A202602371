# RTO/RPO Evidence — Lab 23

Các con số dưới đây được đo từ logs của hai drill. Drill 1 giữ nguyên làm baseline; Drill 2 là lần có DR dùng để chấm RTO/RPO.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| `t_outage` | 2026-10-10 11:22:51 UTC | Chaos event `kill`, Region A | `chaos/chaos-events.jsonl:1` |
| Request lỗi đầu tiên theo đồng hồ đo | +4.6 s | Request đầu tiên có timestamp sau outage và `ok:false` | `reports/drill-1-nodr.jsonl:4` |
| Request thành công sau outage | Không có | Không có `ok:true` sau `t_outage`; 5 request lỗi | `reports/drill-1-nodr.jsonl:4`; `reports/measure-drill-1.json:28` |
| Verdict RTO | `NO_RECOVERY` (`rto_measured_s=null`) | Kết quả công cụ đo | `reports/measure-drill-1.json:25` |

Ghi chú: request ở dòng 3 bắt đầu trước `t_outage` dù hoàn tất bằng timeout sau đó; `tools/measure_rto.py` dùng timestamp bắt đầu request nên mốc fail đầu tiên tính sau outage là dòng 4. Kết quả baseline có `valid=true`, `requests_failed=5`, `rto_verdict=NO_RECOVERY`.

## 2. Drill 2 — có DR

| Mốc | Thời gian từ `t_outage` | Evidence |
|---|---:|---|
| Outage A, `netblock` (mốc 0) | 0 s | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +3.5 s | `reports/drill-2-withdr.jsonl:5` |
| Health checker phát hiện A `UNHEALTHY` | +25.2 s | `reports/health-events.jsonl:2` |
| Snapshot restore hoàn tất | +56.6 s | `reports/failover-events.jsonl:2` (`restored_at`) |
| B ready | +69.8 s | `reports/failover-events.jsonl:4` (`4_wait_ready`, `waited_s=13.219`) |
| DNS cutover sang B | +74.2 s | `reports/failover-events.jsonl:5` |
| Request thành công đầu tiên từ B / RTO | **+75.7 s** | `reports/drill-2-withdr.jsonl:17`; `reports/measure-drill-2.json:20`; `reports/measure-drill-2.json:22` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---:|---:|---|
| RTO — inference API | **75.7 s** | 300 s | PASS; còn 224.3 s headroom |
| RPO — vector DB tại restore | **4.02 s / 2 documents** | ≤300 s | Đạt mục tiêu theo thời gian và đã lượng hóa documents lost |

Kết quả đo đầy đủ: `reports/measure-drill-2.json:20`; `reports/measure-drill-2.json:22`. Nó báo `valid=true`, `invalid_reasons=[]`, `warnings=[]`, `recovered_by_region=b`, `requests_failed=11`, `rto_verdict=PASS`.

### RTO breakdown cộng đúng tổng đo

Các mốc gốc: outage `chaos/chaos-events.jsonl:3`; detector `reports/health-events.jsonl:2`; restore/scale/ready/cutover `reports/failover-events.jsonl:2`; `reports/failover-events.jsonl:3`; `reports/failover-events.jsonl:4`; `reports/failover-events.jsonl:5`; request phục hồi `reports/drill-2-withdr.jsonl:17`. Tổng các khoảng chưa làm tròn là 75.687 s, làm tròn một chữ số thành 75.7 s.

| Thành phần | Giây | Cách tính / nguồn | Cách có thể giảm |
|---|---:|---|---|
| Health-check detection | **25.21** (floor lý thuyết **15.00**) | Thực đo `t_detect − t_outage` từ `chaos/chaos-events.jsonl:3` và `reports/health-events.jsonl:2`; cấu hình 5 s × 3 tại `reports/health-events.jsonl:2` | Poll nhanh hơn làm giảm floor nhưng tăng probe load và nguy cơ flapping; cân nhắc timeout/debounce theo SLO. |
| Incident/runbook confirmation + restore đến lúc scale pool | **31.41** | `t_scale − t_detect` từ `reports/health-events.jsonl:2` và `reports/failover-events.jsonl:3`. Khoảng này bao gồm độ trễ khởi chạy/xác nhận runbook và restore; log không có mốc bắt đầu riêng của thao tác copy snapshot nên không thể tách riêng thời lượng copy. Restore hoàn tất ghi ở `reports/failover-events.jsonl:2`. | Tích hợp alert→runbook có kiểm soát và đặt SLA phản hồi; đo riêng thời gian restore ở lần drill sau. |
| GPU pool warm-up và readiness-to-cutover | **17.63** | `t_cutover − t_scale` từ `reports/failover-events.jsonl:3`; `reports/failover-events.jsonl:5`; trong đó readiness chờ **13.219 s** ở dòng 4 và khoảng B ready→cutover là 4.418 s | Pre-warm pool khi chi phí cho phép; giảm thời gian xác minh/cutover bằng probe tự động có giới hạn. |
| DNS/LB TTL tới request thành công đầu tiên | **1.44** | `t_first_success − t_cutover` từ `reports/failover-events.jsonl:5` và `reports/drill-2-withdr.jsonl:17` | Giảm TTL nếu edge/cache và hạ tầng thật đáp ứng; giữ kiểm tra readiness trước cutover. |
| **Tổng RTO** | **75.69** (75.7 khi làm tròn 0.1 s) | Tổng các khoảng liền kề từ outage đến timestamp request thành công đầu tiên; đối chiếu `reports/measure-drill-2.json:20`; `reports/measure-drill-2.json:22` | — |

Bảng giữ riêng floor 15 s theo rubric và dùng thời gian detector thực tế 25.21 s trong phép cộng RTO. Các bucket incident/runbook và warm-up/readiness-to-cutover nêu rõ khoảng vận hành đã quan sát để bốn khoảng liền kề cộng khớp timestamp đầu-cuối; không diễn giải 31.41 s là thời lượng copy snapshot thuần túy.

### RPO và replication evidence

- Snapshot đầu tiên được ghi trước outage; manifest ghi source A và model version `embed-model=vi-e5-base@v3`: `reports/replication.jsonl:1`, `state/_replica/dr-artifacts/MANIFEST.json:1`; `state/_replica/dr-artifacts/MANIFEST.json:5`.
- Các snapshot theo chu kỳ 30 s trước/sát thời điểm restore: `reports/replication.jsonl:1`; `reports/replication.jsonl:4`. Snapshot được restore có `snapshot_at=1791632365.0113997`; failover log tính `rpo_seconds=4.02`, `docs_lost=2`: `reports/failover-events.jsonl:2`.
- Không có double outage: chaos event Drill 2 ghi `other_alive=true`, `forced_both=false`: `chaos/chaos-events.jsonl:3`.
- Runbook xác minh 10 request thành công, p95 30 ms, error rate 0%: `reports/runbook-run.jsonl:6`.
