# Payload Transform Scripts

Optional Python scripts that reshape webhook payloads and/or provide custom signature verification. They serve three purposes:

1. **Pre-filtering (drop):** Return `None` to drop low-value events before any LLM call.
2. **Payload cleanup (reshape):** Return a slimmed-down dict with only the fields the agent needs, reducing token cost. For Inbox agents, cleaner payloads also mean a leaner JSONL queue at drain time.
3. **Custom signature verification:** Implement non-standard verification when `signature_algo: custom` is set (e.g., Readwise passes secrets in the payload body rather than headers).

Transforms are a **deterministic pre-filter layer** between the webhook and the LLM. Return `None` from `transform()` to silently drop an event — the webhook is acknowledged (HTTP 200) but no agents are matched or dispatched. This applies cheap, local rules (sender, labels, keywords, event subtype) so the LLM only sees events that matter.

Scripts live in the directory configured via `transforms_dir` (gitignored — user-specific).

---

## Interface

```python
def transform(payload: dict, event_type: str) -> dict | None:
    """Reshape raw webhook payload. Return a dict for {{ payload }} injection, or None to drop the event."""
    ...

def verify(header_value: str, body: bytes, secret: str) -> bool:
    """Custom signature verification. Only called when signature_algo is 'custom'."""
    ...
```

Both functions are optional — a script can export one or both.

Scripts are lazy-loaded on first use and cached with mtime-based invalidation. Editing the file on disk automatically reloads on the next webhook. Syntax errors fall back to no-transform behavior with a log error.

---

## Examples

### Standard Signature Verification (Todoist)

Todoist uses HMAC-SHA256-base64 — the generic verification path handles it. Only a payload transform is needed.

```bash
dispatcher-cli webhook add todoist \
  --secret-env TODOIST_WEBHOOK_SECRET \
  --signature-header x-todoist-hmac-sha256 \
  --signature-algo hmac-sha256-base64 \
  --event-type-path '$.event_name' \
  --event-id-path '$.event_data.id' \
  --transform-script todoist.py
```

```python
def transform(payload: dict, event_type: str) -> dict:
    data = payload.get("event_data", {})
    return {
        "event": event_type,
        "task_id": data.get("id"),
        "content": data.get("content"),
        "description": data.get("description"),
        "project_id": data.get("project_id"),
        "labels": data.get("labels", []),
        "priority": data.get("priority"),
        "due": data.get("due"),
    }
```

---

### Custom Signature Verification (Readwise)

Readwise passes a 32-character secret in the webhook payload body (not in headers). Custom verification extracts and compares it.

```bash
dispatcher-cli webhook add readwise \
  --secret-env READWISE_WEBHOOK_SECRET \
  --signature-algo custom \
  --event-type-path '$.event_type' \
  --event-id-path '$.id' \
  --transform-script readwise.py
```

```python
import hmac
import json


def verify(header_value: str, body: bytes, secret: str) -> bool:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False
    return hmac.compare_digest(payload.get("secret", ""), secret)


def transform(payload: dict, event_type: str) -> dict:
    return {
        "event": event_type,
        "id": payload.get("id"),
        "title": payload.get("title", ""),
        "author": payload.get("author", ""),
        "url": payload.get("source_url") or payload.get("url", ""),
        "category": payload.get("category", ""),
        "summary": (payload.get("summary") or "")[:2000],
        "tags": payload.get("tags", []),
    }
```

---

### Event Dropping (Readwise)

The Readwise transform above passes through all document types. This variant demonstrates **event dropping**: returning `None` to silently discard categories you don't want (e.g., EPUBs, PDFs) before any LLM call is made.

```python
import hmac
import json

KEEP_CATEGORIES = {"article", "tweet", "rss"}


def verify(header_value: str, body: bytes, secret: str) -> bool:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False
    return hmac.compare_digest(payload.get("secret", ""), secret)


def transform(payload: dict, event_type: str) -> dict | None:
    category = (payload.get("category") or "").lower()
    if category not in KEEP_CATEGORIES:
        return None  # drop EPUBs, PDFs, emails, etc.

    return {
        "event": event_type,
        "id": payload.get("id"),
        "title": payload.get("title", ""),
        "author": payload.get("author", ""),
        "url": payload.get("source_url") or payload.get("url", ""),
        "category": category,
        "summary": (payload.get("summary") or "")[:2000],
        "tags": payload.get("tags", []),
    }
```
