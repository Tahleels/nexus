"""Live web search and generic external-API call tool implementations.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime

def web_search(query: str, **kwargs) -> dict:
    """Search the live internet and return a concise, cited answer.

    Primary path calls OpenAI's Responses API with the
    ``web_search_preview`` tool (model ``gpt-4.1-mini``) and asks it to
    answer the query concisely and cite source websites. If that call
    fails for any reason (missing connectivity, API error, etc.), falls
    back to the DuckDuckGo Instant Answer API (no key required), trying the
    abstract/definition/answer fields and then related topics.

    Args:
        query (str): The search query / question to answer with live data.
        **kwargs: Orchestrator-injected context. Recognized key:
            api_key (str): OpenAI API key. Falls back to the
                ``OPENAI_API_KEY`` environment variable if not supplied.

    Returns:
        dict: On success, ``{"success": True, "query": query, "answer":
        str, "summary": str, "source": "openai_web_search" | "duckduckgo" |
        "duckduckgo_topics"}``. On failure, ``{"success": False, "query":
        query, "error": str}`` — e.g. when no API key is configured, both
        the primary and fallback paths fail, or DuckDuckGo returns no
        results.
    """
    api_key = kwargs.get('api_key') or os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        return {"success": False, "query": query, "error": "OPENAI_API_KEY not set"}

    # ── Primary: OpenAI Responses API with web_search_preview ────────────────
    try:
        import urllib.request, urllib.error

        payload = {
            "model": "gpt-4.1-mini",
            "tools": [{"type": "web_search_preview"}],
            "input": f"Search the internet and answer this query with live data:\n\n{query}\n\nBe concise, factual, and cite source websites."
        }
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            response = json.loads(resp.read())

        full_text = ""
        for item in response.get("output", []):
            for block in (item.get("content", []) if isinstance(item, dict) else []):
                if isinstance(block, dict):
                    if block.get("type") == "output_text":
                        full_text += block.get("text", "")
                    elif "text" in block:
                        full_text += block.get("text", "")
            if isinstance(item, dict) and item.get("type") == "text":
                full_text += item.get("text", "")

        full_text = full_text.strip()
        if full_text:
            return {"success": True, "query": query, "answer": full_text, "summary": full_text, "source": "openai_web_search"}

    except Exception as primary_err:
        pass  # fall through to DuckDuckGo

    # ── Fallback: DuckDuckGo Instant Answer API (no key needed) ──────────────
    try:
        import urllib.request, urllib.parse
        params  = urllib.parse.urlencode({"q": query, "format": "json", "no_redirect": "1"})
        ddg_url = f"https://api.duckduckgo.com/?{params}"
        req     = urllib.request.Request(ddg_url,
                                         headers={"User-Agent": "Nexus/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ddg = json.loads(resp.read())

        answer = (ddg.get("AbstractText")
                  or ddg.get("Answer")
                  or ddg.get("Definition")
                  or "")
        if answer:
            return {"success": True, "query": query, "answer": answer,
                    "summary": answer, "source": "duckduckgo"}

        # If no abstract, return related topics
        topics = [t.get("Text", "") for t in ddg.get("RelatedTopics", [])[:3] if t.get("Text")]
        if topics:
            combined = "\n".join(topics)
            return {"success": True, "query": query, "answer": combined,
                    "summary": combined, "source": "duckduckgo_topics"}

        return {"success": False, "query": query,
                "error": "No results found for this query."}

    except Exception as fallback_err:
        return {"success": False, "query": query,
                "error": f"Web search failed: {fallback_err}"}


def call_external_api(url: str, method: str = "GET", payload: dict = None,
                      headers: dict = None, **kwargs) -> dict:
    """Make a real HTTP request to an arbitrary external REST API and return the response.

    No URL allowlist or domain restriction is applied — any agent with
    this tool enabled can reach any HTTP(S) endpoint.

    Args:
        url (str): Full URL to call.
        method (str): HTTP method — GET, POST, PUT, DELETE, etc. Defaults
            to "GET". Case-insensitive (normalised to uppercase).
        payload (dict, optional): JSON-serialisable body, sent for
            methods like POST/PUT. Encoded as a UTF-8 JSON byte string.
        headers (dict, optional): Extra HTTP headers to merge over the
            defaults (``Content-Type: application/json``,
            ``Accept: application/json``, a ``User-Agent`` string).
        **kwargs: Orchestrator-injected context (unused directly by this
            tool).

    Returns:
        dict: On success, ``{"success": True, "status": int, "url": str,
        "method": str, "response": Any}`` where ``response`` is the
        parsed JSON body, or the raw decoded text if it isn't valid JSON.
        On an HTTP error response, ``{"success": False, "status": int,
        "url": str, "error": Any}`` (error body parsed as JSON when
        possible). On any other failure (e.g. connection error),
        ``{"success": False, "url": str, "error": str}``.
    """
    import urllib.request, urllib.error

    method       = method.upper()
    req_headers  = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   "Nexus-Agent/1.0",
    }
    if headers:
        req_headers.update(headers)

    data = json.dumps(payload).encode('utf-8') if payload else None
    req  = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body        = resp.read().decode('utf-8', errors='replace')
            status_code = resp.status
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = body
        return {"success": True, "status": status_code, "url": url,
                "method": method, "response": parsed}
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        try:
            body = json.loads(body)
        except Exception:
            pass
        return {"success": False, "status": e.code, "url": url, "error": body}
    except Exception as e:
        return {"success": False, "url": url, "error": str(e)}
