"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """TODO: append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.flush()
    print(json.dumps(record, ensure_ascii=False))
    return record


def state_of(region: str) -> dict:
    """Đọc trạng thái hiện tại của một region qua API serving."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=3.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """TODO: 5 bước ở trên, đúng thứ tự."""
    if target not in URL:
        raise ValueError(f"target không hợp lệ: {target}")
    if wait <= 0:
        raise ValueError("wait phải > 0")

    result = {"ok": False, "target": target, "backend": backend, "cutover": False}

    # 1. Verify that the target API can be inspected. Empty state is acceptable here;
    # restoring the snapshot is precisely what fills it.
    try:
        initial_state = state_of(target)
        emit(step="1_verify_target", region=target, ok=True, state=initial_state)
    except (httpx.HTTPError, ValueError) as exc:
        emit(step="1_verify_target", region=target, ok=False,
             error=f"{type(exc).__name__}: {exc}")
        result["error"] = f"target verification failed: {exc}"
        return result

    # 2. Restore vectors and weights, then compare primary and restored DBs for RPO.
    try:
        manifest = snapshot.get(target, backend)
        rpo = snapshot.rpo(
            pathlib.Path("state/region-a/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        restored = {**manifest, **rpo}
        emit(step="2_restore_snapshot", region=target, backend=backend,
             rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"),
             embed_model_version=manifest.get("embed_model_version"),
             snapshot_at=manifest.get("snapshot_at"), restored_at=manifest.get("restored_at"))
    except (Exception, SystemExit) as exc:
        emit(step="2_restore_snapshot", region=target, backend=backend, ok=False,
             error=f"{type(exc).__name__}: {exc}")
        result["error"] = f"snapshot restore failed: {exc}"
        return result
    result["restore"] = restored

    # 3. Request a full pool. The serving process observes this state transition and
    # starts its warm-up timer.
    try:
        pathlib.Path(f"state/region-{target}/pool_state").write_text("full\n", encoding="utf-8")
        emit(step="3_scale_pool", region=target, pool_state="full", ok=True)
    except OSError as exc:
        emit(step="3_scale_pool", region=target, ok=False, error=str(exc))
        result["error"] = f"pool scale failed: {exc}"
        return result

    # 4. Do not cut over until readiness returns HTTP 200. Poll with bounded requests
    # and abort cleanly when the caller's deadline expires.
    started = time.monotonic()
    deadline = started + wait
    attempts = 0
    last_reason = "readiness not checked"
    ready_body = None
    while time.monotonic() < deadline:
        attempts += 1
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, max(0.1, deadline-time.monotonic())))
            if response.status_code == 200:
                ready_body = response.json()
                waited = round(time.monotonic() - started, 3)
                emit(step="4_wait_ready", region=target, ok=True, waited_s=waited,
                     attempts=attempts, ready=ready_body)
                break
            try:
                body = response.json()
                last_reason = ",".join(body.get("reasons", [])) or f"HTTP {response.status_code}"
            except ValueError:
                last_reason = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    else:
        waited = round(time.monotonic() - started, 3)
        emit(step="4_wait_ready", region=target, ok=False, waited_s=waited,
             attempts=attempts, reason=last_reason)
        result["error"] = f"target did not become ready within {wait}s: {last_reason}"
        return result

    result["ready"] = ready_body
    try:
        result["target_state"] = state_of(target)
    except (httpx.HTTPError, ValueError) as exc:
        # The target passed readiness, but the runbook still needs an independently
        # readable replica state before it can safely authorize the cutover.
        result["error"] = f"could not verify restored target state: {exc}"
        return result

    # 5. Mutate the edge pointer only after the target is ready.
    try:
        active_file = pathlib.Path("edge/active_region")
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(f"{target}\n", encoding="utf-8")
        event = emit(step="5_dns_cutover", region=target, ok=True,
                     active_region=target, ttl_seconds=5.0)
        result.update(ok=True, cutover=True, cutover_ts=event["ts"])
    except OSError as exc:
        emit(step="5_dns_cutover", region=target, ok=False, error=str(exc))
        result["error"] = f"DNS cutover failed: {exc}"
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
