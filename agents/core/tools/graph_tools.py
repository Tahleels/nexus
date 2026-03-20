"""Microsoft Graph (Teams + Outlook) communication tool implementations.
"""

import json
import os
import sys
import time
import tempfile
from datetime import datetime

def get_teams_chats_with_person(
    target: str,
    pool_size: int = 30,
    include_groups: bool = False,
    group_name: str = None,
    search_keyword: str = None,
    **kwargs,
) -> dict:
    """Fetch Microsoft Teams messages between the current user and another person.

    Calls Microsoft Graph (client-credentials / app-only auth) to list the
    requester's chats, find the one(s) matching ``target`` (and
    optionally ``group_name``), page through their messages, and return a
    merged, deduplication-free pool of messages formatted as a readable
    transcript. The requester's identity always comes from the hub
    session (never from LLM-supplied input), so this tool can only ever
    read the calling user's own chats.

    - Default: 1:1 chat only, most recent messages.
    - ``group_name``: filter to a specific group by topic name (implies
      ``include_groups=True``).
    - ``search_keyword``: scan deeper history (up to 500 messages per
      chat instead of 100) and return only messages containing the
      keyword.
    - Messages are returned oldest-first so they read naturally like a
      chat window, after being collected newest-first and truncated to
      ``pool_size``.

    Args:
        target (str): Email address or display name (or substring of
            either) of the person to find chats with. If it contains
            ``"@"`` it is matched as an exact email; otherwise matched as
            a case-insensitive substring of display name or email local
            part.
        pool_size (int): Maximum number of messages to return, taken from
            the most recent end of the merged pool. Defaults to 30.
        include_groups (bool): If True, also search group chats
            containing ``target`` (not just the 1:1 chat). Defaults to
            False. Implicitly set True when ``group_name`` is given.
        group_name (str, optional): Topic name (or substring) of a
            specific group chat to restrict the search to.
        search_keyword (str, optional): If given, scans deeper message
            history per chat and keeps only messages whose body contains
            this keyword (case-insensitive).
        **kwargs: Orchestrator-injected context. Recognized key:
            ``_hub_ctx`` (dict) — hub session context; ``_hub_ctx['user']
            ['email']`` is used as the requester's mailbox/chat owner.

    Returns:
        dict: On success, ``{"success": True, "requester": str, "target":
        str, "chats_searched": str, "message_count": int, "transcript":
        str}``. On failure, ``{"success": False, "error": str}`` — e.g.
        requester email missing from session, Teams not configured (env
        vars), no matching chat/group found, or a Microsoft Graph API
        error (``HTTPError``).

    Note:
        Auth: client-credentials flow (app-only). Requires admin consent
        for the ``Chat.Read.All`` application permission, plus a Teams
        ``CsApplicationAccessPolicy`` grant for this app (via the
        Microsoft Teams PowerShell module) -- without both, chat listing
        works but message reads 403.
        Message reads must hit ``/chats/{id}/messages`` directly, not
        ``/users/{email}/chats/{id}/messages`` -- the latter 403s with
        "Client access token validation ... failed" under app-only auth
        even with correct permissions/policy in place, while the bare
        chat-scoped endpoint succeeds.
        Env vars: ``TENANT_ID``, ``BOT_APP_ID``, ``BOT_APP_PASSWORD``.
    """
    import re as _re
    import urllib.request as _ureq
    import urllib.parse as _uparse
    import urllib.error as _uerr

    TENANT_ID      = os.environ.get("TENANT_ID", "")
    CLIENT_ID      = os.environ.get("BOT_APP_ID", "")
    CLIENT_SECRET  = os.environ.get("BOT_APP_PASSWORD", "")
    GRAPH          = "https://graph.microsoft.com/v1.0"
    MAX_CHAT_PAGES = 4    # cap chat listing at 200 chats max
    # When searching by keyword scan up to 10 pages (500 msgs) per chat;
    # for normal recent-message fetch use 2 pages (100 msgs).
    MAX_MSG_PAGES  = 10 if search_keyword else 2

    # group_name implies we need groups
    if group_name:
        include_groups = True

    # ── Resolve requester email — always from session, never from LLM input ──
    hub_ctx = kwargs.get("_hub_ctx") or {}
    user    = hub_ctx.get("user") or {}
    requester_email = user.get("email", "")
    if not requester_email:
        return {"success": False, "error": "Could not determine your email address from session."}

    if not TENANT_ID or not CLIENT_ID or not CLIENT_SECRET:
        return {
            "success": False,
            "error":   "Teams not configured. Set TENANT_ID, BOT_APP_ID, BOT_APP_PASSWORD.",
        }

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _get_token() -> str:
        """Acquire an app-only Graph access token via the client-credentials flow."""
        data = _uparse.urlencode({
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope":         "https://graph.microsoft.com/.default",
        }).encode()
        req = _ureq.Request(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            data=data, method="POST",
        )
        with _ureq.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["access_token"]

    def _get(url: str, token: str) -> dict:
        """GET a Microsoft Graph URL with a bearer token and return the parsed JSON body."""
        req = _ureq.Request(url, headers={"Authorization": f"Bearer {token}"})
        with _ureq.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def _strip_html(s: str) -> str:
        """Strip HTML tags from a Graph message body, returning plain text."""
        return _re.sub(r"<[^>]+>", "", s or "").strip()

    # ── List chats with expanded members (capped at MAX_CHAT_PAGES) ──────────
    def _list_chats(token: str) -> list:
        """List the requester's chats with members expanded, paging up to MAX_CHAT_PAGES."""
        chats, url, pages = [], f"{GRAPH}/users/{requester_email}/chats?$top=50&$expand=members", 0
        while url and pages < MAX_CHAT_PAGES:
            data = _get(url, token)
            chats += data.get("value", [])
            url = data.get("@odata.nextLink")
            pages += 1
        return chats

    def _match_member(chat: dict, t_lower: str, by_email: bool):
        """Return the matching member's display name/email if `chat` includes `t_lower`, else None."""
        for member in chat.get("members", []):
            email = (member.get("email") or "").lower()
            name  = (member.get("displayName") or "").lower()
            local = email.split("@")[0] if email else ""
            if email and email == requester_email.lower():
                continue
            if by_email:
                if email == t_lower:
                    return member.get("displayName") or member.get("email")
            else:
                if (name and t_lower in name) or (local and t_lower in local):
                    return member.get("displayName") or member.get("email")
        return None

    # ── Fetch messages from a single chat ────────────────────────────────────
    def _fetch_messages(chat_id: str, label: str, token: str) -> list:
        """Page through one chat's messages, strip HTML, and filter by `search_keyword` if set."""
        out, url, pages = [], (
            f"{GRAPH}/chats/{chat_id}/messages?$top=50"
        ), 0
        kw = search_keyword.lower() if search_keyword else None
        while url and pages < MAX_MSG_PAGES:
            data = _get(url, token)
            for m in data.get("value", []):
                body = _strip_html((m.get("body") or {}).get("content"))
                if not body:
                    continue
                if kw and kw not in body.lower():
                    continue  # skip messages that don't contain the keyword
                sender = (((m.get("from") or {}).get("user") or {})
                          .get("displayName")) or "system"
                out.append((m.get("createdDateTime", ""), sender, body, label))
            pages += 1
            url = data.get("@odata.nextLink")
        return out

    try:
        token  = _get_token()
        chats  = _list_chats(token)
        t_low  = target.strip().lower()
        by_email = "@" in t_low

        gn_low = group_name.strip().lower() if group_name else None

        matched = []
        for chat in chats:
            who = _match_member(chat, t_low, by_email)
            if not who:
                continue
            is_one_on_one = chat.get("chatType") == "oneOnOne"
            if not is_one_on_one and not include_groups:
                continue
            if is_one_on_one:
                label = f"1:1 with {who}"
            else:
                topic = chat.get("topic") or ""
                # If caller wants a specific group, skip non-matching ones
                if gn_low and gn_low not in topic.lower():
                    continue
                others = [
                    (m.get("displayName") or m.get("email") or "?")
                    for m in chat.get("members", [])
                    if (m.get("email") or "").lower() != requester_email.lower()
                ]
                label = f'Group "{topic}"' if topic else "Group [" + ", ".join(others[:5]) + "]"
            matched.append((chat, label, who))

        if not matched:
            if group_name:
                return {
                    "success": False,
                    "error":   f"No group chat named '{group_name}' found with '{target}'.",
                }
            if not include_groups:
                return {
                    "success": False,
                    "error":   f"No 1:1 chat found between {requester_email} and '{target}'. "
                               f"If you want to search group chats too, set include_groups=true.",
                }
            return {
                "success": False,
                "error":   f"No chats found for {requester_email} with '{target}'.",
            }

        # Collect pool newest-first, then reverse to ascending for natural reading
        pool = []
        for chat, label, _ in matched:
            pool.extend(_fetch_messages(chat["id"], label, token))
        pool.sort(key=lambda x: x[0], reverse=True)
        pool = pool[:pool_size]
        pool.reverse()  # oldest-first so messages read like a chat window

        matched_names = ", ".join(sorted({w for _, _, w in matched}))
        chat_labels   = "; ".join(sorted({lbl for _, lbl, _ in matched}))
        mode = f'keyword search: "{search_keyword}"' if search_keyword else "recent messages"
        lines = [
            f"Requester: {requester_email}",
            f"Target matched: {matched_names}",
            f"Chats: {chat_labels}",
            f"Mode: {mode}",
            f"--- {len(pool)} messages (oldest → newest) ---",
            "",
        ]
        for created, sender, body, label in pool:
            ts = created.replace("T", " ").replace("Z", "")[:19]
            src = f" ({label})" if include_groups else ""
            lines.append(f"[{ts}]{src} {sender}: {body[:400]}")

        return {
            "success":        True,
            "requester":      requester_email,
            "target":         matched_names,
            "chats_searched": chat_labels,
            "message_count":  len(pool),
            "transcript":     "\n".join(lines),
        }

    except _uerr.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"success": False, "error": f"Graph API {exc.code}: {body[:400]}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def get_outlook_emails(
    from_address: str = None,
    subject_keyword: str = None,
    body_keyword: str = None,
    folder: str = None,
    max_results: int = 20,
    unread_only: bool = False,
    date_from: str = None,
    date_to: str = None,
    **kwargs,
) -> dict:
    """Fetch emails from the current user's own Outlook mailbox via Microsoft Graph.

    The mailbox is always the calling user's (the email is resolved from
    the hub session, never from LLM-supplied input). Text criteria
    (``from_address``, ``subject_keyword``, ``body_keyword``) are combined
    into a single Graph ``$search`` KQL query; structural criteria
    (``unread_only``, ``date_from``, ``date_to``) use OData ``$filter``
    when no text search is active, or are applied as a client-side
    post-filter when a ``$search`` query is in effect (Graph does not
    support combining ``$search`` with ``$filter``/``$orderby`` for mail).

    Args:
        from_address (str, optional): Filter by sender — accepts an email
            address or a display name; used as a ``from:`` KQL qualifier.
        subject_keyword (str, optional): Keyword to search for in the
            subject line (``subject:`` KQL qualifier).
        body_keyword (str, optional): Keyword to search for in the email
            body (``body:`` KQL qualifier).
        folder (str, optional): Mailbox folder to search. If omitted (the
            default), searches the *entire* mailbox — every folder
            including custom ones (e.g. a folder an inbox rule auto-files
            client mail into), but excluding Sent Items, Drafts, Deleted
            Items, and Junk Email since those aren't received mail. This is
            almost always the right choice for an open-ended "what's my
            latest email" request, since inbox rules can route mail
            straight past the inbox. Pass an explicit value to scope the
            search: one of the well-known names "inbox", "sent"/
            "sentitems", "drafts", "deleted"/"deleteditems"/"trash",
            "archive", "junk"/"spam" — or the exact display name of any
            custom folder (e.g. "Acme Corp"), including folders nested
            under Inbox or other folders, resolved via a case-insensitive
            Graph lookup. If a named custom folder can't be found, the call
            fails with an error rather than silently falling back to inbox.
        max_results (int): Number of emails to return, clamped to 1-50.
            Defaults to 20.
        unread_only (bool): If True, return only unread emails. Defaults
            to False.
        date_from (str, optional): Start of date range, "YYYY-MM-DD".
            Only emails received on or after this date are returned.
        date_to (str, optional): End of date range, "YYYY-MM-DD". Only
            emails received on or before this date are returned.
        **kwargs: Orchestrator-injected context. Recognized key:
            ``_hub_ctx`` (dict) — hub session context;
            ``_hub_ctx['user']['email']`` is used as the mailbox owner.

    Returns:
        dict: On success, ``{"success": True, "requester": str, "folder":
        str, "email_count": int, "transcript": str}`` — ``transcript`` is
        a human-readable formatted listing (or an explanatory "no emails
        found" message when ``email_count`` is 0). On failure,
        ``{"success": False, "error": str}`` — e.g. requester email
        missing from session, Outlook not configured (env vars), or a
        Microsoft Graph API error (``HTTPError``).

    Note:
        Auth: client-credentials flow (app-only). Requires the
        ``Mail.Read.All`` application permission with admin consent.
        Env vars: ``TENANT_ID``, ``BOT_APP_ID``, ``BOT_APP_PASSWORD``.
    """
    import re as _re
    import urllib.request as _ureq
    import urllib.parse as _uparse
    import urllib.error as _uerr

    TENANT_ID     = os.environ.get("TENANT_ID", "")
    CLIENT_ID     = os.environ.get("BOT_APP_ID", "")
    CLIENT_SECRET = os.environ.get("BOT_APP_PASSWORD", "")
    GRAPH         = "https://graph.microsoft.com/v1.0"

    # ── Requester email always from session ───────────────────────────────────
    hub_ctx = kwargs.get("_hub_ctx") or {}
    user    = hub_ctx.get("user") or {}
    requester_email = user.get("email", "")
    if not requester_email:
        return {"success": False, "error": "Could not determine your email address from session."}

    if not TENANT_ID or not CLIENT_ID or not CLIENT_SECRET:
        return {
            "success": False,
            "error": "Outlook not configured. Set TENANT_ID, BOT_APP_ID, BOT_APP_PASSWORD.",
        }

    max_results = max(1, min(int(max_results), 50))

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _get_token() -> str:
        """Acquire an app-only Graph access token via the client-credentials flow."""
        data = _uparse.urlencode({
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope":         "https://graph.microsoft.com/.default",
        }).encode()
        req = _ureq.Request(
            f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
            data=data, method="POST",
        )
        with _ureq.urlopen(req, timeout=15) as r:
            return json.loads(r.read())["access_token"]

    def _get(url: str, token: str) -> dict:
        """GET a Microsoft Graph URL with a bearer token and return the parsed JSON body."""
        req = _ureq.Request(url, headers={"Authorization": f"Bearer {token}"})
        with _ureq.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def _strip_html(s: str) -> str:
        """Strip HTML tags from a Graph message body, returning plain text."""
        return _re.sub(r"<[^>]+>", "", s or "").strip()

    def _find_custom_folder_id(name: str, token: str) -> str:
        """Resolve a custom folder's Graph id by case-insensitive display-name
        match, searching top-level folders first and then breadth-first
        through child folders (nested folders aren't returned by the
        top-level listing). Returns "" if no match is found."""
        target = name.lower().strip()
        queue = [f"{GRAPH}/users/{requester_email}/mailFolders?$top=250&$select=id,displayName,childFolderCount"]
        visited = 0
        while queue and visited < 50:
            url = queue.pop(0)
            visited += 1
            data = _get(url, token)
            for f in data.get("value", []):
                if (f.get("displayName") or "").lower().strip() == target:
                    return f.get("id", "")
                if f.get("childFolderCount", 0) > 0:
                    queue.append(
                        f"{GRAPH}/users/{requester_email}/mailFolders/{f['id']}"
                        f"/childFolders?$top=250&$select=id,displayName,childFolderCount"
                    )
        return ""

    def _excluded_folder_ids(token: str) -> set:
        """Resolve the Graph ids of Sent Items, Drafts, Deleted Items, and
        Junk Email so a mailbox-wide query can exclude them — those hold
        mail the user sent or discarded, not mail they received, and would
        otherwise pollute a "latest mail" search across every folder."""
        ids = set()
        for name in ("sentitems", "drafts", "deleteditems", "junkemail"):
            try:
                info = _get(f"{GRAPH}/users/{requester_email}/mailFolders/{name}?$select=id", token)
                fid = info.get("id")
                if fid:
                    ids.add(fid)
            except Exception:
                pass
        return ids

    try:
        token = _get_token()

        # ── Resolve well-known folder names ───────────────────────────────────
        folder_map = {
            "inbox":        "inbox",
            "sent":         "sentitems",
            "sentitems":    "sentitems",
            "drafts":       "drafts",
            "deleted":      "deleteditems",
            "deleteditems": "deleteditems",
            "trash":        "deleteditems",
            "archive":      "archive",
            "junk":         "junkemail",
            "spam":         "junkemail",
        }
        requested = (folder or "").lower().strip()
        whole_mailbox = requested in ("", "all", "any", "everywhere", "entire mailbox", "whole mailbox")
        excluded_ids: set = set()
        if whole_mailbox:
            base_url = f"{GRAPH}/users/{requester_email}/messages"
            folder_label = "entire mailbox (excluding Sent, Drafts, Deleted Items, Junk Email)"
            excluded_ids = _excluded_folder_ids(token)
        else:
            folder_id = folder_map.get(requested)
            folder_label = folder_id
            if folder_id is None:
                # Not a well-known name — look it up as a custom folder by display name.
                folder_id = _find_custom_folder_id(folder, token)
                if not folder_id:
                    return {
                        "success": False,
                        "error": (
                            f"No mail folder named '{folder}' was found in "
                            f"{requester_email}'s mailbox."
                        ),
                    }
                folder_label = folder.strip()

            base_url = (
                f"{GRAPH}/users/{requester_email}/mailFolders/{folder_id}/messages"
            )
        select = (
            "id,subject,from,toRecipients,receivedDateTime,"
            "isRead,hasAttachments,bodyPreview,parentFolderId"
        )

        # ── Build KQL $search query for text criteria ─────────────────────────
        # Graph mail $search uses KQL; from:, subject:, body: are valid qualifiers.
        kql_parts = []
        if from_address:
            # Quote multi-word names; bare emails work without quotes
            val = f'"{from_address}"' if " " in from_address else from_address
            kql_parts.append(f"from:{val}")
        if subject_keyword:
            kql_parts.append(f'subject:"{subject_keyword}"')
        if body_keyword:
            kql_parts.append(f'body:"{body_keyword}"')

        # ── Build OData $filter for structural criteria ───────────────────────
        # $filter and $search cannot be combined; use $filter only when no text search.
        filter_parts = []
        if unread_only:
            filter_parts.append("isRead eq false")
        if date_from:
            filter_parts.append(f"receivedDateTime ge {date_from}T00:00:00Z")
        if date_to:
            filter_parts.append(f"receivedDateTime le {date_to}T23:59:59Z")

        # Fetch extra when searching the whole mailbox, since the Sent/Drafts/
        # Deleted/Junk exclusion filter below is applied client-side and would
        # otherwise leave fewer than max_results results after filtering.
        fetch_top = min(max_results * 4, 100) if whole_mailbox else max_results
        params: dict = {"$top": str(fetch_top), "$select": select}

        if kql_parts:
            kql = " AND ".join(kql_parts)
            params["$search"] = f'"{kql}"'
            # $orderby is not supported alongside $search for mail
        else:
            params["$orderby"] = "receivedDateTime desc"
            if filter_parts:
                params["$filter"] = " and ".join(filter_parts)

        url = base_url + "?" + _uparse.urlencode(params)
        data = _get(url, token)
        emails = data.get("value", [])

        # ── Exclude Sent/Drafts/Deleted/Junk from a mailbox-wide query ─────────
        if whole_mailbox and excluded_ids:
            emails = [e for e in emails if e.get("parentFolderId") not in excluded_ids]

        # ── Client-side post-filter when $search was used ─────────────────────
        # Apply date and unread filters the server couldn't do alongside $search.
        if kql_parts and (date_from or date_to or unread_only):
            def _keep(e: dict) -> bool:
                """Apply date_from/date_to/unread_only filters that couldn't be combined with $search."""
                rd = e.get("receivedDateTime", "")[:10]
                if date_from and rd < date_from:
                    return False
                if date_to and rd > date_to:
                    return False
                if unread_only and e.get("isRead", False):
                    return False
                return True
            emails = [e for e in emails if _keep(e)]

        # fetch_top over-fetched for whole-mailbox exclusion filtering above;
        # trim back down to what the caller actually asked for.
        emails = emails[:max_results]

        if not emails:
            criteria = []
            if from_address:    criteria.append(f"from '{from_address}'")
            if subject_keyword: criteria.append(f"subject containing '{subject_keyword}'")
            if body_keyword:    criteria.append(f"body containing '{body_keyword}'")
            if unread_only:     criteria.append("unread only")
            if date_from:       criteria.append(f"from {date_from}")
            if date_to:         criteria.append(f"to {date_to}")
            detail = (", ".join(criteria) + " ") if criteria else ""
            return {
                "success":     True,
                "requester":   requester_email,
                "folder":      folder_label,
                "email_count": 0,
                "transcript":  (
                    f"No emails found in '{folder_label}' "
                    f"{detail}for {requester_email}."
                ),
            }

        # When searching the whole mailbox, resolve each email's actual folder
        # name (only for the folder ids that show up among the results) —
        # otherwise there'd be no way to tell a "Dataload" hit from an inbox one.
        folder_name_cache: dict = {}
        if whole_mailbox:
            for e in emails:
                fid = e.get("parentFolderId")
                if fid and fid not in folder_name_cache:
                    try:
                        info = _get(f"{GRAPH}/users/{requester_email}/mailFolders/{fid}?$select=displayName", token)
                        folder_name_cache[fid] = info.get("displayName", "(unknown)")
                    except Exception:
                        folder_name_cache[fid] = "(unknown)"

        # ── Format transcript ─────────────────────────────────────────────────
        lines = [
            f"Mailbox: {requester_email}",
            f"Folder:  {folder_label}",
            f"Emails:  {len(emails)} (newest first)",
            "",
        ]
        for i, e in enumerate(emails, 1):
            rd = e.get("receivedDateTime", "")[:19].replace("T", " ")
            sender_obj   = (e.get("from") or {}).get("emailAddress") or {}
            sender_name  = sender_obj.get("name", "")
            sender_addr  = sender_obj.get("address", "")
            sender_str   = (
                f"{sender_name} <{sender_addr}>" if sender_name else sender_addr
            )
            to_list = [
                (r.get("emailAddress") or {}).get("address", "")
                for r in (e.get("toRecipients") or [])
            ][:3]
            subject  = e.get("subject") or "(no subject)"
            preview  = (e.get("bodyPreview") or "").strip()[:500]
            is_read  = "Read" if e.get("isRead") else "Unread"
            has_att  = "  [Attachments]" if e.get("hasAttachments") else ""

            lines.append(f"── [{i}] ─────────────────────────────────────────────")
            if whole_mailbox:
                lines.append(f"In folder: {folder_name_cache.get(e.get('parentFolderId'), '(unknown)')}")
            lines.append(f"Date:    {rd}")
            lines.append(f"From:    {sender_str}")
            lines.append(f"To:      {', '.join(to_list) or '—'}")
            lines.append(f"Subject: {subject}{has_att}")
            lines.append(f"Status:  {is_read}")
            lines.append(f"Preview: {preview}")
            lines.append("")

        return {
            "success":     True,
            "requester":   requester_email,
            "folder":      folder_label,
            "email_count": len(emails),
            "transcript":  "\n".join(lines),
        }

    except _uerr.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"success": False, "error": f"Graph API {exc.code}: {body[:400]}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
