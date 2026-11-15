#!/usr/bin/env python3
"""Paperless-ngx auto-classification via the LiteLLM gateway.

Reads untagged documents from Paperless, asks the gateway LLM for a
German classification (Belegart, Korrespondent, tags, Kurzzusammenfassung)
and writes the result back as Paperless tags + a note in the document's
title suffix (never overwrites the original title).

Modes:
  --check            audit: Paperless reachable + token valid + gateway reachable + model present
  --list-untagged    dry run: print which documents would be classified
  --classify ID       classify + tag a single document
  --run              classify + tag all currently untagged documents (idempotent via state file)
  --state PATH       override state file (default scripts/.paperless-classify.state.json)

Environment (never printed):
  PAPERLESS_URL            default https://paperless.home.opensme.graphwiz.ai
  PAPERLESS_API_TOKEN      required for any write; create via Paperless admin UI -> API token
  LITELLM_BASE             default http://127.0.0.1:4000/v1  (host-reachable after port publish)
  LITELLM_API_KEY          gateway bearer (falls back to LITELLM_MASTER_KEY)
  AI_MODEL                 default glm-5.3-flash
  PAPERLESS_CLASSIFY_STATE override state file path

The script is stdlib-only so it runs on the host and inside containers alike.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "https://paperless.home.opensme.graphwiz.ai").rstrip("/")
TOKEN = os.environ.get("PAPERLESS_API_TOKEN", "")
GATEWAY = os.environ.get("LITELLM_BASE", "http://127.0.0.1:4000/v1").rstrip("/")
# Gateway auth: master key first (we talk to the local gateway, not the upstream).
API_KEY = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("LITELLM_API_KEY", "")
MODEL = os.environ.get("AI_MODEL", "glm-5.3-flash")
STATE_FILE = os.environ.get("PAPERLESS_CLASSIFY_STATE", "scripts/.paperless-classify.state.json")
MAX_CONTENT = 3500


def http(method, url, headers=None, data=None, timeout=30):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"detail": raw.decode(errors="replace")[:300]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def paperless_headers():
    return {"Authorization": f"Token {TOKEN}"}


def gateway_chat(prompt):
    if not API_KEY:
        raise RuntimeError("LITELLM_API_KEY/LITELLM_MASTER_KEY not set")
    status, data = http(
        "POST",
        f"{GATEWAY}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        data={"model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False},
        timeout=120,
    )
    if status != 200:
        raise RuntimeError(f"gateway HTTP {status}: {data}")
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"unexpected gateway response: {data}") from e


def list_tags():
    status, data = http("GET", f"{PAPERLESS_URL}/api/tags/?page_size=200", paperless_headers())
    if status != 200:
        raise RuntimeError(f"tags list HTTP {status}: {data}")
    return data.get("results", [])


def get_or_create_tag(name):
    name = name.strip()[:64]
    if not name:
        return None
    for t in list_tags():
        if t.get("name", "").lower() == name.lower():
            return t["id"]
    status, data = http("POST", f"{PAPERLESS_URL}/api/tags/", paperless_headers(), {"name": name, "color": "#888888"})
    if status not in (200, 201):
        raise RuntimeError(f"tag create HTTP {status}: {data}")
    return data["id"]


def fetch_document(doc_id):
    status, data = http("GET", f"{PAPERLESS_URL}/api/documents/{doc_id}/", paperless_headers())
    if status != 200:
        raise RuntimeError(f"document {doc_id} HTTP {status}: {data}")
    return data


def list_untagged(page_size=50):
    status, data = http("GET", f"{PAPERLESS_URL}/api/documents/?page_size={page_size}&ordering=-created", paperless_headers())
    if status != 200:
        raise RuntimeError(f"documents list HTTP {status}: {data}")
    out = []
    for d in data.get("results", []):
        if not d.get("tags"):
            out.append(d)
    return out, data.get("count", 0)


def classify(doc):
    content = (doc.get("content") or "").strip()[:MAX_CONTENT]
    title = doc.get("title") or ""
    prompt = (
        "Du bist ein Assistent für eine kleine deutsche Firma (openSME). "
        "Klassifiziere das folgende Dokument und antworte NUR als JSON-Objekt mit den Schlüsseln "
        '"tags" (Liste von 1-5 kurzen deutschen Schlagwörtern, z.B. Rechnung, Angebot, Vertrag, '
        'Lieferschein, Quittung), "korrespondent" (Name des Absenders/Empfängers oder \"unbekannt\"), '
        '"typ" (Rechnung|Angebot|Vertrag|Lieferschein|Sonstiges) und "zusammenfassung" (ein Satz, Deutsch).\n\n'
        f"Titel: {title}\n\nInhalt:\n{content if content else '(kein Text extrahiert)'}"
    )
    raw = gateway_chat(prompt)
    # tolerate fenced code blocks
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # try to find a JSON object substring
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            parsed = json.loads(text[start:end + 1])
        else:
            raise RuntimeError(f"LLM returned non-JSON: {raw[:200]}")
    tags = [str(t) for t in parsed.get("tags", [])]
    return parsed, tags


def apply_classification(doc_id, tags):
    tag_ids = []
    for name in tags:
        tid = get_or_create_tag(name)
        if tid is not None:
            tag_ids.append(tid)
    if not tag_ids:
        return []
    status, data = http("PATCH", f"{PAPERLESS_URL}/api/documents/{doc_id}/", paperless_headers(), {"tags": tag_ids})
    if status not in (200, 204):
        raise RuntimeError(f"document patch HTTP {status}: {data}")
    return tag_ids


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"done": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def cmd_check(_args):
    print("Paperless-URL:", PAPERLESS_URL)
    print("Gateway:", GATEWAY, "model:", MODEL)
    if not TOKEN:
        print("FEHLER: PAPERLESS_API_TOKEN nicht gesetzt")
        return 1
    status, data = http("GET", f"{PAPERLESS_URL}/api/documents/?page_size=1", paperless_headers())
    if status != 200:
        print(f"FEHLER: Paperless API HTTP {status}: {data}")
        return 1
    print("OK: Paperless API erreichbar, Token gueltig (Dokumente gesamt:", data.get("count", "?"), ")")
    # gateway
    g_status, g_data = http("GET", f"{GATEWAY}/models", {"Authorization": f"Bearer {API_KEY}"})
    if g_status != 200:
        print(f"WARNUNG: Gateway HTTP {g_status}: {g_data}")
        return 0
    ids = [m.get("id") for m in g_data.get("data", [])]
    if MODEL in ids:
        print(f"OK: Gateway erreichbar, Modell '{MODEL}' vorhanden ({len(ids)} Modelle)")
    else:
        print(f"WARNUNG: Modell '{MODEL}' nicht in Gateway-Liste ({len(ids)} Modelle)")
    return 0


def cmd_list_untagged(_args):
    if not TOKEN:
        print("FEHLER: PAPERLESS_API_TOKEN nicht gesetzt", file=sys.stderr)
        return 1
    docs, total = list_untagged()
    print(f"Untagged documents: {len(docs)} (of {total} total)")
    for d in docs:
        print(f"  [{d['id']}] {d.get('title','')[:80]}")


def cmd_classify(args):
    if not TOKEN:
        print("FEHLER: PAPERLESS_API_TOKEN nicht gesetzt", file=sys.stderr)
        return 1
    doc = fetch_document(args.id)
    print(f"Document {args.id}: {doc.get('title')}")
    parsed, tags = classify(doc)
    print("Klassifikation:", json.dumps(parsed, ensure_ascii=False))
    print("Tags:", tags)
    if args.apply:
        ids = apply_classification(args.id, tags)
        print("Applied tag ids:", ids)
        state = load_state()
        if args.id not in state["done"]:
            state["done"].append(args.id)
        save_state(state)
    else:
        print("(dry run — pass --apply to write tags)")
    return 0


def cmd_run(_args):
    if not TOKEN:
        print("FEHLER: PAPERLESS_API_TOKEN nicht gesetzt", file=sys.stderr)
        return 1
    state = load_state()
    done = set(state.get("done", []))
    docs, _ = list_untagged()
    pending = [d for d in docs if d["id"] not in done]
    print(f"Zu klassifizieren: {len(pending)} (von {len(docs)} untagged)")
    for d in pending:
        try:
            parsed, tags = classify(d)
            ids = apply_classification(d["id"], tags)
            print(f"  [{d['id']}] {d.get('title','')[:60]} -> {tags} (tag ids {ids})")
            state["done"].append(d["id"])
            save_state(state)
        except Exception as e:  # noqa: BLE001
            print(f"  [{d['id']}] FEHLER: {e}", file=sys.stderr)
    return 0


def main():
    p = argparse.ArgumentParser(description="Paperless-ngx auto-classification via LiteLLM gateway")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="audit connectivity")
    sub.add_parser("list-untagged", help="dry run")
    c = sub.add_parser("classify", help="classify one document")
    c.add_argument("id", type=int)
    c.add_argument("--apply", action="store_true", help="write tags (default dry run)")
    sub.add_parser("run", help="classify all untagged documents")
    args = p.parse_args()
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "list-untagged":
        return cmd_list_untagged(args)
    if args.cmd == "classify":
        return cmd_classify(args)
    if args.cmd == "run":
        return cmd_run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
