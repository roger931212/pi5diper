import os
import logging
from fastapi.templating import Jinja2Templates

# ============================
# Log 設定
# ============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ============================
# 設定與環境變數
# ============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

EXTERNAL_BASE = os.getenv("EXTERNAL_BASE", "https://your-app.zeabur.app").rstrip("/")
EXTERNAL_API_KEY = os.getenv("EXTERNAL_API_KEY", "").strip()
EXTERNAL_SIGNING_SECRET = os.getenv("EXTERNAL_SIGNING_SECRET", "").strip()
EDGE_AUTH_TOKEN = os.getenv("EDGE_AUTH_TOKEN", "").strip()
EDGE_ALLOWED_IPS = {
    ip.strip() for ip in os.getenv("EDGE_ALLOWED_IPS", "").split(",") if ip.strip()
}
EDGE_COOKIE_SECURE = os.getenv("EDGE_COOKIE_SECURE", "1").strip() == "1"
EDGE_COOKIE_MAX_AGE_SEC = int(os.getenv("EDGE_COOKIE_MAX_AGE_SEC", "28800"))
RUN_BACKGROUND_WORKERS = os.getenv("RUN_BACKGROUND_WORKERS", "1").strip() == "1"
BACKGROUND_LOCK_PATH = os.path.join(BASE_DIR, ".edge_workers.lock")

if not EXTERNAL_API_KEY or EXTERNAL_API_KEY == "SET_ME_PLEASE":
    logger.critical("❌ 尚未設定 EXTERNAL_API_KEY！伺服器拒絕啟動。")
    raise RuntimeError("EXTERNAL_API_KEY must be set in environment variables.")
if not EXTERNAL_SIGNING_SECRET:
    logger.critical("❌ 尚未設定 EXTERNAL_SIGNING_SECRET！伺服器拒絕啟動。")
    raise RuntimeError("EXTERNAL_SIGNING_SECRET must be set in environment variables.")
if (
    not EXTERNAL_BASE
    or "your-app.zeabur.app" in EXTERNAL_BASE
    or not (EXTERNAL_BASE.startswith("http://") or EXTERNAL_BASE.startswith("https://"))
):
    logger.critical("❌ EXTERNAL_BASE 未正確設定（不可使用預設 placeholder，且必須是 http/https URL）")
    raise RuntimeError("EXTERNAL_BASE must be a valid http(s) URL and not the default placeholder.")
if not EDGE_AUTH_TOKEN:
    logger.critical("❌ 尚未設定 EDGE_AUTH_TOKEN！伺服器拒絕啟動。")
    raise RuntimeError("EDGE_AUTH_TOKEN must be set in environment variables.")

# LINE
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_PUSH_API = os.getenv("LINE_PUSH_API", "https://api.line.me/v2/bot/message/push").strip()
try:
    LINE_API_TIMEOUT_SEC = max(1.0, float(os.getenv("LINE_API_TIMEOUT_SEC", "10")))
except Exception:
    LINE_API_TIMEOUT_SEC = 10.0
if not LINE_CHANNEL_ACCESS_TOKEN:
    logger.critical("❌ 尚未設定 LINE_CHANNEL_ACCESS_TOKEN！伺服器拒絕啟動。")
    raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN must be set in environment variables.")

# 內網資料
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
CROP_FOLDER = os.path.join(UPLOAD_FOLDER, "crops")
DB_PATH = os.path.join(BASE_DIR, "internal.db")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CROP_FOLDER, exist_ok=True)

# 同步頻率
SYNC_EMPTY_SLEEP_SEC = float(os.getenv("SYNC_EMPTY_SLEEP_SEC", "5"))
SYNC_ERROR_SLEEP_SEC = float(os.getenv("SYNC_ERROR_SLEEP_SEC", "10"))
RECONCILE_INTERVAL_SEC = float(os.getenv("RECONCILE_INTERVAL_SEC", "30"))
LINE_RETRY_INTERVAL_SEC = float(os.getenv("LINE_RETRY_INTERVAL_SEC", "60"))

# 安全：限制允許寫入的副檔名（避免奇怪檔案）
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png"}

MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", "8388608"))  # 8MB
MAX_NOTE_CHARS = int(os.getenv("MAX_NOTE_CHARS", "800"))
MAX_AI_SUGGESTION_CHARS = int(os.getenv("MAX_AI_SUGGESTION_CHARS", "1200"))

# LINE retry dead-letter threshold
LINE_MAX_RETRY_COUNT = int(os.getenv("LINE_MAX_RETRY_COUNT", "10"))

# ============================
# AI 三階段模型設定
# ============================
# Stage 1 (diaper region detection) and Stage 2 (lesion detection) use SEPARATE model weights.
# They MUST NOT point to the same file in production.
DIAPER_DETECTOR_MODEL_PATH = os.getenv(
    "DIAPER_DETECTOR_MODEL_PATH",
    os.path.join(BASE_DIR, "models", "yolo_best_step1.pt"),
)
LESION_DETECTOR_MODEL_PATH = os.getenv(
    "LESION_DETECTOR_MODEL_PATH",
    os.path.join(BASE_DIR, "models", "yolo_best_step2.pt"),
)
SEVERITY_CLASSIFIER_MODEL_PATH = os.getenv(
    "SEVERITY_CLASSIFIER_MODEL_PATH",
    os.path.join(BASE_DIR, "models", "efficientnet_best.pth"),
)

# Production safety: Stage 1 and Stage 2 MUST use different model files.
_s1_real = os.path.realpath(DIAPER_DETECTOR_MODEL_PATH) if os.path.exists(DIAPER_DETECTOR_MODEL_PATH) else DIAPER_DETECTOR_MODEL_PATH
_s2_real = os.path.realpath(LESION_DETECTOR_MODEL_PATH) if os.path.exists(LESION_DETECTOR_MODEL_PATH) else LESION_DETECTOR_MODEL_PATH
if _s1_real == _s2_real:
    logger.critical(
        "❌ DIAPER_DETECTOR_MODEL_PATH and LESION_DETECTOR_MODEL_PATH resolve to the same file: %s. "
        "Stage 1 (diaper detection) and Stage 2 (lesion detection) require separate models.",
        _s1_real,
    )
    raise RuntimeError(
        "DIAPER_DETECTOR_MODEL_PATH and LESION_DETECTOR_MODEL_PATH must not point to the same file. "
        "Stage 1 and Stage 2 are different detection tasks requiring separate trained models."
    )

DIAPER_DETECT_CONF_THRESHOLD = float(os.getenv("DIAPER_DETECT_CONF_THRESHOLD", "0.25"))
LESION_DETECT_CONF_THRESHOLD = float(os.getenv("LESION_DETECT_CONF_THRESHOLD", "0.25"))
MIN_CROP_SIZE_PX = int(os.getenv("MIN_CROP_SIZE_PX", "10"))

DIAPER_NOT_FOUND_MESSAGE = os.getenv(
    "DIAPER_NOT_FOUND_MESSAGE",
    "無法偵測到尿布位置，請重新拍照",
).strip()
LESION_NOT_FOUND_MESSAGE = os.getenv(
    "LESION_NOT_FOUND_MESSAGE",
    "未偵測到明確病灶，請重新拍照或持續觀察",
).strip()
AI_FAILED_MESSAGE = os.getenv(
    "AI_FAILED_MESSAGE",
    "AI 推論發生錯誤，請等待醫護人員人工審閱。",
).strip()

SEVERITY_SUGGESTIONS = {
    0: "目前未發現明顯尿布疹症狀，皮膚狀態正常。請繼續保持臀部清潔乾燥，勤換尿布。",
    1: "輕度尿布疹，皮膚出現局部發紅。建議加強清潔、保持乾燥，可使用含氧化鋅的護臀膏，並增加換尿布頻率。若 2-3 天未改善請就醫。",
    2: "中重度尿布疹，皮膚出現明顯紅腫或破損。建議盡快就醫，依醫師處方使用藥膏治療。在就醫前請保持患部乾燥並避免摩擦。",
}

# ============================
# Edge Token Bootstrap Security Note
# ============================
# The edge_token is accepted in query parameters ONLY for the root URL ("/")
# to allow initial UI bootstrap, then immediately redirected to a clean URL
# with the token stored in an httpOnly secure cookie.
#
# SECURITY RISK: During the initial request, the token appears in the URL
# and may be logged by:
#   - Web server access logs
#   - Reverse proxy logs
#   - Browser history
#
# MITIGATIONS in place:
#   - URL-based token accepted only on "/" path
#   - Immediate 303 redirect strips token from URL
#   - Cookie is httpOnly, Secure, SameSite=Strict
#   - EDGE_COOKIE_MAX_AGE_SEC limits cookie lifetime (default 8h)
#
# RECOMMENDED DEPLOYMENT ACTIONS:
#   - Configure reverse proxy to redact query parameters from access logs
#   - Use HTTPS exclusively
#   - Rotate EDGE_AUTH_TOKEN periodically

# Templates
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
