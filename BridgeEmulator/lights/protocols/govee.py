import json
import requests
import uuid
import logManager
from functions.colors import convert_rgb_xy, convert_xy, hsv_to_rgb
from typing import List, Dict, Any

logging = logManager.logger.get_logger(__name__)

BASE_URL = "https://openapi.api.govee.com/router/api/v1"
BASE_TYPE = "devices.capabilities."

def get_headers() -> Dict[str, str]:
    import configManager
    bridgeConfig = configManager.bridgeConfig.yaml_config
    return {
        "Govee-API-Key": bridgeConfig["config"]["govee"].get('api_key', ''),
        "Content-Type": "application/json"
    }

def is_json(content: str) -> bool:
    try:
        json.loads(content)
    except ValueError:
        return False
    return True

def discover(detectedLights: List[Dict[str, Any]]) -> None:
    logging.debug("Govee: <discover> invoked!")
    try:
        response = requests.get(f"{BASE_URL}/user/devices", headers=get_headers())
        response.raise_for_status()
        if response.content and is_json(response.content):
            devices = response.json().get("data", {})
            logging.debug(f"Govee: Found {len(devices)} devices")
            for device in devices:
                device_id = device["device"]
                device_name = device.get("deviceName", f'{device["sku"]}-{device_id.replace(":","")[10:]}')
                capabilities = [function["type"] for function in device.get("capabilities", [])]
                if has_capabilities(capabilities, ["on_off", "segment_color_setting"]):
                    handle_segmented_device(device, device_name, detectedLights)
                elif has_capabilities(capabilities, ["on_off", "color_setting"]):
                    handle_non_segmented_device(device, device_name, detectedLights)
    except requests.RequestException as e:
        logging.error("Error connecting to Govee: %s", e)

def has_capabilities(capabilities: List[str], required_capabilities: List[str]) -> bool:
    return all(f"{BASE_TYPE}{cap}" in capabilities for cap in required_capabilities)

def handle_segmented_device(device: Dict[str, Any], device_name: str, detectedLights: List[Dict[str, Any]]) -> None:
    segments, bri_range = get_segmented_device_info(device)
    logging.debug(f"Govee: Found {device_name} with {segments} segments")
    for option in range(segments):
        detectedLights.append(create_light_entry(device, device_name, option, bri_range))

def get_segmented_device_info(device: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    segments = 0
    bri_range = {}
    for function in device.get("capabilities", []):
        if function["type"] == f"{BASE_TYPE}segment_color_setting":
            try:
                segments = len(function['parameters']['fields'][0]['options'])
            except (KeyError, IndexError):
                segments = 1
        if function["type"] == f"{BASE_TYPE}range" and "brightness" in function.get("instance", ""):
            bri_range = function.get('parameters', {}).get('range', {})
    return segments, bri_range

def create_light_entry(device: Dict[str, Any], device_name: str, segment_id: int, bri_range: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "protocol": "govee",
        "name": f"{device_name}-seg{segment_id}" if segment_id >= 0 else device_name,
        "modelid": "LLC010",
        "protocol_cfg": {
            "device_id": device["device"],
            "sku_model": device["sku"],
            "segmentedID": segment_id,
            "bri_range": {
                "min": bri_range.get("min", 1),
                "max": bri_range.get("max", 100),
                "precision": bri_range.get("precision", 1)
            }
        }
    }

def handle_non_segmented_device(device: Dict[str, Any], device_name: str, detectedLights: List[Dict[str, Any]]) -> None:
    bri_range = get_brightness_range(device)
    detectedLights.append(create_light_entry(device, device_name, -1, bri_range))
    logging.debug(f"Govee: Found {device_name}")

def get_brightness_range(device: Dict[str, Any]) -> Dict[str, Any]:
    for function in device.get("capabilities", []):
        if function["type"] == f"{BASE_TYPE}range" and "brightness" in function.get("instance", ""):
            return function.get('parameters', {}).get('range', {})
    return {}

def set_light(light: Any, data: Dict[str, Any]) -> None:
    for data_type in data:
        request_data = create_request_data(light, data, data_type)
        if request_data is not None:
            # JAVÍTVA: POST metódus használata PUT helyett
            payload = {
                "requestId": str(uuid.uuid4()),
                "payload": request_data
            }
            try:
                response = requests.post(f"{BASE_URL}/device/control", headers=get_headers(), json=payload, timeout=5)
                response.raise_for_status()
            except requests.RequestException as e:
                logging.error(f"Govee set_light Error: {e}")

def create_request_data(light: Any, data: Dict[str, Any], data_type: str) -> Dict[str, Any]:
    device_id = light.protocol_cfg["device_id"]
    model = light.protocol_cfg["sku_model"]
    request_data = {"sku": model, "device": device_id}

    if data_type == "on":
        request_data["capability"] = create_on_off_capability(data["on"])
        return request_data

    elif data_type == "bri":
        request_data["capability"] = create_brightness_capability(data['bri'], light.protocol_cfg.get("segmentedID", -1), light.protocol_cfg.get("bri_range", {}))
        return request_data

    elif data_type == "xy":
        r, g, b = convert_xy(data['xy'][0], data['xy'][1], data.get('bri', 255))
        request_data["capability"] = create_color_capability(r, g, b, light.protocol_cfg.get("segmentedID", -1))
        return request_data

    elif data_type in ["hue", "sat"]:
        hue = data.get('hue', 0)
        sat = data.get('sat', 0)
        bri = data.get('bri', 255)
        r, g, b = hsv_to_rgb(hue, sat, bri)
        request_data["capability"] = create_color_capability(r, g, b, light.protocol_cfg.get("segmentedID", -1))
        return request_data

    # HOZZÁADVA: CT (Kelvin / Színhőmérséklet) támogatás Hue mired átváltással
    elif data_type == "ct":
        mired = data['ct']
        # Mired átváltása Kelvinre (Kelvin = 1,000,000 / mired)
        kelvin = int(1000000 / mired)
        # Korlátozás a Govee által támogatott 2000K - 9000K tartományra
        kelvin = max(2000, min(9000, kelvin))
        
        request_data["capability"] = {
            "type": f"{BASE_TYPE}color_setting",
            "instance": "colorTemperatureK",
            "value": kelvin
        }
        return request_data

    else:
        return None

def create_on_off_capability(value: bool) -> Dict[str, Any]:
    # JAVÍTVA: Boolean helyett Integer (1 vagy 0) küldése
    return {
        "type": f"{BASE_TYPE}on_off",
        "instance": "powerSwitch",
        "value": 1 if value else 0
    }

def create_brightness_capability(brightness: int, segment_id: int, bri_range: Dict[str, Any]) -> Dict[str, Any]:
    min_val = bri_range.get("min", 1)
    max_val = bri_range.get("max", 100)
    mapped_value = int(round(min_val + ((brightness / 255) * (max_val - min_val))))
    mapped_value = max(1, mapped_value)

    if segment_id >= 0:
        return {
            "type": f"{BASE_TYPE}segment_color_setting",
            "instance": "segmentedBrightness",
            "value": {
                "segment": [segment_id],
                "brightness": mapped_value
            }
        }
    return {
        "type": f"{BASE_TYPE}range",
        "instance": "brightness",
        "value": mapped_value
    }

def create_color_capability(r: int, g: int, b: int, segment_id: int) -> Dict[str, Any]:
    rgb_int = (((r & 0xFF) << 16) | ((g & 0xFF) << 8) | (b & 0xFF))
    if segment_id >= 0:
        return {
            "type": f"{BASE_TYPE}segment_color_setting",
            "instance": "segmentedColorRgb",
            "value": {
                "segment": [segment_id],
                "rgb": rgb_int
            }
        }
    return {
        "type": f"{BASE_TYPE}color_setting",
        "instance": "colorRgb",
        "value": rgb_int
    }

def get_light_state(light: Any) -> Dict[str, Any]:
    payload = {
        "requestId": str(uuid.uuid4()),
        "payload": {
            "sku": light.protocol_cfg["sku_model"],
            "device": light.protocol_cfg["device_id"]
        }
    }
    try:
        # JAVÍTVA: POST kérés használata helyes lekérdezéshez
        response = requests.post(f"{BASE_URL}/device/state", headers=get_headers(), json=payload, timeout=5)
        response.raise_for_status()
        return parse_light_state(response.json().get("payload", {}).get("capabilities", []), light)
    except Exception as e:
        logging.error(f"Govee get_light_state Error: {e}")
        return {}

def parse_light_state(state_data: List[Dict[str, Any]], light: Any) -> Dict[str, Any]:
    state = {}
    for function in state_data:
        f_type = function.get("type", "")
        f_state = function.get("state", {}).get("value")
        
        if f_type == f"{BASE_TYPE}online":
            state["reachable"] = str(f_state).lower() == "true"
        if f_type == f"{BASE_TYPE}on_off":
            state["on"] = f_state == 1
        if f_type == f"{BASE_TYPE}range" and "brightness" in function.get("instance", ""):
            max_bri = light.protocol_cfg.get("bri_range", {}).get("max", 100)
            state["bri"] = round((f_state / max_bri) * 255)
        if f_type == f"{BASE_TYPE}color_setting" and function.get("instance") == "colorRgb":
            rgb = f_state
            state["xy"] = convert_rgb_xy((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF)
    return state
