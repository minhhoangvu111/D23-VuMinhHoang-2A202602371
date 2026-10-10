# Runbook — Region chính down (Lab 23)

**Phạm vi:** local two-region drill, primary A / target B, filesystem snapshots. On-call incident commander (IC) điều phối; failover cần IC xác nhận. Không chuyển về A chỉ vì A process đã sống lại.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` (lặp 3 lần, cách nhau 1s); kiểm tra `curl -i --max-time 2 localhost:8001/readyz` và `curl -i --max-time 2 localhost:8002/healthz` | A không ready 3 lần liên tiếp; B còn alive. B có thể chưa ready vì state rỗng. Nếu cả hai không alive: dừng và mở major incident, không ép failover. | On-call SRE |
| 2 | Mở incident, ghi mốc operator và xin quyết định | `python3 dr/runbook.py --primary a --target b --backend fs` | Có `thong_bao_incident` trong `reports/runbook-run.jsonl` với `outage_ts`, `incident_started_ts`; IC nhập `y` để cho phép. Lệnh này điều phối tiếp các bước 3–7; không chạy failover lần hai. | On-call SRE + IC |
| 3 | Restore snapshot, scale pool, chờ target ready | Được runner gọi đúng một lần; kiểm tra `tail -n 5 reports/failover-events.jsonl` | Có đủ năm bước theo thứ tự: `1_verify_target`, `2_restore_snapshot`, `3_scale_pool`, `4_wait_ready` với `ok=true`, `5_dns_cutover`. Nếu bước 4 timeout, không cutover. | DR automation; IC theo dõi |
| 4 | Xác minh replica | `curl -s localhost:8002/v1/state` và `curl -i localhost:8002/readyz` | `count > 0`, `weights=true`, `pool_state=full`, `/readyz` HTTP 200; model version phù hợp snapshot. Nếu không đạt, giữ traffic ở trạng thái an toàn và báo IC. | On-call SRE |
| 5 | Xác minh DNS/LB cutover | `curl -s localhost:8080/edge/state` | `active_region=b`; sau tối đa TTL 5 giây, request edge đi tới B. Chỉ runner failover ghi pointer. | On-call SRE |
| 6 | Kiểm tra golden signals | `grep 'verify_golden_signals' reports/runbook-run.jsonl` | 10 request thật thành công, error rate = 0%, p95 < 15.000 ms. Nếu fail, IC đánh giá sự cố; không tự động failback. | On-call SRE + IC |
| 7 | Đo RTO/RPO, ghi postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid=true`, `rto_verdict=PASS`, có RPO seconds và docs lost; điền số và evidence `path:line` vào `reports/rto-evidence.md`, `reports/postmortem.md`. | Incident commander |

## Rollback / failback về Region A

Chỉ fail back khi (1) A `/readyz` trả HTTP 200 ổn định ít nhất 3 lần liên tiếp, (2) vectors, weights và embedding model version đã đồng bộ/đối chiếu với B, (3) IC chấp nhận rủi ro và đã thông báo incident team. Nếu B đang lỗi nhưng A chưa ready, không đổi pointer mù; giữ incident mở và IC điều phối khôi phục.

IC là người duy nhất phê duyệt failback. Sau phê duyệt, thực hiện bằng change có giám sát (không auto-flip): cập nhật pointer, chờ TTL, rồi xác minh `curl -s localhost:8080/edge/state` và request infer thành công từ A. Ghi timestamp, người duyệt và kết quả vào incident/postmortem. Không bật full-auto failback khi chưa có circuit breaker.
