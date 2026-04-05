import base64
import binascii
import logging
import math
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
CROP_FOLDER = os.path.join(UPLOAD_FOLDER, "crops")
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png"}
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", "8388608"))
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
DIAPER_DETECT_CONF_THRESHOLD = float(os.getenv("DIAPER_DETECT_CONF_THRESHOLD", "0.25"))
LESION_DETECT_CONF_THRESHOLD = float(os.getenv("LESION_DETECT_CONF_THRESHOLD", "0.25"))
MIN_CROP_SIZE_PX = int(os.getenv("MIN_CROP_SIZE_PX", "10"))
DIAPER_NOT_FOUND_MESSAGE = os.getenv(
    "DIAPER_NOT_FOUND_MESSAGE",
    "無法偵測到尿布位置，請重新拍照",
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
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CROP_FOLDER, exist_ok=True)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def sanitize_ext(ext: str) -> str:
    ext = (ext or "").lower().strip()
    if not ext.startswith("."):
        ext = "." + ext
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in ALLOWED_IMAGE_EXT:
        return ".jpg"
    return ext


def _detect_image_ext_from_magic(data: bytes) -> str | None:
    if not data:
        return None
    if data.startswith(b"\xFF\xD8\xFF"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    return None


def save_base64_image(b64_str: str, filename: str) -> bool:
    try:
        if not b64_str:
            return False

        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]

        b64_str = "".join((b64_str or "").split())
        approx_bytes = (len(b64_str) * 3) // 4
        if approx_bytes > MAX_IMAGE_BYTES + 4096:
            logger.error(f"[SAVE] image too large (estimated={approx_bytes}, max={MAX_IMAGE_BYTES})")
            return False

        try:
            img_data = base64.b64decode(b64_str, validate=True)
        except (binascii.Error, ValueError):
            img_data = base64.b64decode(b64_str)

        if not img_data or len(img_data) > MAX_IMAGE_BYTES:
            return False

        detected_ext = _detect_image_ext_from_magic(img_data[:16])
        if not detected_ext:
            return False
        expected_ext = sanitize_ext(os.path.splitext(filename)[1] or ".jpg")
        if expected_ext != detected_ext:
            return False

        save_path = os.path.abspath(os.path.join(UPLOAD_FOLDER, filename))
        if not save_path.startswith(os.path.abspath(UPLOAD_FOLDER) + os.sep):
            return False

        with open(save_path, "wb") as f:
            f.write(img_data)
        return True
    except Exception as e:
        logger.error(f"[SAVE] image save failed ({filename}): {e}")
        return False


_diaper_detector = None
_lesion_detector = None
_severity_classifier = None
_severity_device = None
_models_loaded = False
_models_lock = threading.Lock()

_EFFNET_NUM_CLASSES = 3
_EFFNET_INPUT_SIZE = 224
_AGGREGATION_RULE = "max_numeric_severity"


def _empty_stage1() -> Dict[str, Any]:
    return {
        "executed": False,
        "candidate_count": 0,
        "selected_bbox": None,
        "selected_confidence": None,
        "selected_area": None,
        "diaper_crop_path": None,
    }


def _empty_stage2() -> Dict[str, Any]:
    return {
        "executed": False,
        "candidate_count": 0,
        "valid_lesion_count": 0,
        "discarded_count": 0,
        "lesions": [],
    }


def _empty_stage3() -> Dict[str, Any]:
    return {
        "executed": False,
        "iterations": 0,
        "lesion_results": [],
    }


def _empty_aggregation() -> Dict[str, Any]:
    return {
        "executed": False,
        "aggregation_rule": _AGGREGATION_RULE,
        "case_severity": None,
        "representative_lesion_index": None,
    }


def _default_pipeline_result() -> Dict[str, Any]:
    return {
        "status": "error",
        "message": AI_FAILED_MESSAGE,
        "ai_level": None,
        "ai_prob": None,
        "ai_suggestion": "",
        "stage1": _empty_stage1(),
        "stage2": _empty_stage2(),
        "stage3": _empty_stage3(),
        "aggregation": _empty_aggregation(),
    }


def _save_crop(image_bgr, crop_basename: str) -> Optional[str]:
    try:
        import cv2

        os.makedirs(CROP_FOLDER, exist_ok=True)
        filename = f"{crop_basename}.jpg"
        out_path = os.path.join(CROP_FOLDER, filename)
        ok = cv2.imwrite(out_path, image_bgr)
        if not ok:
            return None
        return f"crops/{filename}"
    except Exception:
        return None


def _load_models():
    global _diaper_detector, _lesion_detector, _severity_classifier, _severity_device, _models_loaded
    with _models_lock:
        if _models_loaded:
            return

        try:
            from ultralytics import YOLO
        except ImportError:
            logger.error("[AI] ultralytics not installed")
            YOLO = None

        if YOLO is not None:
            if os.path.exists(DIAPER_DETECTOR_MODEL_PATH):
                try:
                    _diaper_detector = YOLO(DIAPER_DETECTOR_MODEL_PATH)
                    logger.info(f"[AI-S1] loaded diaper detector: {DIAPER_DETECTOR_MODEL_PATH}")
                except Exception as e:
                    logger.error(f"[AI-S1] diaper detector load failed: {e}")
            else:
                logger.error(f"[AI-S1] diaper detector not found: {DIAPER_DETECTOR_MODEL_PATH}")

            if os.path.exists(LESION_DETECTOR_MODEL_PATH):
                try:
                    _lesion_detector = YOLO(LESION_DETECTOR_MODEL_PATH)
                    logger.info(f"[AI-S2] loaded lesion detector: {LESION_DETECTOR_MODEL_PATH}")
                except Exception as e:
                    logger.error(f"[AI-S2] lesion detector load failed: {e}")
            else:
                logger.error(f"[AI-S2] lesion detector not found: {LESION_DETECTOR_MODEL_PATH}")

        if os.path.exists(SEVERITY_CLASSIFIER_MODEL_PATH):
            try:
                import torch
                from torchvision import models as tv_models

                _severity_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                model = tv_models.efficientnet_b0(weights=None)
                in_features = model.classifier[1].in_features
                model.classifier[1] = torch.nn.Linear(in_features, _EFFNET_NUM_CLASSES)
                state_dict = torch.load(SEVERITY_CLASSIFIER_MODEL_PATH, map_location=_severity_device, weights_only=True)
                model.load_state_dict(state_dict)
                model.to(_severity_device)
                model.eval()
                _severity_classifier = model
                logger.info(f"[AI-S3] loaded severity classifier: {SEVERITY_CLASSIFIER_MODEL_PATH} ({_severity_device})")
            except ImportError:
                logger.error("[AI-S3] torch/torchvision not installed")
            except Exception as e:
                logger.error(f"[AI-S3] severity classifier load failed: {e}")
        else:
            logger.error(f"[AI-S3] severity classifier not found: {SEVERITY_CLASSIFIER_MODEL_PATH}")

        _models_loaded = True


def get_model_readiness() -> Dict[str, Any]:
    """Return AI model readiness for health/readiness checks."""
    _load_models()
    missing = []
    if _diaper_detector is None:
        missing.append("diaper_detector")
    if _lesion_detector is None:
        missing.append("lesion_detector")
    if _severity_classifier is None:
        missing.append("severity_classifier")
    return {
        "ready": len(missing) == 0,
        "models_loaded": _models_loaded,
        "missing": missing,
        "paths": {
            "diaper_detector": DIAPER_DETECTOR_MODEL_PATH,
            "lesion_detector": LESION_DETECTOR_MODEL_PATH,
            "severity_classifier": SEVERITY_CLASSIFIER_MODEL_PATH,
        },
    }


def _is_finite_number(value: Any) -> bool:
    try:
        v = float(value)
        return math.isfinite(v)
    except Exception:
        return False


def _clip_bbox_float(x1: float, y1: float, x2: float, y2: float, width: int, height: int) -> Tuple[float, float, float, float]:
    cx1 = min(max(float(x1), 0.0), float(width))
    cy1 = min(max(float(y1), 0.0), float(height))
    cx2 = min(max(float(x2), 0.0), float(width))
    cy2 = min(max(float(y2), 0.0), float(height))
    return cx1, cy1, cx2, cy2


def _bbox_to_int(cx1: float, cy1: float, cx2: float, cy2: float, width: int, height: int) -> Tuple[int, int, int, int]:
    x1 = max(0, min(width, int(math.floor(cx1))))
    y1 = max(0, min(height, int(math.floor(cy1))))
    x2 = max(0, min(width, int(math.ceil(cx2))))
    y2 = max(0, min(height, int(math.ceil(cy2))))
    return x1, y1, x2, y2


def _collect_raw_boxes(result) -> List[Dict[str, float]]:
    if not result or not hasattr(result, "boxes") or result.boxes is None:
        return []
    boxes = result.boxes
    if len(boxes) == 0:
        return []

    raw_list: List[Dict[str, float]] = []
    for i in range(len(boxes)):
        try:
            coords = boxes.xyxy[i].cpu().numpy().tolist()
            conf = float(boxes.conf[i])
        except Exception:
            continue
        if len(coords) != 4:
            continue
        if not _is_finite_number(conf):
            continue
        if not all(_is_finite_number(v) for v in coords):
            continue
        raw_list.append(
            {
                "x1": float(coords[0]),
                "y1": float(coords[1]),
                "x2": float(coords[2]),
                "y2": float(coords[3]),
                "confidence": conf,
            }
        )
    return raw_list


def _select_stage1_bbox(result, image_w: int, image_h: int) -> Optional[Dict[str, Any]]:
    raw = _collect_raw_boxes(result)
    prepared: List[Dict[str, Any]] = []
    for item in raw:
        cx1, cy1, cx2, cy2 = _clip_bbox_float(item["x1"], item["y1"], item["x2"], item["y2"], image_w, image_h)
        area = max(0.0, (cx2 - cx1) * (cy2 - cy1))
        if area <= 0.0:
            continue
        x1, y1, x2, y2 = _bbox_to_int(cx1, cy1, cx2, cy2, image_w, image_h)
        if (x2 - x1) <= 0 or (y2 - y1) <= 0:
            continue
        prepared.append(
            {
                "bbox": [x1, y1, x2, y2],
                "confidence": float(item["confidence"]),
                "area": float(area),
                "y_min": float(cy1),
                "x_min": float(cx1),
            }
        )
    if not prepared:
        return None

    prepared.sort(
        key=lambda c: (
            -c["confidence"],
            -c["area"],
            c["y_min"],
            c["x_min"],
        )
    )
    return prepared[0]


def _predict_detector(detector, source, conf_threshold: float):
    if detector is None:
        return None
    results = detector.predict(source=source, conf=conf_threshold, verbose=False)
    if not results:
        return None
    return results[0]


def _crop_with_bbox(image_bgr, bbox: List[int]):
    if image_bgr is None or not bbox or len(bbox) != 4:
        return None
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        return None
    return image_bgr[y1:y2, x1:x2]


def _collect_valid_lesions(result, crop_w: int, crop_h: int) -> Tuple[List[Dict[str, Any]], int]:
    raw = _collect_raw_boxes(result)
    valid: List[Dict[str, Any]] = []
    discarded = 0

    for item in raw:
        conf = float(item["confidence"])
        if conf < LESION_DETECT_CONF_THRESHOLD:
            discarded += 1
            continue
        if not all(_is_finite_number(item[k]) for k in ("x1", "y1", "x2", "y2")):
            discarded += 1
            continue

        cx1, cy1, cx2, cy2 = _clip_bbox_float(item["x1"], item["y1"], item["x2"], item["y2"], crop_w, crop_h)
        x1, y1, x2, y2 = _bbox_to_int(cx1, cy1, cx2, cy2, crop_w, crop_h)
        width = x2 - x1
        height = y2 - y1
        area = width * height

        if width < MIN_CROP_SIZE_PX:
            discarded += 1
            continue
        if height < MIN_CROP_SIZE_PX:
            discarded += 1
            continue
        if area <= 0:
            discarded += 1
            continue

        valid.append(
            {
                "bbox": [x1, y1, x2, y2],
                "confidence": conf,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            }
        )

    valid.sort(key=lambda l: (-l["confidence"], l["x1"], l["y1"], l["x2"], l["y2"]))
    return valid, discarded


def _classify_with_effnet(image_bgr) -> Tuple[int, float]:
    import cv2
    import numpy as np
    import torch

    img_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    img_resized = cv2.resize(img_rgb, (_EFFNET_INPUT_SIZE, _EFFNET_INPUT_SIZE))
    img_float = img_resized.astype(np.float32) / 255.0

    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    img_normalized = (img_float - mean) / std
    img_tensor = torch.from_numpy(img_normalized.transpose(2, 0, 1)).unsqueeze(0).to(_severity_device)

    with torch.no_grad():
        logits = _severity_classifier(img_tensor)
        probs = torch.nn.functional.softmax(logits, dim=1)
        confidence, predicted = torch.max(probs, dim=1)

    pred_class = int(predicted.item())
    pred_class = max(0, min(pred_class, _EFFNET_NUM_CLASSES - 1))
    pred_conf = float(confidence.item())
    return pred_class, pred_conf


def _suggestion_from_severity(level: int) -> str:
    return SEVERITY_SUGGESTIONS.get(level, SEVERITY_SUGGESTIONS[2])


def run_ai_pipeline(local_image_path: str, case_id: Optional[str] = None) -> Dict[str, Any]:
    _load_models()
    result = _default_pipeline_result()
    if case_id:
        result["case_id"] = case_id

    try:
        import cv2

        image_bgr = cv2.imread(local_image_path)
        if image_bgr is None:
            result["status"] = "error"
            result["message"] = "AI 無法讀取圖片，請重新拍照。"
            return result
        img_h, img_w = image_bgr.shape[:2]

        result["stage1"]["executed"] = True
        if _diaper_detector is None:
            result["status"] = "error"
            result["message"] = "尿布區域偵測模型尚未部署，請等待醫護人員人工審閱。"
            return result
        stage1_raw = _predict_detector(_diaper_detector, local_image_path, DIAPER_DETECT_CONF_THRESHOLD)
        stage1_candidates = _collect_raw_boxes(stage1_raw) if stage1_raw is not None else []
        result["stage1"]["candidate_count"] = len(stage1_candidates)
        selected = _select_stage1_bbox(stage1_raw, img_w, img_h) if stage1_raw is not None else None
        if selected is None:
            result["status"] = "error"
            result["message"] = DIAPER_NOT_FOUND_MESSAGE
            return result

        diaper_bbox = selected["bbox"]
        diaper_crop = _crop_with_bbox(image_bgr, diaper_bbox)
        if diaper_crop is None:
            result["status"] = "error"
            result["message"] = DIAPER_NOT_FOUND_MESSAGE
            return result

        diaper_crop_path = _save_crop(diaper_crop, f"{(case_id or uuid.uuid4().hex)}_diaper")
        result["stage1"].update(
            {
                "selected_bbox": diaper_bbox,
                "selected_confidence": selected["confidence"],
                "selected_area": selected["area"],
                "diaper_crop_path": diaper_crop_path,
            }
        )

        result["stage2"]["executed"] = True
        if _lesion_detector is None:
            result["status"] = "error"
            result["message"] = "病灶位置辨識模型尚未部署，請等待醫護人員人工審閱。"
            return result
        stage2_raw = _predict_detector(_lesion_detector, diaper_crop, LESION_DETECT_CONF_THRESHOLD)
        stage2_candidates = _collect_raw_boxes(stage2_raw) if stage2_raw is not None else []
        lesion_valid, lesion_discarded = _collect_valid_lesions(stage2_raw, diaper_crop.shape[1], diaper_crop.shape[0]) if stage2_raw is not None else ([], 0)
        result["stage2"].update(
            {
                "candidate_count": len(stage2_candidates),
                "valid_lesion_count": len(lesion_valid),
                "discarded_count": lesion_discarded,
                "lesions": [{"bbox": l["bbox"], "confidence": l["confidence"]} for l in lesion_valid],
            }
        )

        result["stage3"]["executed"] = True
        if _severity_classifier is None:
            result["status"] = "error"
            result["message"] = "嚴重度辨識模型尚未部署，請等待醫護人員人工審閱。"
            return result

        lesion_results: List[Dict[str, Any]] = []
        for idx, lesion in enumerate(lesion_valid):
            lesion_crop = _crop_with_bbox(diaper_crop, lesion["bbox"])
            if lesion_crop is None:
                result["status"] = "error"
                result["message"] = "病灶裁切失敗，請等待醫護人員人工審閱。"
                return result
            crop_path = _save_crop(lesion_crop, f"{(case_id or uuid.uuid4().hex)}_lesion_{idx}")
            severity, cls_conf = _classify_with_effnet(lesion_crop)
            lesion_results.append(
                {
                    "bbox": lesion["bbox"],
                    "confidence": lesion["confidence"],
                    "severity": int(severity),
                    "classification_confidence": float(cls_conf),
                    "crop_path": crop_path,
                }
            )
        result["stage3"]["iterations"] = len(lesion_results)
        result["stage3"]["lesion_results"] = lesion_results

        result["aggregation"]["executed"] = True
        if lesion_results:
            severities = [int(x["severity"]) for x in lesion_results]
            case_severity = int(max(severities))
            representative_index = severities.index(case_severity)
            ai_prob = float(lesion_results[representative_index]["classification_confidence"])
        else:
            case_severity = 0
            representative_index = None
            ai_prob = None
        result["aggregation"]["case_severity"] = case_severity
        result["aggregation"]["representative_lesion_index"] = representative_index

        result["status"] = "done"
        result["message"] = "分析完成"
        result["ai_level"] = case_severity
        result["ai_prob"] = ai_prob
        result["ai_suggestion"] = _suggestion_from_severity(case_severity)
        return result

    except Exception as e:
        logger.error(f"[AI] pipeline error: {e}")
        result["status"] = "error"
        result["message"] = AI_FAILED_MESSAGE
        result["ai_suggestion"] = ""
        return result


def run_ai_model(local_image_path: str) -> Tuple[int, float, str]:
    result = run_ai_pipeline(local_image_path)
    level = int(result.get("ai_level") or 0)
    prob = float(result.get("ai_prob") or 0.0)
    suggestion = result.get("ai_suggestion") or _suggestion_from_severity(level)
    return level, prob, suggestion
