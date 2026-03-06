import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dateutil.rrule import rrulestr

logger = logging.getLogger("zo-dispatcher")


def parse_agent_file(filepath: Path, agents_dir: Path) -> tuple[dict | None, str | None]:
    try:
        text = filepath.read_text()
    except Exception as e:
        logger.error(f"Failed to read {filepath}: {e}")
        return None, f"Failed to read file: {e}"

    fm_match = re.match(r"^---\n(.*?)\n---\n?(.*)", text, re.DOTALL)
    if not fm_match:
        logger.warning(f"No YAML frontmatter in {filepath}")
        return None, "No YAML frontmatter"

    raw_yaml = fm_match.group(1)

    try:
        frontmatter = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as e:
        logger.error(f"Invalid YAML in {filepath}: {e}")
        return None, f"Invalid YAML: {e}"

    body = fm_match.group(2).strip()
    if not body:
        logger.warning(f"Empty body in {filepath}")
        return None, "Empty prompt body"

    trigger = frontmatter.get("trigger")
    if trigger not in ("schedule", "webhook", "both"):
        logger.warning(f"Invalid or missing trigger in {filepath}")
        return None, f"Invalid or missing trigger: {trigger!r}"

    if trigger == "schedule" and not frontmatter.get("rrule"):
        logger.warning(f"Schedule agent missing rrule: {filepath}")
        return None, "Schedule agent missing rrule"

    # Normalize event to a list
    raw_event = frontmatter.get("event")
    if isinstance(raw_event, str):
        event_list = [raw_event]
    elif isinstance(raw_event, list):
        if not raw_event:
            logger.warning(f"Empty event list in {filepath}")
            return None, "Event list must not be empty"
        invalid = [e for e in raw_event if not isinstance(e, str) or not e.strip()]
        if invalid:
            logger.warning(f"Invalid event entries in {filepath}: {invalid}")
            return None, f"Event entries must be non-empty strings, got: {invalid}"
        event_list = raw_event
    elif raw_event is None:
        event_list = None
    else:
        logger.warning(f"Invalid event type in {filepath}: {type(raw_event)}")
        return None, f"Event must be a string or list of strings, got: {type(raw_event).__name__}"

    if trigger in ("webhook", "both") and event_list is None:
        label = "Webhook" if trigger == "webhook" else "Dual-trigger"
        logger.warning(f"{label} agent missing event: {filepath}")
        return None, f"{label} agent missing event"

    if trigger == "both" and not frontmatter.get("rrule"):
        logger.warning(f"Dual-trigger agent missing rrule: {filepath}")
        return None, "Dual-trigger agent missing rrule"

    raw_defer = frontmatter.get("defer_to_cron", False)
    if raw_defer is True:
        defer_to_cron = "skip_if_empty"
    elif raw_defer is False or raw_defer is None:
        defer_to_cron = False
    elif isinstance(raw_defer, str) and raw_defer in ("skip_if_empty", "always_run"):
        defer_to_cron = raw_defer
    else:
        logger.warning(f"Invalid defer_to_cron value in {filepath}: {raw_defer!r}")
        return None, f"defer_to_cron must be false, skip_if_empty, or always_run, got: {raw_defer!r}"

    if defer_to_cron:
        if trigger == "schedule":
            logger.warning(f"defer_to_cron with trigger: schedule in {filepath}")
            return None, "defer_to_cron requires trigger: both (no events to queue)"
        if trigger == "webhook":
            logger.warning(f"defer_to_cron with trigger: webhook in {filepath}")
            return None, "defer_to_cron requires trigger: both (no scheduled run to drain queue)"

    # Namespaced ID from relative path
    rel = filepath.relative_to(agents_dir)
    agent_id = str(rel.with_suffix(""))  # "schedules/memory-extraction"

    warnings = []
    if defer_to_cron and "{{ queue_file }}" not in body:
        msg = f"defer_to_cron: {defer_to_cron} but prompt does not contain {{{{ queue_file }}}}"
        logger.warning(f"{msg}: {filepath}")
        warnings.append(msg)

    return {
        "id": agent_id,
        "trigger": trigger,
        "rrule": frontmatter.get("rrule"),
        "event": event_list,
        "model": frontmatter.get("model"),
        "persona": frontmatter.get("persona"),
        "active": frontmatter.get("active", True),
        "title": frontmatter.get("title", agent_id),
        "notify_channel": frontmatter.get("notify_channel"),
        "notify": frontmatter.get("notify", "errors"),
        "timeout": frontmatter.get("timeout"),
        "retry_delays": frontmatter.get("retry_delays"),
        "max_runs": frontmatter.get("max_runs"),
        "max_runs_window": frontmatter.get("max_runs_window", 3600),
        "defer_to_cron": defer_to_cron,
        "prompt": body,
        "_warnings": warnings,
    }, None


def compute_next_run(rrule_str: str, after: datetime) -> datetime | None:
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)

    has_dtstart = "DTSTART" in rrule_str.upper()
    try:
        if has_dtstart:
            rule = rrulestr(rrule_str)
        else:
            rule = rrulestr(rrule_str, dtstart=datetime(2026, 1, 1))
    except Exception:
        logger.error(f"Failed to parse rrule '{rrule_str}'")
        return None

    if has_dtstart:
        next_dt = rule.after(after, inc=False)
    else:
        next_dt = rule.after(after.replace(tzinfo=None), inc=False)

    if next_dt is None:
        return None
    if next_dt.tzinfo is not None:
        next_dt = next_dt.astimezone(timezone.utc)
    else:
        next_dt = next_dt.replace(tzinfo=timezone.utc)
    return next_dt
