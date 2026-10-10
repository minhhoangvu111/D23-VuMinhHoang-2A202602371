"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """TODO: ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
              "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.flush()
    print(json.dumps(record, ensure_ascii=False))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """TODO: auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        answer = input(f"{msg} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _alive(region: str, timeout: float = 1.5) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{URL[region]}/healthz", timeout=timeout)
        return response.status_code == 200, f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _outage_timestamp(primary: str):
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == primary:
            events.append(event)
    return max(events, key=lambda event: event.get("ts", 0)).get("ts") if events else None


def _health_detection(primary: str, not_before: float, timeout: float = 45.0):
    """Wait for the health checker to record the primary's thresholded outage."""
    path = pathlib.Path("reports/health-events.jsonl")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (event.get("event") == "state_change"
                        and event.get("to") == "UNHEALTHY"
                        and event.get("region") == primary
                        and event.get("ts", 0) >= not_before):
                    return event
        time.sleep(0.2)
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """TODO: 7 bước ở trên."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary và target phải là hai region khác nhau trong {a,b}")

    started = time.time()
    outage_ts = _outage_timestamp(primary)

    # 1. Require three consecutive readiness failures on the primary and verify the
    # alternate process is still alive before treating this as a regional incident.
    primary_failures = 0
    primary_reasons = []
    target_alive = False
    target_alive_reason = "not checked"
    for attempt in range(1, 4):
        ready, reason = hc.probe(primary, timeout=0.75)
        primary_reasons.append(reason)
        primary_failures = primary_failures + 1 if not ready else 0
        target_alive, target_alive_reason = _alive(target)
        if primary_failures >= 3 or ready:
            break
        time.sleep(0.2)
    outage_confirmed = primary_failures >= 3 and target_alive
    step(1, "xac_nhan_outage", primary=primary, target=target,
         confirmed=outage_confirmed, primary_unready_consecutive=primary_failures,
         primary_reasons=primary_reasons, target_alive=target_alive,
         target_alive_reason=target_alive_reason)
    if not outage_confirmed:
        result = {"ok": False, "error": "outage not confirmed or target is not alive"}
        step(2, "thong_bao_incident", ok=False, outage_ts=outage_ts,
             incident_started_ts=time.time(), reason=result["error"])
        return result

    # Require the configured anti-flap health checker to confirm the same outage
    # before recording the operator alert or cutting over. This keeps t_detect ahead
    # of t_cutover in the drill evidence and avoids racing the threshold policy.
    detection = _health_detection(primary, outage_ts if outage_ts is not None else started)
    if detection is None:
        result = {"ok": False, "error": "health checker did not record primary UNHEALTHY"}
        step(2, "thong_bao_incident", ok=False, outage_ts=outage_ts,
             incident_started_ts=time.time(), health_detection_confirmed=False,
             reason=result["error"])
        return result

    # 2. Record the operator's incident time separately from the outage timestamp.
    incident_ts = time.time()
    notification_delay = round(incident_ts - outage_ts, 3) if outage_ts is not None else None
    step(2, "thong_bao_incident", ok=True, primary=primary, target=target,
         outage_ts=outage_ts, incident_started_ts=incident_ts,
         notification_delay_s=notification_delay,
         health_detected_ts=detection.get("ts"),
         health_detection_delay_s=round(detection["ts"] - outage_ts, 3)
         if outage_ts is not None else None,
         rto_clock_started=True)

    authorized = confirm(auto, f"Fail over traffic from Region {primary.upper()} to {target.upper()}?")
    if not authorized:
        result = {"ok": False, "cancelled": True, "error": "operator declined failover"}
        step(3, "scale_gpu_pool", ok=False, authorized=False, reason=result["error"])
        return result

    # 3. This is the sole failover call. It owns the five ordered sub-steps and logs
    # them to failover-events.jsonl.
    try:
        failover_result = fo.failover(target, backend, wait=60.0)
    except Exception as exc:
        failover_result = {"ok": False, "cutover": False,
                           "error": f"{type(exc).__name__}: {exc}"}
    step(3, "scale_gpu_pool", ok=bool(failover_result.get("ok")),
         failover_result=failover_result)

    # 4. Read only the return value from failover; do not restore or call failover again.
    state = failover_result.get("target_state") or {}
    vectors = state.get("count")
    weights = state.get("weights")
    replica_verified = bool(failover_result.get("ok") and vectors and weights)
    step(4, "verify_state_replica", ok=replica_verified, region=target,
         vector_count=vectors, weights=weights, state=state)

    # 5. Read the cutover result already returned by failover.
    cutover_ok = bool(failover_result.get("cutover"))
    step(5, "dns_cutover", ok=cutover_ok, region=target,
         cutover_ts=failover_result.get("cutover_ts"),
         active_region=target if cutover_ok else None)

    # 6. Send ten real inference requests directly to the target and summarize its
    # golden signals. Keep a finite timeout so an unhealthy target cannot hang the runbook.
    latencies = []
    errors = []
    if cutover_ok and replica_verified:
        with httpx.Client(timeout=15.0) as client:
            for index in range(10):
                begin = time.perf_counter()
                try:
                    response = client.get(f"{URL[target]}/v1/infer",
                                          params={"q": f"golden signal {index}"})
                    elapsed_ms = (time.perf_counter() - begin) * 1000
                    latencies.append(elapsed_ms)
                    if response.status_code != 200:
                        errors.append({"request": index + 1, "status": response.status_code})
                except httpx.HTTPError as exc:
                    latencies.append((time.perf_counter() - begin) * 1000)
                    errors.append({"request": index + 1, "error": type(exc).__name__})
    else:
        errors.append({"reason": "skipped because restore/readiness/cutover did not succeed"})
    p95_ms = None
    error_rate = 1.0 if not latencies else len(errors) / len(latencies)
    if latencies:
        ordered = sorted(latencies)
        p95_ms = round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 1)
    golden_ok = (len(latencies) == 10 and error_rate == 0.0
                 and p95_ms is not None and p95_ms < 15000)
    step(6, "verify_golden_signals", ok=golden_ok, region=target,
         requests=len(latencies), p95_ms=p95_ms, error_count=len(errors),
         error_rate=round(error_rate, 3), p95_budget_ms=15000, errors=errors)

    # 7. Record elapsed time and the reproducible RTO measurement command.
    finished = time.time()
    elapsed_s = round(finished - (outage_ts if outage_ts is not None else started), 3)
    measurement_command = (
        "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
        "--target-rto 300"
    )
    ok = bool(failover_result.get("ok") and replica_verified and cutover_ok and golden_ok)
    result = {"ok": ok, "primary": primary, "target": target,
              "outage_ts": outage_ts, "incident_started_ts": incident_ts,
              "elapsed_s": elapsed_s, "failover": failover_result,
              "golden_signals": {"p95_ms": p95_ms, "error_rate": error_rate,
                                 "requests": len(latencies)}}
    step(7, "post_incident", ok=ok, elapsed_s=elapsed_s,
         rto_measure_command=measurement_command,
         rto_target_s=300, rpo=failover_result.get("restore"), result=result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
