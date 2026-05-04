import os
import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import config as _config  # Force startup-time required env validation.
from ai_pipeline import get_model_readiness
from database import db_lock, get_conn
from edge_auth import (
    normalize_case_id,
    resolve_image_path_safe,
    template_with_auth_cookie,
    verify_edge_access as verify_edge_access_impl,
)
from process_lock import acquire_single_process_lock
from review_service import submit_review_workflow
from worker_runtime import stop_event, sync_once, sync_worker, reconcile_worker, line_retry_worker

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
NOTIFICATION_SOUND_PATH = os.path.join(
    BASE_DIR, "static", "sounds", "new_case_notification.mp3"
)
EDGE_AUTH_TOKEN = os.getenv("EDGE_AUTH_TOKEN", "").strip()
EDGE_ALLOWED_IPS = {
    ip.strip() for ip in os.getenv("EDGE_ALLOWED_IPS", "").split(",") if ip.strip()
}
EDGE_COOKIE_SECURE = os.getenv("EDGE_COOKIE_SECURE", "1").strip() == "1"
EDGE_COOKIE_MAX_AGE_SEC = int(os.getenv("EDGE_COOKIE_MAX_AGE_SEC", "28800"))
RUN_BACKGROUND_WORKERS = os.getenv("RUN_BACKGROUND_WORKERS", "1").strip() == "1"
BACKGROUND_LOCK_PATH = os.path.join(BASE_DIR, ".edge_workers.lock")
SYNC_TRIGGER_COOLDOWN_SEC = float(os.getenv("SYNC_TRIGGER_COOLDOWN_SEC", "5"))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
_sync_trigger_lock = threading.Lock()
_sync_trigger_running = False
_sync_trigger_last_started_at = 0.0


# ============================
# SECURITY: Edge Authentication
# ============================
def _normalize_case_id(case_id: str) -> str:
    return normalize_case_id(case_id)

def _resolve_image_path_safe(image_filename: str) -> str | None:
    return resolve_image_path_safe(image_filename, UPLOAD_FOLDER)

def _template_with_auth_cookie(request: Request, template_name: str):
    return template_with_auth_cookie(
        request,
        template_name,
        templates=templates,
        edge_auth_token=EDGE_AUTH_TOKEN,
        edge_cookie_secure=EDGE_COOKIE_SECURE,
        edge_cookie_max_age_sec=EDGE_COOKIE_MAX_AGE_SEC,
    )

def verify_edge_access(request: Request):
    return verify_edge_access_impl(
        request,
        edge_allowed_ips=EDGE_ALLOWED_IPS,
        edge_auth_token=EDGE_AUTH_TOKEN,
        logger=logger,
    )

# ============================
# FastAPI (lifespan + UI/API)
# ============================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sync_trigger_running, _sync_trigger_last_started_at
    stop_event.clear()
    lock_fd = None
    with _sync_trigger_lock:
        _sync_trigger_running = False
        _sync_trigger_last_started_at = 0.0

    if RUN_BACKGROUND_WORKERS:
        lock_fd = acquire_single_process_lock(BACKGROUND_LOCK_PATH)
        if lock_fd is not None:
            t1 = threading.Thread(target=sync_worker, daemon=True)
            t2 = threading.Thread(target=reconcile_worker, daemon=True)
            t3 = threading.Thread(target=line_retry_worker, daemon=True)
            t1.start()
            t2.start()
            t3.start()
            logger.info("Lifespan startup complete (sync/reconcile/line-retry threads started)")
        else:
            logger.warning("[WORKER] Skip background workers startup: lock already held by another process")
    else:
        logger.info("[WORKER] RUN_BACKGROUND_WORKERS=0, skip starting background workers")
    try:
        yield
    finally:
        stop_event.set()
        logger.info("Lifespan shutdown: stopping background threads...")
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass
            try:
                if os.path.exists(BACKGROUND_LOCK_PATH):
                    os.remove(BACKGROUND_LOCK_PATH)
            except Exception:
                pass

app = FastAPI(lifespan=lifespan)

# ============================
# UI Routes (AUTHENTICATED)
# ============================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, _auth: bool = Depends(verify_edge_access)):
    return _template_with_auth_cookie(request, "index.html")

@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request, _auth: bool = Depends(verify_edge_access)):
    return _template_with_auth_cookie(request, "review.html")

@app.get("/reviewed", response_class=HTMLResponse)
async def reviewed_page(request: Request, _auth: bool = Depends(verify_edge_access)):
    return _template_with_auth_cookie(request, "reviewed.html")

@app.get("/review/detail", response_class=HTMLResponse)
async def review_detail_page(request: Request, _auth: bool = Depends(verify_edge_access)):
    return _template_with_auth_cookie(request, "review_detail.html")

# ============================
# Health Check
# ============================
@app.get("/health")
async def health_check():
    """Readiness-aware health endpoint for container orchestration."""
    models = get_model_readiness()
    payload = {"service": "edge_private", "models": models}
    if not models.get("ready"):
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", **payload},
        )
    return {"status": "ok", **payload}

# ============================
# API Routes (AUTHENTICATED)
# ============================
@app.get("/api/image/{case_id}")
async def get_image(case_id: str, _auth: bool = Depends(verify_edge_access)):
    """Serve the case image file — with path traversal protection."""
    case_id = _normalize_case_id(case_id)
    with db_lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT image_filename FROM cases WHERE id=?", (case_id,)).fetchone()
        finally:
            conn.close()

    if not row or not row["image_filename"]:
        raise HTTPException(status_code=404, detail="Image not found")

    image_filename = row["image_filename"]
    image_path = _resolve_image_path_safe(image_filename)

    if image_path is None:
        logger.warning(f"[SEC] Path traversal attempt blocked for case_id={case_id}")
        raise HTTPException(status_code=400, detail="Invalid path")

    if not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail="Image file not found")

    return FileResponse(image_path, headers={"Cache-Control": "no-store"})


@app.get("/api/notification/sound")
async def get_notification_sound(_auth: bool = Depends(verify_edge_access)):
    """Serve notification sound for new incoming cases."""
    if not os.path.exists(NOTIFICATION_SOUND_PATH):
        raise HTTPException(status_code=404, detail="Notification sound not found")
    return FileResponse(
        NOTIFICATION_SOUND_PATH,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )

@app.post("/api/sync/trigger")
async def trigger_sync(_auth: bool = Depends(verify_edge_access)):
    """Manually trigger a sync operation."""
    global _sync_trigger_running, _sync_trigger_last_started_at

    now = time.monotonic()
    with _sync_trigger_lock:
        if _sync_trigger_running:
            return JSONResponse(
                status_code=409,
                content={"status": "busy", "message": "Sync is already running"},
            )

        if now - _sync_trigger_last_started_at < max(0.0, SYNC_TRIGGER_COOLDOWN_SEC):
            return JSONResponse(
                status_code=429,
                content={"status": "throttled", "message": "Sync trigger cooling down"},
            )

        _sync_trigger_running = True
        _sync_trigger_last_started_at = now

    def _run_sync_once():
        global _sync_trigger_running
        try:
            sync_once()
        finally:
            with _sync_trigger_lock:
                _sync_trigger_running = False

    try:
        thread = threading.Thread(target=_run_sync_once, daemon=True)
        thread.start()
        return {"status": "ok", "message": "Sync triggered successfully"}
    except Exception as e:
        with _sync_trigger_lock:
            _sync_trigger_running = False
        logger.error(f"[SYNC] Manual trigger failed: {e}")
        raise HTTPException(status_code=500, detail="Sync trigger failed")

@app.get("/api/list_pending")
async def list_pending(_auth: bool = Depends(verify_edge_access)):
    with db_lock:
        conn = get_conn()
        try:
            rows = []
            for r in conn.execute(
                "SELECT id, created_at, status, ai_status, ai_message, ai_level, ai_prob, ai_suggestion, image_filename "
                "FROM cases WHERE status='pending' ORDER BY created_at ASC"
            ):
                image_path = _resolve_image_path_safe(r["image_filename"])
                if not image_path or not os.path.exists(image_path):
                    continue
                rows.append(
                    {
                        "id": r["id"],
                        "created_at": r["created_at"],
                        "status": r["status"],
                        "ai_status": r["ai_status"],
                        "ai_message": r["ai_message"],
                        "ai_level": r["ai_level"],
                        "ai_prob": r["ai_prob"],
                        "ai_suggestion": r["ai_suggestion"],
                    }
                )
        finally:
            conn.close()
    return rows

@app.get("/api/list_reviewed")
async def list_reviewed(_auth: bool = Depends(verify_edge_access)):
    with db_lock:
        conn = get_conn()
        try:
            rows = [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "status": r["status"],
                    "ai_status": r["ai_status"],
                    "ai_message": r["ai_message"],
                    "ai_level": r["ai_level"],
                    "ai_prob": r["ai_prob"],
                    "reviewed_level": r["reviewed_level"],
                    "reviewed_note": r["reviewed_note"],
                    "reviewed_at": r["reviewed_at"],
                    "line_send_status": r["line_send_status"],
                }
                for r in conn.execute(
                    "SELECT id, created_at, status, ai_status, ai_message, ai_level, ai_prob, reviewed_level, "
                    "reviewed_note, reviewed_at, line_send_status "
                    "FROM cases WHERE status='reviewed' ORDER BY reviewed_at DESC"
                )
            ]
        finally:
            conn.close()
    return rows

@app.get("/api/case/{case_id}")
async def case_detail(case_id: str, _auth: bool = Depends(verify_edge_access)):
    case_id = _normalize_case_id(case_id)
    with db_lock:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT id, created_at, status, ai_status, ai_message, ai_level, ai_prob, ai_suggestion "
                "FROM cases WHERE id=?",
                (case_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Case not found")
    return dict(row)

@app.post("/api/review/submit")
async def review_submit(
    case_id: str = Form(...),
    level: int = Form(...),
    note: str = Form(""),
    _auth: bool = Depends(verify_edge_access)
):
    case_id = _normalize_case_id(case_id)
    return submit_review_workflow(case_id=case_id, level=level, note=note)

# ============================
# 手動刪除案件 API（刪除 DB 記錄 + 實體圖片）
# ============================
@app.delete("/api/case/delete/{case_id}")
async def delete_case(case_id: str, _auth: bool = Depends(verify_edge_access)):
    """手動刪除案件：移除 DB 記錄並刪除 uploads/ 中的實體圖片檔。"""
    case_id = _normalize_case_id(case_id)
    with db_lock:
        conn = get_conn()
        try:
            row = conn.execute("SELECT image_filename FROM cases WHERE id=?", (case_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Case not found")

            image_filename = row["image_filename"]
            conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
            conn.commit()
        finally:
            conn.close()

    # File I/O outside db_lock
    image_deleted = False
    if image_filename:
        image_path = os.path.realpath(os.path.join(UPLOAD_FOLDER, image_filename))
        if image_path.startswith(os.path.realpath(UPLOAD_FOLDER) + os.sep):
            try:
                if os.path.exists(image_path):
                    os.remove(image_path)
                    image_deleted = True
                    logger.info(f"[DELETE] 圖片已刪除: {case_id}")
            except Exception as e:
                logger.error(f"[DELETE] 圖片刪除失敗 {case_id}: {e}")
        else:
            logger.warning(f"[SEC] 路徑穿越嘗試已阻擋 for case_id={case_id}")

    logger.info(f"[DELETE] 案件已刪除: {case_id}, 圖片刪除={'yes' if image_deleted else 'no'}")
    return {"status": "ok", "message": "Case and image deleted", "image_deleted": image_deleted}
