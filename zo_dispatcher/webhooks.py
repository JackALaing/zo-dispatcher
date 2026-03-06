import base64
import hashlib
import hmac as hmac_mod
import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType

logger = logging.getLogger("zo-dispatcher")

HASH_FUNCTIONS = {
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
}

_transform_cache: dict[str, tuple[float, ModuleType]] = {}


def load_transform_module(source_config: dict, transforms_dir: Path | None = None) -> ModuleType | None:
    script = source_config.get("transform_script")
    if not script or not transforms_dir:
        return None
    resolved = (transforms_dir / script).resolve()

    if not resolved.is_relative_to(transforms_dir.resolve()):
        logger.error(f"Transform path escapes transforms dir: {script}")
        return None

    key = str(resolved)
    try:
        current_mtime = resolved.stat().st_mtime
    except FileNotFoundError:
        _transform_cache.pop(key, None)
        return None

    cached = _transform_cache.get(key)
    if cached and cached[0] == current_mtime:
        return cached[1]

    try:
        spec = importlib.util.spec_from_file_location("transform", resolved)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _transform_cache[key] = (current_mtime, mod)
        return mod
    except Exception as e:
        logger.error(f"Failed to load transform {script}: {e}")
        _transform_cache.pop(key, None)
        return None


def verify_signature(source_config: dict, header_value: str | None, body: bytes,
                     transforms_dir: Path | None = None) -> bool:
    algo = source_config.get("signature_algo")
    secret_env = source_config.get("secret_env")

    if algo == "custom":
        mod = load_transform_module(source_config, transforms_dir)
        if not mod or not hasattr(mod, "verify"):
            logger.error(f"Custom algo requires verify() in transform script for source '{source_config['source']}'")
            return False
        secret = os.environ.get(secret_env, "") if secret_env else ""
        try:
            return mod.verify(header_value or "", body, secret)
        except Exception as e:
            logger.error(f"Custom verify() failed for source '{source_config['source']}': {e}")
            return False

    if secret_env:
        algo = algo or "hmac-sha256-hex"
        if not header_value:
            logger.warning(f"Missing signature header for source '{source_config['source']}'")
            return False
        secret = os.environ.get(secret_env, "")
        if not secret:
            logger.error(f"Secret env var '{secret_env}' not set")
            return False

        prefix = source_config.get("signature_prefix", "")
        if prefix and header_value.startswith(prefix):
            header_value = header_value[len(prefix):]

        parts = algo.split("-")
        if len(parts) != 3 or parts[0] != "hmac":
            logger.error(f"Invalid signature_algo '{algo}' for source '{source_config['source']}'")
            return False
        hash_name, fmt = parts[1], parts[2]

        hash_func = HASH_FUNCTIONS.get(hash_name)
        if not hash_func:
            logger.error(f"Unknown hash function '{hash_name}' in algo '{algo}'")
            return False

        computed = hmac_mod.new(secret.encode(), body, hash_func)
        if fmt == "hex":
            expected = computed.hexdigest()
        elif fmt == "base64":
            expected = base64.b64encode(computed.digest()).decode()
        else:
            logger.error(f"Unknown format '{fmt}' in algo '{algo}'")
            return False

        return hmac_mod.compare_digest(expected, header_value)

    if source_config.get("allow_unsigned"):
        logger.info(f"Accepting unsigned webhook from source '{source_config['source']}' (allow_unsigned=true)")
        return True

    logger.warning(f"Rejecting unsigned webhook from source '{source_config['source']}' (no secret_env, allow_unsigned not set)")
    return False


def apply_transform(source_config: dict, payload: dict, event_type: str,
                    transforms_dir: Path | None = None) -> dict | None:
    mod = load_transform_module(source_config, transforms_dir)
    if not mod or not hasattr(mod, "transform"):
        return payload
    try:
        result = mod.transform(payload, event_type)
        if result is None:
            logger.info(f"Transform dropped event for source '{source_config.get('source')}' (event_type={event_type})")
            return None
        return result
    except Exception as e:
        logger.error(f"Transform failed for {source_config.get('source')}: {e}")
        return payload


def _event_matches_single(agent_event: str, source: str, event_type: str | None) -> bool:
    if agent_event != source and not agent_event.startswith(source + "."):
        return False
    if agent_event == source:
        return True
    if not event_type:
        return False
    sub_event = agent_event[len(source) + 1:]
    return event_type == sub_event or event_type.startswith(sub_event + ".")


def event_matches(agent_event: str | list[str], source: str, event_type: str | None) -> bool:
    if isinstance(agent_event, list):
        return any(_event_matches_single(e, source, event_type) for e in agent_event)
    return _event_matches_single(agent_event, source, event_type)


def _get_nested_value(data: dict, path: str) -> str | None:
    if not path:
        return None
    parts = path.removeprefix("$.").split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return str(current) if current is not None else None
