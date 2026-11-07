use axum::{
    extract::State,
    http::{HeaderMap, StatusCode},
    middleware,
    response::{Html, IntoResponse, Json},
    routing::{delete, get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::signal;
use tracing::{info, warn};

mod cloud;
mod feeds;
mod intercom;
mod logout;
mod metrics;
mod polls;
mod session;

#[derive(Clone)]
struct AppConfig {
    opencloud_url: String,
    mail_url: String,
    idp_url: String,
    ticketing_url: String,
    cms_url: String,
    shop_url: String,
    bpm_url: String,
    invoice_url: String,
    paperless_url: String,
    chat_url: String,
    notes_url: String,
    surveys_url: String,
    calendar_url: String,
    vc_url: String,
    portal_domain: String,
    opensme_domain: String,
    announcements: Arc<Vec<Announcement>>,
    ai: Option<AiConfig>,
    ai_client: reqwest::Client,
    intercom: intercom::IntercomConfig,
    intercom_store: Arc<intercom::IntercomStore>,
    intercom_client: reqwest::Client,
    polls: Option<Arc<polls::PollStore>>,
    /// Cloud files bridge (`/api/cloud/*`): `None` (→ 503 on every
    /// bridge route) unless CLOUD_WEBDAV_URL/_USER/_PASS are all set.
    cloud: Option<cloud::CloudFiles>,
    feed_fetcher: Arc<feeds::FeedFetcher>,
    session: session::SessionConfig,
}

#[derive(Clone, Debug, PartialEq)]
struct AiConfig {
    api_url: String,
    model: String,
    api_key: Option<String>,
}

/// Portal "intercom": operator-broadcast banners (info / warn).
#[derive(Clone, Debug, PartialEq, Serialize)]
struct Announcement {
    level: String,
    text: String,
}

#[derive(Serialize)]
struct Service {
    id: String,
    name: String,
    description: String,
    url: String,
}

/// Cap the banner count so a runaway env var can't balloon the page.
const MAX_ANNOUNCEMENTS: usize = 8;

/// HTML-escape a string to prevent XSS in rendered templates.
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

fn load_env(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| {
        warn!(%key, "environment variable not set, using default");
        default.to_string()
    })
}

/// Parse PORTAL_ANNOUNCEMENTS: a JSON array of {"level": "info"|"warn",
/// "text": "…"}. Unset / empty → no banners. Invalid input is logged and
/// ignored (never break the landing page over a cosmetic feature).
fn parse_announcements(raw: &str) -> Vec<Announcement> {
    if raw.trim().is_empty() {
        return Vec::new();
    }
    #[derive(Deserialize)]
    struct RawAnnouncement {
        level: Option<String>,
        text: String,
    }
    match serde_json::from_str::<Vec<RawAnnouncement>>(raw) {
        Ok(items) => items
            .into_iter()
            .filter(|a| !a.text.trim().is_empty())
            .map(|a| Announcement {
                level: if a.level.as_deref() == Some("warn") {
                    "warn".to_string()
                } else {
                    "info".to_string()
                },
                text: a.text,
            })
            .take(MAX_ANNOUNCEMENTS)
            .collect(),
        Err(e) => {
            warn!(%e, "invalid PORTAL_ANNOUNCEMENTS json — ignoring");
            Vec::new()
        }
    }
}

/// Server-render the intercom list into the landing page — progressive
/// enhancement: the page is meaningful without JS; app.js re-renders
/// after each post (textContent, never innerHTML).
fn render_intercom(messages: &[intercom::IntercomMessage]) -> String {
    if messages.is_empty() {
        return r#"<li class="ic-empty">No notes yet — start the conversation.</li>"#.into();
    }
    messages
        .iter()
        .map(|m| {
            let text = html_escape(&m.text);
            // Kind pill: "note" (the default) stays badge-free, info/warn
            // get a fixed literal — never the raw kind string, so nothing
            // unvalidated can reach the page (app.js does the same).
            let kind = match m.kind.as_str() {
                intercom::KIND_INFO => r#"<span class="ic-kind ic-kind-info">INFO</span>"#,
                intercom::KIND_WARN => r#"<span class="ic-kind ic-kind-warn">WARN</span>"#,
                _ => "",
            };
            // Author line — escaped, muted. Local HH:MM is app.js' job
            // (no time crate in tree for SSR civil-time math).
            let meta = m
                .author
                .as_deref()
                .map(html_escape)
                .map(|a| format!(r#"<span class="ic-meta">{a}</span>"#))
                .unwrap_or_default();
            let att = match &m.attachment {
                Some(a) => {
                    let url = html_escape(&a.url);
                    let name = html_escape(&a.name);
                    let size = match a.size {
                        Some(b) if b < 1024 => format!("{b} B"),
                        Some(b) if b < 1024 * 1024 => {
                            format!("{:.1} KB", b as f64 / 1024.0)
                        }
                        Some(b) => format!("{:.1} MB", b as f64 / (1024.0 * 1024.0)),
                        None => String::new(),
                    };
                    let size_attr = if size.is_empty() {
                        String::new()
                    } else {
                        format!(" ({size})")
                    };
                    let ct = html_escape(&a.content_type);
                    format!(
                        r#"<a class="ic-att" href="{url}" target="_blank" rel="noopener noreferrer">📎 {name}{size_attr} · {ct}</a>"#
                    )
                }
                None => String::new(),
            };
            // Dismiss control — inert without JS (app.js binds the DELETE
            // call); the id is a server-side u64, so no escaping surface.
            let del = format!(
                r#"<button class="ic-del" type="button" data-id="{}" aria-label="Löschen">✕</button>"#,
                m.id
            );
            format!(r#"<li>{kind}{meta}{text}{att}{del}</li>"#)
        })
        .collect()
}

fn render_announcements(announcements: &[Announcement]) -> String {
    announcements
        .iter()
        .map(|a| {
            let cls = if a.level == "warn" {
                "announcement warn"
            } else {
                "announcement info"
            };
            let text = html_escape(&a.text);
            format!(r#"<div class="{cls}" role="status"><span class="pill">⚑</span> {text}</div>"#)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Emoji glyph for a service/func card (decorative, never user-controlled).
fn service_icon(id: &str) -> &'static str {
    match id {
        "opencloud" => "☁",
        "webmail" => "✉",
        "support" => "🆘",
        "website" => "🌐",
        "shop" => "🛒",
        "workflow" => "⚙",
        "invoice" => "🧾",
        "archive" => "🗄",
        "chat" => "🗨",
        "notes" => "📋",
        "surveys" => "🗳",
        _ => "🧩",
    }
}

/// One news column card per configured feed column; the client fills the
/// list from /api/feeds (CSP keeps connect-src 'self').
fn render_feed_cards(columns: &[feeds::FeedColumn]) -> String {
    columns
        .iter()
        .map(|c| {
            let cid = html_escape(&c.id);
            let title = html_escape(&c.title);
            format!(
                r#"<div class="feed-card"><h2>{title}</h2><ul id="feed-{cid}" aria-live="polite"><li class="loading">Lade Meldungen…</li></ul></div>"#
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn build_landing_page(config: &AppConfig, authed: bool) -> String {
    let services = get_services(config);
    let cards: String = services
        .iter()
        .map(|s| {
            let icon = service_icon(&s.id);
            let name = html_escape(&s.name);
            let desc = html_escape(&s.description);
            let url = html_escape(&s.url);
            format!(
                r#"<a href="{url}" class="card func-card" data-pal="{name}" target="_blank" rel="noopener noreferrer">
                <span class="func-icon" aria-hidden="true">{icon}</span>
                <h3>{name}</h3>
                <p class="product">{desc}</p>
                <span class="open">Öffnen</span>
            </a>"#
            )
        })
        .collect::<Vec<_>>()
        .join("\n");

    let ai_card = if config.ai.is_some() {
        r#"<div class="card func-card ai" id="ai-card">
                <span class="func-icon" aria-hidden="true">🧠</span>
                <h3>KI-Assistent</h3>
                <p class="product">Fragen an deine Wissensbasis</p>
                <div class="ai-box">
                    <input class="ai-q" type="text" placeholder="Frage stellen…" aria-label="Frage">
                    <button class="ai-ask" type="button">Stellen</button>
                    <p class="ai-out" hidden></p>
                </div>
            </div>"#
    } else {
        ""
    };

    let ic_rendered = render_intercom(&config.intercom_store.list());
    let polls_panel = match &config.polls {
        Some(_) => r#"<section class="tab-panel" id="tab-umfragen" aria-label="Umfragen" hidden>
            <h2 class="section-title">Umfragen</h2>
            <div class="func-grid single">
                <div class="card func-card polls" id="polls-card">
                <span class="func-icon" aria-hidden="true">📊</span>
                <h3>Umfragen</h3>
                <p class="product">Team-Abstimmungen — optional mit Dokument aus der Cloud</p>
                <ul class="pl-list" id="pl-list" aria-live="polite"><li class="ic-empty">Lade Umfragen…</li></ul>
                <details class="pl-new">
                    <summary>Neue Umfrage anlegen</summary>
                    <div class="pl-box">
                        <input class="pl-question" maxlength="300" placeholder="Frage…" aria-label="Frage">
                        <textarea class="pl-options" rows="3" placeholder="Antwortoptionen — eine pro Zeile (min. 2)" aria-label="Antwortoptionen"></textarea>
                        <input class="ic-url pl-url" type="url" placeholder="Anhang aus der Cloud — geteilten Link einfügen (https://cloud…/s/…)" aria-label="Umfrage-Anhang-URL">
                        <div class="ic-actions">
                            <label class="ic-file-label" for="pl-file">📎 Datei…</label>
                            <input class="ic-file" id="pl-file" type="file" aria-label="Umfrage-Anhang" hidden>
                            <span class="ic-file-name" aria-live="polite"></span>
                            <button class="ic-send pl-create" type="button">Anlegen</button>
                        </div>
                        <p class="ic-out pl-out" hidden></p>
                    </div>
                </details>
                </div>
            </div>
        </section>"#
            .to_string(),
        None => String::new(),
    };
    let notizen_panel = format!(
        r#"<section class="tab-panel" id="tab-notizen" aria-label="Notizen" hidden>
            <h2 class="section-title">Notizen</h2>
            <div class="func-grid single">
                <div class="card func-card intercom" id="intercom-card">
                <span class="func-icon" aria-hidden="true">💬</span>
                <h3>Team-Notizen</h3>
                <p class="product">Kurze Notiz für das Team</p>
                <ul class="ic-list" id="ic-list" aria-live="polite">{ic_rendered}</ul>
                <div class="ic-box">
                    <textarea class="ic-text" rows="2" placeholder="Kurze Notiz für das Team…" aria-label="Nachricht" maxlength="2000"></textarea>
                    <input class="ic-url" type="url" placeholder="Anhang aus der Cloud — geteilten Link einfügen (https://cloud…/s/…)" aria-label="Anhang-URL">
                    <div class="ic-actions">
                        <button class="ic-cloud" type="button" title="Datei aus der Cloud auswählen">☁️ aus der Cloud</button>
                        <label class="ic-file-label" for="ic-file">📎 Datei…</label>
                        <input class="ic-file" id="ic-file" type="file" aria-label="Datei anhängen" hidden>
                        <span class="ic-file-name" aria-live="polite"></span>
                        <button class="ic-send" type="button">Senden</button>
                    </div>
                    <p class="ic-out" hidden></p>
                </div>
                </div>
            </div>
        </section>"#
    );
    let umfragen_tab = if config.polls.is_some() {
        r#"<button class="tab-btn" type="button" role="tab" data-tab="umfragen">Umfragen</button>"#
    } else {
        ""
    };

    let announcements = render_announcements(&config.announcements);
    let feed_cards = render_feed_cards(config.feed_fetcher.columns());
    let domain = html_escape(&config.opensme_domain);

    // Signed-in shell: tabs + logout form in the nav, full function grid
    // (cards + AI + polls + notes panels) in the main area.
    let user_nav = if authed {
        format!(
            r#"<button class="tab-btn is-active" type="button" role="tab" aria-selected="true" data-tab="dienste">Dienste</button>
                {umfragen_tab}
                <button class="tab-btn" type="button" role="tab" aria-selected="false" data-tab="notizen">Notizen</button>
                <form class="logout-form" method="post" action="/api/logout">
                    <button class="btn-ghost" type="submit">Abmelden</button>
                </form>"#
        )
    } else {
        // Anonymous: login CTA instead of tabs/logout — no signed-in
        // affordance may leak onto the public landing.
        r#"<a class="btn-ghost login-link" href="/api/auth/login">Anmelden</a>"#.to_string()
    };
    let main_sections = if authed {
        format!(
            r#"<section class="tab-panel is-active" id="tab-dienste" aria-label="Dienste" role="tabpanel">
            <h2 class="section-title">Dienste</h2>
            <div class="func-grid">
                {cards}
                {ai_card}
            </div>
            <h2 class="section-title">Aktuelles</h2>
            <div class="feeds">
                {feed_cards}
            </div>
        </section>
        {polls_panel}
        {notizen_panel}"#
        )
    } else {
        // Public landing (contracts/tiles.yml header): login CTA + public
        // news — the tile catalog is membership content, service links
        // never render here.
        format!(
            r#"<section class="public-landing" aria-label="Anmeldung">
            <h2 class="section-title">Willkommen bei openSME</h2>
            <p class="public-note">Die Funktionen der Suite — Cloud, Mail, Zusammenarbeit,
            Rechnungen, Workflows und mehr — stehen nach der Anmeldung zur Verfügung.</p>
            <p><a class="login-cta" href="/api/auth/login">Mit dem Betriebs-Konto anmelden</a></p>
            <h2 class="section-title">Aktuelles</h2>
            <div class="feeds">
                {feed_cards}
            </div>
        </section>"#
        )
    };
    let script_tag = if authed {
        r#"<script src="/app.js" defer></script>"#
    } else {
        ""
    };

    format!(
        r##"<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>openSME-Portal</title>
    <meta name="description" content="openSME — deine selbst gehostete Suite: Cloud, Mail, Zusammenarbeit, Identität und Workflow-Automatisierung.">
    <meta name="theme-color" content="#005CB9">
    <meta property="og:title" content="openSME-Portal">
    <meta property="og:description" content="Dein Startpunkt für alle Dienste.">
    <meta property="og:type" content="website">
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23005cb9'/%3E%3Ctext x='16' y='22' font-family='system-ui,sans-serif' font-size='18' font-weight='700' fill='white' text-anchor='middle'%3EoS%3C/text%3E%3C/svg%3E">
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self';">
    {script_tag}
    <style>
        :root {{
            --bg: #F3F5F7;
            --card: #FFFFFF;
            --border: #E2E6EA;
            --text: #22272E;
            --text-dim: #5F6B7A;
            --accent: #005CB9;
            --accent-light: #E8F1FB;
            --accent-hover: #E5732D;
            --footer-bg: #003E7C;
            --footer-text: #FFFFFF;
            --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
            --radius: 10px;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: "Helvetica Neue", Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }}
        .container {{ width: 100%; max-width: 1200px; margin: 0 auto; padding: 20px; }}
        main.container {{ flex: 1; }}

        /* Announcements (operator broadcast banners) */
        .announcement {{
            text-align: center;
            padding: 0.6rem 1rem;
            font-size: 0.92rem;
        }}
        .announcement .pill {{ opacity: 0.8; }}
        .announcement.info {{ background: var(--accent-light); color: var(--accent); border-bottom: 1px solid var(--border); }}
        .announcement.warn {{ background: #FFF4E5; color: #B25E00; border-bottom: 1px solid var(--border); }}

        /* Header */
        header {{ background: var(--accent); color: #fff; box-shadow: 0 2px 6px rgba(0,0,0,0.15); }}
        .header-row {{
            display: flex; align-items: center; justify-content: space-between;
            flex-wrap: wrap; gap: 16px; padding: 18px 20px;
        }}
        .brand h1 {{ font-size: 26px; margin-bottom: 2px; letter-spacing: 0.3px; }}
        .brand p {{ opacity: 0.9; font-size: 15px; }}
        .user-nav {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
        .tab-btn {{
            background: transparent; border: 1px solid transparent; color: #fff;
            font: inherit; font-size: 14px; font-weight: 600;
            border-radius: 999px; padding: 7px 16px; cursor: pointer;
            transition: background 0.2s, color 0.2s;
        }}
        .tab-btn:hover {{ background: rgba(255,255,255,0.15); }}
        .tab-btn.is-active {{ background: #fff; color: var(--accent); }}
        .tab-panel[hidden] {{ display: none; }}
        .logout-form {{ margin-left: 10px; }}
        /* Logout must read as a solid action (not a ghost): white on
           #A93226 = 6.62:1, WCAG AA for normal text. The previous thin
           red border on the blue header only reached 1.83:1. */
        .logout-form .btn-ghost {{
            background: #A93226;
            border-color: #A93226;
            color: #fff;
        }}
        .logout-form .btn-ghost:hover {{
            background: #8E1F1F;
            border-color: #8E1F1F;
        }}
        .func-grid.single {{ grid-template-columns: minmax(220px, 420px); justify-content: center; }}
        .btn-ghost {{
            text-decoration: none; font-size: 14px; font-weight: 600;
            border-radius: 999px; padding: 7px 16px; color: #fff;
            border: 1px solid rgba(255,255,255,0.6);
            transition: background 0.2s, color 0.2s;
        }}
        .btn-ghost:hover {{ background: rgba(255,255,255,0.15); }}

        /* Sections */
        .section-title {{ color: var(--accent); font-size: 20px; margin: 26px 0 14px; }}

        /* Service + widget tiles */
        .func-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }}
        .func-card {{
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius); box-shadow: var(--shadow);
            padding: 22px 18px; text-align: center;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
            display: flex; flex-direction: column; gap: 2px;
        }}
        a.func-card {{ text-decoration: none; color: inherit; }}
        .func-card:hover {{ transform: translateY(-3px); box-shadow: 0 6px 14px rgba(0,0,0,0.12); }}
        .func-icon {{
            width: 50px; height: 50px; margin: 0 auto 10px;
            display: grid; place-items: center; font-size: 24px;
            background: var(--accent-light); color: var(--accent); border-radius: 14px;
        }}
        .func-card h3 {{ color: var(--accent); margin-bottom: 2px; font-size: 15px; }}
        .func-card .product {{ color: var(--text-dim); font-size: 13px; font-style: italic; flex: 1; }}
        .func-card .open {{
            display: block; margin-top: auto;
            background: var(--accent); color: #fff;
            padding: 9px 14px; border-radius: 999px;
            text-decoration: none; font-weight: 600; font-size: 14px;
        }}
        a.func-card:hover .open {{ background: var(--accent-hover); }}

        /* Feed columns */
        .feeds {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; margin: 22px 0 4px; }}
        .feed-card {{
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px 18px;
        }}
        .feed-card h2 {{ color: var(--accent); font-size: 16px; margin-bottom: 8px; border-bottom: 2px solid var(--accent-light); padding-bottom: 6px; }}
        .feeds ul {{ list-style: none; padding: 0; }}
        .feeds li {{ padding: 7px 0; border-bottom: 1px solid var(--border); font-size: 13.5px; }}
        .feeds li:last-child {{ border-bottom: none; }}
        .feeds li a {{ color: var(--accent); text-decoration: none; }}
        .feeds li a:hover {{ text-decoration: underline; }}
        .feeds li .date {{ color: var(--text-dim); font-size: 11.5px; display: block; margin-top: 2px; }}
        .loading, .error {{ color: var(--text-dim); font-style: italic; }}
        .error {{ color: #d32f2f; }}

        /* AI box (inside its func-card) */
        .ai-box {{ display: flex; flex-direction: column; gap: 0.6rem; margin-top: 12px; text-align: left; }}
        .ai-q {{
            background: #fff; border: 1px solid var(--border); border-radius: 6px;
            color: var(--text); padding: 8px 10px; font-size: 14px; width: 100%;
        }}
        .ai-ask {{
            align-self: flex-start; background: var(--accent); color: #fff;
            border: none; border-radius: 999px; padding: 7px 16px;
            font-size: 13px; font-weight: 600; cursor: pointer;
        }}
        .ai-ask:hover {{ background: var(--accent-hover); }}
        .ai-out {{ color: var(--text); font-size: 0.92rem; line-height: 1.55; white-space: pre-wrap; }}

        /* Intercom (team notes) */
        .ic-list {{
            list-style: none; display: flex; flex-direction: column; gap: 0.5rem;
            max-height: 16rem; overflow-y: auto; margin-top: 12px; text-align: left;
        }}
        .ic-list li {{ position: relative; color: var(--text); font-size: 0.92rem; line-height: 1.5; word-break: break-word; border-bottom: 1px solid var(--border); padding: 4px 22px 4px 0; }}
        .ic-list li:last-child {{ border-bottom: none; }}
        .ic-list .ic-meta {{ display: block; color: var(--text-muted, #888); font-size: 0.75rem; margin-bottom: 1px; }}
        .ic-list .ic-att {{ display: block; color: var(--accent); font-size: 0.85rem; text-decoration: none; }}
        .ic-list .ic-att:hover {{ text-decoration: underline; }}
        .ic-del {{
            position: absolute; top: 3px; right: 0; margin: 0; padding: 2px 5px;
            background: none; border: none; cursor: pointer; font: inherit;
            font-size: 0.78rem; line-height: 1; color: var(--text-dim);
            border-radius: 4px;
        }}
        .ic-del:hover, .ic-del:focus-visible {{ background: #A93226; color: #fff; }}
        .ic-kind {{
            display: inline-block; font-size: 0.62rem; font-weight: 700;
            letter-spacing: 0.06em; line-height: 1.5; border-radius: 999px;
            padding: 0 7px; margin-bottom: 2px;
        }}
        .ic-kind-info {{ background: var(--accent-light); color: var(--accent); }}
        .ic-kind-warn {{ background: #FFF4E5; color: #B25E00; }}
        .ic-unread {{
            display: inline-block; margin-left: 0.45rem; vertical-align: middle;
            background: var(--accent); color: #fff; border-radius: 999px;
            padding: 1px 8px; font-size: 0.68rem; font-weight: 700;
            line-height: 1.5; white-space: nowrap;
        }}
        .ic-unread[hidden] {{ display: none; }}
        /* The Notizen panel starts hidden — mirror the count on the
           always-visible tab button (light pill: readable on the blue
           header and on the white active tab alike). */
        .tab-btn .ic-unread {{ background: #FFF4E5; color: #B25E00; }}
        .ic-box {{ display: flex; flex-direction: column; gap: 0.6rem; margin-top: 0.75rem; text-align: left; }}
        .ic-text, .ic-url {{
            border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
            font-size: 14px; width: 100%; color: var(--text); background: #fff;
            font-family: inherit; resize: vertical;
        }}
        .ic-send {{
            align-self: flex-start; background: var(--accent); color: #fff;
            border: none; border-radius: 999px; padding: 7px 16px;
            font-size: 13px; font-weight: 600; cursor: pointer;
        }}
        .ic-send:hover {{ background: var(--accent-hover); }}
        .ic-actions {{ display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }}
        .ic-file-label {{
            background: var(--accent-light); color: var(--accent); border-radius: 999px;
            padding: 6px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
        }}
        .ic-file-label:hover {{ background: #d7e7f8; }}
        /* ☁️ "aus der Cloud": opens /files?picker=1 in a new tab; same
           pill styling as the file label so the row reads as one group. */
        .ic-cloud {{
            background: var(--accent-light); color: var(--accent); border: none;
            border-radius: 999px; padding: 6px 14px; font-size: 13px;
            font-weight: 600; cursor: pointer; font-family: inherit;
        }}
        .ic-cloud:hover {{ background: #d7e7f8; }}
        .ic-file-name {{ color: var(--text-dim); font-size: 0.85rem; }}
        .ic-out {{ color: var(--text-dim); font-size: 0.88rem; }}

        /* Polls (Umfragen) — optional card */
        .pl-list {{ list-style: none; padding: 0; margin: 0.5rem 0; text-align: left; display: flex; flex-direction: column; gap: 0.9rem; }}
        .poll-q {{ font-weight: 600; font-size: 0.95rem; margin-bottom: 0.4rem; }}
        .poll-att {{ display: inline-block; font-size: 0.85rem; margin-bottom: 0.4rem; }}
        .poll-opts {{ display: flex; flex-direction: column; gap: 0.35rem; }}
        .poll-opt {{
            position: relative; overflow: hidden; display: flex; align-items: center;
            justify-content: space-between; gap: 0.5rem;
            border: 1px solid var(--border); border-radius: 6px; padding: 7px 10px;
            font-size: 13px; background: #fff;
        }}
        .poll-fill {{ position: absolute; inset: 0; background: var(--accent-light); width: 0; }}
        .poll-opt > * {{ position: relative; }}
        .poll-label {{ flex: 1; text-align: left; word-break: break-word; }}
        .poll-count {{ color: var(--text-dim); font-variant-numeric: tabular-nums; white-space: nowrap; }}
        .poll-vote {{
            border: none; background: none; font: inherit; font-weight: 600;
            color: var(--accent); cursor: pointer; white-space: nowrap; padding: 0;
        }}
        .poll-vote:hover {{ text-decoration: underline; }}
        .pl-new {{ margin-top: 0.6rem; text-align: left; }}
        .pl-new summary {{ cursor: pointer; color: var(--accent); font-size: 0.9rem; font-weight: 600; }}
        .pl-box {{ display: flex; flex-direction: column; gap: 0.5rem; margin-top: 0.6rem; }}
        .pl-question, .pl-options {{
            border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
            font-size: 14px; width: 100%; background: #fff; font-family: inherit;
            box-sizing: border-box; resize: vertical;
        }}

        /* Quick-switch palette (Ctrl+K) */
        #palette {{
            position: fixed; inset: 0; background: rgba(0, 30, 60, 0.45);
            display: flex; align-items: flex-start; justify-content: center;
            padding-top: 14vh; z-index: 40;
        }}
        #palette[hidden] {{ display: none; }}
        .pal-box {{
            width: min(520px, 92vw); background: #fff;
            border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
            box-shadow: 0 24px 64px rgba(0, 0, 0, 0.25);
        }}
        #pal-input {{
            width: 100%; background: transparent; border: none;
            border-bottom: 1px solid var(--border); color: var(--text);
            font-size: 1.05rem; padding: 0.9rem 1.1rem; outline: none;
        }}
        #pal-list {{ list-style: none; max-height: 46vh; overflow-y: auto; }}
        #pal-list li {{ padding: 0.6rem 1.1rem; color: var(--text); cursor: pointer; font-size: 0.98rem; }}
        #pal-list li.sel {{ background: var(--accent-light); color: var(--accent); }}
        #pal-list li.none {{ color: var(--text-dim); cursor: default; }}
        .pal-hint {{ padding: 0.5rem 1.1rem 0.7rem; color: var(--text-dim); font-size: 0.78rem; border-top: 1px solid var(--border); }}
        #pal-hint {{
            position: fixed; right: 1.1rem; bottom: 1.1rem;
            background: var(--accent); color: #fff; border: none;
            border-radius: 999px; font-size: 0.8rem; font-weight: 600;
            padding: 0.4rem 0.7rem; cursor: pointer; z-index: 30;
        }}
        #pal-hint:hover {{ background: var(--accent-hover); }}

        footer {{
            background: var(--footer-bg); color: var(--footer-text);
            padding: 22px 0; text-align: center; font-size: 14px; margin-top: 28px;
        }}
        footer a {{ color: var(--footer-text); text-decoration: none; opacity: 0.85; }}
        footer a:hover {{ opacity: 1; text-decoration: underline; }}
        footer .row {{ margin-top: 8px; }}

        @media (max-width: 640px) {{
            .header-row {{ flex-direction: column; align-items: flex-start; }}
            .brand h1 {{ font-size: 22px; }}
        }}
    </style>
</head>
<body>
    {announcements}
    <header>
        <div class="container header-row">
            <div class="brand">
                <h1>openSME</h1>
                <p>Dein Startpunkt für alle Dienste</p>
            </div>
            <nav class="user-nav" aria-label="Navigation">
                {user_nav}
            </nav>
        </div>
    </header>
    <main class="container">
        {main_sections}
    </main>
    <footer>
        <div class="container">
            <p>openSME — die selbst gehostete Suite für kleine und mittlere Unternehmen.</p>
            <p class="row">Support &amp; Consulting: <a href="mailto:hello@opensme.org">hello@opensme.org</a> &middot; {domain}</p>
        </div>
    </footer>
</body>
</html>"##
    )
}

/// Return only services that have a non-default URL (i.e. actually configured).
fn get_services(config: &AppConfig) -> Vec<Service> {
    let mut services = Vec::new();

    // Catalog functions only — infrastructure (identity/auth, embedded
    // editors, mail backends) never gets a tile; see openspec capability
    // portal-catalog.

    // OpenCloud — only if URL differs from the raw default
    if !config.opencloud_url.is_empty() {
        services.push(Service {
            id: "opencloud".into(),
            name: "OpenCloud".into(),
            description: "Cloud storage, file sharing and collaboration".into(),
            url: config.opencloud_url.clone(),
        });
    }

    // Webmail — only if explicitly configured (not empty)
    if !config.mail_url.is_empty() {
        services.push(Service {
            id: "webmail".into(),
            name: "Webmail".into(),
            description: "Email, calendar and contacts".into(),
            url: config.mail_url.clone(),
        });
    }

    // Helpdesk — only if explicitly configured (not empty)
    if !config.ticketing_url.is_empty() {
        services.push(Service {
            id: "support".into(),
            name: "Support".into(),
            description: "Helpdesk — tickets and knowledge base".into(),
            url: config.ticketing_url.clone(),
        });
    }

    // Website CMS — only if explicitly configured (not empty)
    if !config.cms_url.is_empty() {
        services.push(Service {
            id: "website".into(),
            name: "Website".into(),
            description: "Content management — pages, news and blog".into(),
            url: config.cms_url.clone(),
        });
    }

    // Store — only if explicitly configured (not empty)
    if !config.shop_url.is_empty() {
        services.push(Service {
            id: "shop".into(),
            name: "Shop".into(),
            description: "Storefront — products, cart and checkout".into(),
            url: config.shop_url.clone(),
        });
    }

    // Workflow (Operaton) — only if explicitly configured (not empty)
    if !config.bpm_url.is_empty() {
        services.push(Service {
            id: "workflow".into(),
            name: "Workflow".into(),
            description: "BPMN process engine — Cockpit, Tasklist and REST API".into(),
            url: config.bpm_url.clone(),
        });
    }

    // Invoice (Invoice Ninja) — only if explicitly configured (not empty)
    if !config.invoice_url.is_empty() {
        services.push(Service {
            id: "invoice".into(),
            name: "Invoice".into(),
            description: "Invoicing — quotes, invoices and payments".into(),
            url: config.invoice_url.clone(),
        });
    }

    // Archive (Paperless-ngx) — only if explicitly configured (not empty)
    if !config.paperless_url.is_empty() {
        services.push(Service {
            id: "archive".into(),
            name: "Archive".into(),
            description: "Document archive — full-text searchable records".into(),
            url: config.paperless_url.clone(),
        });
    }

    // Chat (Element/Synapse) — only if explicitly configured (not empty)
    if !config.chat_url.is_empty() {
        services.push(Service {
            id: "chat".into(),
            name: "Chat".into(),
            description: "Team messaging — channels and direct messages".into(),
            url: config.chat_url.clone(),
        });
    }

    // Notes (Impress) — only if explicitly configured (not empty)
    if !config.notes_url.is_empty() {
        services.push(Service {
            id: "notes".into(),
            name: "Notes".into(),
            description: "Shared notes — links and snippets for the team".into(),
            url: config.notes_url.clone(),
        });
    }

    // Surveys (LimeSurvey CE) — only if explicitly configured (not empty)
    if !config.surveys_url.is_empty() {
        services.push(Service {
            id: "surveys".into(),
            name: "Surveys".into(),
            description: "Online surveys — LimeSurvey".into(),
            url: config.surveys_url.clone(),
        });
    }

    // Calendar (calrs booking) — only if explicitly configured (not empty)
    if !config.calendar_url.is_empty() {
        services.push(Service {
            id: "calendar".into(),
            name: "Calendar".into(),
            description: "Video consultations — book a slot".into(),
            url: config.calendar_url.clone(),
        });
    }

    // VC (LiveKit meetings) — only if explicitly configured (not empty)
    if !config.vc_url.is_empty() {
        services.push(Service {
            id: "vc".into(),
            name: "Meet".into(),
            description: "Video meetings — self-hosted".into(),
            url: config.vc_url.clone(),
        });
    }

    services
}

async fn handle_root(
    State(config): State<Arc<AppConfig>>,
    headers: HeaderMap,
) -> impl IntoResponse {
    // Session-aware landing: the tile grid is membership content
    // (contracts/tiles.yml, decision 2026-11-01) — anonymous visitors get
    // the login CTA + public news only, never service links or the
    // logout affordance. A stale/garbage cookie simply reads as anonymous.
    let authed = session::cookie_value(&headers, session::PORTAL_SESSION_COOKIE)
        .and_then(|tok| session::verify_session(&config.session, &tok))
        .is_some();
    let html = build_landing_page(&config, authed);
    Html(html)
}

async fn handle_health() -> impl IntoResponse {
    Json(serde_json::json!({"status": "ok"}))
}

/// Prometheus scrape endpoint (text exposition format).
async fn handle_metrics() -> impl IntoResponse {
    match metrics::global().render() {
        Ok(body) => (
            StatusCode::OK,
            [(
                axum::http::header::CONTENT_TYPE,
                "text/plain; version=0.0.4; charset=utf-8",
            )],
            body,
        ),
        Err(e) => {
            warn!(%e, "metrics render failed");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                [(
                    axum::http::header::CONTENT_TYPE,
                    "text/plain; charset=utf-8",
                )],
                format!("metrics gather error: {e}"),
            )
        }
    }
}

async fn handle_services(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    let services = get_services(&config);
    Json(serde_json::json!({ "services": services }))
}

async fn handle_announcements(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    Json(serde_json::json!({ "announcements": config.announcements.as_ref() }))
}

/// Same-origin news columns: server-side RSS aggregation (see feeds.rs).
/// Always 200 — a broken upstream yields an empty column, never an error.
async fn handle_feeds(State(config): State<Arc<AppConfig>>) -> impl IntoResponse {
    Json(serde_json::json!({ "columns": config.feed_fetcher.snapshot().await }))
}

#[derive(Deserialize)]
struct AiChatRequest {
    question: String,
}

#[derive(Deserialize)]
struct ChatChoiceMessage {
    content: Option<String>,
}

#[derive(Deserialize)]
struct ChatChoice {
    message: Option<ChatChoiceMessage>,
}

#[derive(Deserialize)]
struct ChatResponse {
    choices: Vec<ChatChoice>,
}

/// Same-origin proxy to the operator-configured OpenAI-compatible endpoint.
/// Only mounted when AI_API_URL is set (card and endpoint stay hidden
/// otherwise). Stateless by design: one question in, one answer out.
async fn handle_ai_chat(
    State(config): State<Arc<AppConfig>>,
    Json(req): Json<AiChatRequest>,
) -> impl IntoResponse {
    let Some(ai) = config.ai.as_ref() else {
        return (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({
                "error": "AI integration not configured"
            })),
        );
    };

    let question = req.question.trim().to_string();
    if question.is_empty() || question.len() > 4000 {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "question must be 1-4000 chars" })),
        );
    }

    let url = format!("{}/v1/chat/completions", ai.api_url.trim_end_matches('/'));
    let mut builder = config.ai_client.post(&url).json(&serde_json::json!({
        "model": ai.model,
        "messages": [{ "role": "user", "content": question }],
        "max_tokens": 512,
    }));
    if let Some(key) = ai.api_key.as_deref().filter(|k| !k.is_empty()) {
        builder = builder.bearer_auth(key);
    }

    match builder.send().await {
        Ok(resp) if resp.status().is_success() => match resp.json::<ChatResponse>().await {
            Ok(body) => {
                let answer = body
                    .choices
                    .first()
                    .and_then(|c| c.message.as_ref())
                    .and_then(|m| m.content.clone())
                    .unwrap_or_default();
                (
                    StatusCode::OK,
                    Json(serde_json::json!({ "answer": answer })),
                )
            }
            Err(e) => (
                StatusCode::BAD_GATEWAY,
                Json(serde_json::json!({ "error": format!("upstream decode error: {e}") })),
            ),
        },
        Ok(resp) => (
            StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({
                "error": format!("upstream returned {}", resp.status())
            })),
        ),
        Err(e) => (
            StatusCode::BAD_GATEWAY,
            Json(serde_json::json!({ "error": format!("upstream unreachable: {e}") })),
        ),
    }
}

/// Security headers on every response (belt-and-braces with the CSP meta
/// tag in the HTML — headers also cover the JSON API responses).
async fn security_headers(
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let mut res = next.run(request).await;
    let h = res.headers_mut();
    h.insert("x-content-type-options", "nosniff".parse().unwrap());
    h.insert("x-frame-options", "DENY".parse().unwrap());
    // HSTS: the portal is served TLS-only (traefik redirects :80 → :443),
    // so telling browsers to refuse plain-http for a year is always safe
    // here — and it covers the JSON API responses too.
    h.insert(
        "strict-transport-security",
        "max-age=31536000; includeSubDomains".parse().unwrap(),
    );
    h.insert(
        "referrer-policy",
        "strict-origin-when-cross-origin".parse().unwrap(),
    );
    h.insert(
        "content-security-policy",
        "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self'"
            .parse()
            .unwrap(),
    );
    res
}

/// Prometheus instrumentation for every served request: one counter
/// increment + one latency observation, labelled by method, matched route
/// and status code (see metrics.rs).
async fn track_metrics(
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    let start = std::time::Instant::now();
    let method = request.method().to_string();
    // MatchedPath is populated by routing before router-level middleware
    // runs, so the route label is the route pattern, not the raw URI.
    let route = request
        .extensions()
        .get::<axum::extract::MatchedPath>()
        .map(|p| p.as_str().to_string())
        .unwrap_or_else(|| request.uri().path().to_string());
    let response = next.run(request).await;
    metrics::global().record_request(
        &method,
        &route,
        response.status().as_u16(),
        start.elapsed().as_secs_f64(),
    );
    response
}

fn build_router(config: Arc<AppConfig>) -> Router {
    // NO permissive CORS layer: every browser path is same-origin (the
    // Next.js portal-web proxies /api/* server-side, and the legacy Rust UI
    // serves its own /app.js from the same origin). allow_origin(Any) here
    // previously let any website read /api/* responses whenever the backend
    // was directly reachable (e.g. a mis-exposed container port). Same-origin
    // only: no Access-Control-Allow-* headers are emitted at all.
    let router = Router::new()
        .route("/", get(handle_root))
        .route("/app.js", get(handle_app_js))
        .route("/health", get(handle_health))
        .route("/api/announcements", get(handle_announcements))
        .route("/api/feeds", get(handle_feeds))
        .route("/api/auth/login", get(session::handle_login))
        .route("/api/session", get(session::handle_session))
        .route("/api/auth/callback", get(session::handle_callback))
        .route("/api/logout", post(logout::handle_logout))
        .route("/logged-out", get(logout::handle_logged_out));

    // Authenticated surfaces: intercom, polls, the service-tile catalog,
    // the AI chat proxy and their upload/file routes sit behind the
    // signed-session firewall. The public landing page and the news
    // surfaces (feeds + announcements) stay anonymous — they are public
    // content shown to every visitor. The tile catalog (/api/services) is
    // only meaningful for signed-in members: the Next.js page probes
    // /api/session first and skips the catalog fetch entirely when
    // anonymous, so the gate causes no anonymous-page breakage.
    let mut protected = Router::new()
        .route("/api/intercom", get(handle_intercom_list))
        .route("/api/intercom", post(handle_intercom_post))
        .route("/api/intercom/upload", post(handle_intercom_upload))
        .route("/api/intercom/:id", delete(handle_intercom_delete))
        .route(
            "/api/intercom/files/:token/:name",
            get(handle_intercom_file),
        )
        .route("/api/services", get(handle_services))
        // Cloud files bridge — registered UNCONDITIONALLY so a missing
        // cloud config surfaces as a clean 503 "not configured" instead
        // of a bare 404; anonymous callers are stopped by the session
        // firewall below (401 runs first, before any handler logic).
        .route(
            "/api/cloud/files",
            get(handle_cloud_list)
                .post(handle_cloud_mkdir)
                .put(handle_cloud_upload),
        )
        .route(
            "/api/cloud/file",
            get(handle_cloud_download).delete(handle_cloud_delete),
        )
        .route("/api/cloud/share", post(handle_cloud_share))
        // Cloud picker page (browse + upload + select; ?picker=1 turns
        // it into the cross-tab attachment picker). Session-gated like
        // the bridge it drives — anonymous callers get 401 from the
        // firewall below.
        .route("/files", get(handle_files));

    // The AI proxy is the one METERED endpoint (the operator pays per
    // upstream token) — it must sit behind the session firewall like
    // every other interactive surface, or anyone who reaches the edge
    // can run a cost-DoS with anonymous 4000-char questions. Anonymous
    // callers get 401; the cards' fetch sends cookies (same-origin), so
    // signed-in users are unaffected.
    if config.ai.is_some() {
        protected = protected.route("/api/ai/chat", post(handle_ai_chat));
    }

    if config.polls.is_some() {
        protected = protected
            .route("/api/polls", get(handle_polls_list))
            .route("/api/polls", post(handle_polls_create))
            .route("/api/polls/upload", post(handle_polls_upload))
            .route("/api/polls/:id/vote", post(handle_polls_vote))
            .route("/api/files/:token/:name", get(handle_polls_file));
    }

    // The session firewall is scoped to the protected sub-router only —
    // every other route on the portal stays public.
    protected = protected.route_layer(middleware::from_fn_with_state(
        config.clone(),
        session::require_session,
    ));

    router
        .merge(protected)
        .route("/metrics", get(handle_metrics))
        // Picked-file uploads arrive as raw bodies; axum's DefaultBodyLimit
        // (2 MiB) would silently 413 everything between 2 and 5 MiB before
        // the handler's own MAX_UPLOAD check could produce a clean 400.
        // One shared limit — upload routes + JSON posts live here together.
        .layer(axum::extract::DefaultBodyLimit::max(
            intercom::MAX_UPLOAD as usize + 1024,
        ))
        .layer(middleware::from_fn(security_headers))
        .layer(middleware::from_fn(track_metrics))
        .with_state(config)
}

/// Portal intercom: a short internal note, optionally referencing one
/// attachment from the cloud service (metadata snapshot only). `kind`
/// is the optional severity ("note" | "info" | "warn", default note).
#[derive(Deserialize)]
struct IntercomPost {
    // default: a missing text must hit the store's "message text is
    // empty" 400, not axum's raw 422 deserializer rejection
    #[serde(default)]
    text: String,
    #[serde(default)]
    attachment_url: Option<String>,
    #[serde(default)]
    kind: Option<String>,
}

/// Polling-friendly listing: `?since=<id>` returns only newer messages
/// (delta), no param = the full newest-first list. Response also carries
/// `latest` so clients can initialize their cursor without a second hit.
#[derive(Deserialize)]
struct IntercomListQuery {
    #[serde(default)]
    since: Option<u64>,
}

async fn handle_intercom_list(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<IntercomListQuery>,
) -> impl IntoResponse {
    let messages = match q.since {
        Some(since) => config.intercom_store.list_since(since),
        None => config.intercom_store.list(),
    };
    Json(serde_json::json!({
        "messages": messages,
        "latest": config.intercom_store.latest_id(),
    }))
}

/// Team-notes persistence layout: INTERCOM_DATA_PATH holds the JSON store,
/// picked-file uploads live in "attachments/" next to it.
fn intercom_store_from_env() -> intercom::IntercomStore {
    match std::env::var("INTERCOM_DATA_PATH") {
        Ok(path) if !path.trim().is_empty() => {
            let path = PathBuf::from(path.trim());
            let dir = path.parent().map(|p| p.to_path_buf()).unwrap_or_default();
            let attachments = dir.join("attachments");
            if let Err(e) = std::fs::create_dir_all(&attachments) {
                // Usually a volume ownership problem (portal runs as uid 1000);
                // notes still work, picked-file uploads will 400 until fixed.
                tracing::warn!(%e, dir = %attachments.display(), "cannot create intercom attachments dir — file picker uploads will fail");
            }
            intercom::IntercomStore::open(Some(path), Some(attachments))
        }
        _ => intercom::IntercomStore::default(),
    }
}

#[derive(Deserialize)]
struct IntercomUploadQuery {
    text: String,
    #[serde(default)]
    filename: Option<String>,
    #[serde(default)]
    kind: Option<String>,
}

/// File-picker upload: raw body (no multipart feature), metadata in the
/// query. Posts the note directly with the stored attachment.
async fn handle_intercom_upload(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    axum::extract::Query(q): axum::extract::Query<IntercomUploadQuery>,
    headers: axum::http::HeaderMap,
    bytes: axum::body::Bytes,
) -> impl IntoResponse {
    let bad = |msg: &str| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": msg })),
        )
    };
    let text = match intercom::validate_text(&q.text) {
        Ok(t) => t,
        Err(e) => return bad(&e).into_response(),
    };
    let kind = match intercom::validate_kind(q.kind.as_deref()) {
        Ok(k) => k,
        Err(e) => return bad(&e).into_response(),
    };
    let filename = q
        .filename
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .or_else(|| {
            headers
                .get(axum::http::header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .and_then(|ct| ct.rsplit('/').next())
                .map(|ext| format!("upload.{ext}"))
        })
        .unwrap_or_else(|| "upload.bin".into());
    let content_type = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .split(';')
        .next()
        .unwrap_or("application/octet-stream")
        .trim()
        .to_string();
    let (token, name) = match config.intercom_store.save_attachment(&filename, &bytes) {
        Ok(pair) => pair,
        Err(e) => return bad(&e).into_response(),
    };
    let attachment = intercom::Attachment {
        url: format!("/api/intercom/files/{token}/{name}"),
        name,
        size: Some(bytes.len() as u64),
        content_type,
    };
    let msg = config
        .intercom_store
        .push(text, Some(session.name.clone()), Some(attachment), kind);
    metrics::global().inc_intercom_message();
    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "message": msg })),
    )
        .into_response()
}

/// Moderation delete: remove one note by id. 204 on success (no body),
/// 404 when the id is unknown. Same session firewall as every other
/// intercom surface — only signed-in members reach this route.
async fn handle_intercom_delete(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Path(id): axum::extract::Path<u64>,
) -> impl IntoResponse {
    if config.intercom_store.delete(id) {
        (StatusCode::NO_CONTENT, "").into_response()
    } else {
        (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "message not found" })),
        )
            .into_response()
    }
}

/// Serve a picked-file attachment. Body from disk, content-type from the
/// stored snapshot; never trusts the request path beyond token+name.
async fn handle_intercom_file(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Path((token, name)): axum::extract::Path<(String, String)>,
) -> impl IntoResponse {
    use axum::http::header::{CACHE_CONTROL, CONTENT_DISPOSITION, CONTENT_TYPE};
    match config.intercom_store.read_attachment(&token, &name) {
        Some((bytes, ct)) => {
            // Active content (svg/html/xml…) is never inline: it would
            // run attacker JS in the portal's own origin (stored XSS).
            let disposition = if intercom::serve_inline(&name) {
                format!("inline; filename=\"{name}\"")
            } else {
                format!("attachment; filename=\"{name}\"")
            };
            // Token-addressed immutable content — safe to cache hard.
            // `private`: the route is session-gated, shared caches must
            // never store it.
            (
                [
                    (CONTENT_TYPE, ct),
                    (CONTENT_DISPOSITION, disposition),
                    (
                        CACHE_CONTROL,
                        "private, max-age=31536000, immutable".to_string(),
                    ),
                ],
                bytes,
            )
                .into_response()
        }
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

// ── Polls (Umfragen) — optional feature behind POLLS_ENABLED=1 ────────

fn polls_store_from_env(http: &reqwest::Client) -> Option<Arc<polls::PollStore>> {
    let enabled = std::env::var("POLLS_ENABLED")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    if !enabled {
        return None;
    }
    let path = std::env::var("POLLS_DATA_PATH")
        .ok()
        .map(|p| p.trim().to_string())
        .filter(|p| !p.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/data/polls.json"));
    let dir = path.parent().map(|p| p.to_path_buf()).unwrap_or_default();
    let attachments = dir.join("attachments");
    if let Err(e) = std::fs::create_dir_all(&attachments) {
        tracing::warn!(%e, dir = %attachments.display(), "cannot create polls attachments dir — local uploads will fail");
    }
    // Cloud uploads ride the same wired client as the rest of the portal
    // (timeout, no redirects, egress proxy with internal-host bypass).
    let cloud = cloud::CloudFiles::from_env().map(|c| c.with_client(http.clone()));
    tracing::info!(cloud = cloud.is_some(), "polls feature enabled");
    Some(Arc::new(polls::PollStore::open(
        Some(path),
        Some(attachments),
        cloud,
    )))
}

#[derive(Deserialize)]
struct PollPost {
    // defaults: missing question/options must reach the store's clean
    // 400s ("question is empty" / "needs 2..10 options"), not a raw 422
    #[serde(default)]
    question: String,
    #[serde(default)]
    options: Vec<String>,
    /// Cloud share link (https://cloud…/s/…) — metadata snapshot only.
    #[serde(default)]
    attachment_url: Option<String>,
}

#[derive(Deserialize)]
struct PollVotePost {
    // i64 instead of usize: a negative option ("option": -1) must hit
    // the handler's JSON 400, not axum's raw 422 usize rejection
    option: i64,
}

#[derive(Deserialize)]
struct PollUploadQuery {
    question: String,
    /// JSON-encoded array of option strings (client uses encodeURIComponent).
    options: String,
    #[serde(default)]
    filename: Option<String>,
    /// Cloud share link — alternative to a picked-file upload body.
    #[serde(default)]
    attachment_url: Option<String>,
}

async fn handle_polls_list(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
) -> impl IntoResponse {
    match &config.polls {
        Some(store) => Json(serde_json::json!({ "polls": store.list_public(Some(&session.sub)) }))
            .into_response(),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn handle_polls_create(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    Json(post): Json<PollPost>,
) -> impl IntoResponse {
    let Some(store) = &config.polls else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let bad = |msg: &str| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": msg})),
        )
    };
    let (question, options) = match polls::validate_poll(&post.question, &post.options) {
        Ok(pair) => pair,
        Err(e) => return bad(&e).into_response(),
    };
    let attachment =
        match resolve_cloud_attachment(&config, post.attachment_url.as_deref().unwrap_or("")).await
        {
            Ok(a) => a,
            Err(e) => return bad(&e).into_response(),
        };
    let poll = store.create(question, options, attachment, Some(session.name.clone()));
    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "poll": poll.public(None) })),
    )
        .into_response()
}

/// Create a poll carrying a picked-file attachment (raw body upload).
/// With CLOUD_WEBDAV_* configured the file goes to the cloud (WebDAV PUT
/// + public share link); otherwise it falls back to the local dir.
async fn handle_polls_upload(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    axum::extract::Query(q): axum::extract::Query<PollUploadQuery>,
    headers: axum::http::HeaderMap,
    bytes: axum::body::Bytes,
) -> impl IntoResponse {
    let Some(store) = &config.polls else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let bad = |msg: &str| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({"error": msg})),
        )
    };
    let options: Vec<String> = match serde_json::from_str(&q.options) {
        Ok(v) => v,
        Err(_) => return bad("options must be a JSON array string").into_response(),
    };
    let (question, options) = match polls::validate_poll(&q.question, &options) {
        Ok(pair) => pair,
        Err(e) => return bad(&e).into_response(),
    };
    let attachment = if let Some(url) = q
        .attachment_url
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        match resolve_cloud_attachment(&config, url).await {
            Ok(a) => a,
            Err(e) => return bad(&e).into_response(),
        }
    } else if bytes.is_empty() {
        None
    } else {
        let filename = q
            .filename
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .unwrap_or("upload.bin")
            .to_string();
        match &store.cloud {
            Some(cloud) => {
                let key = intercom::random_token();
                match polls::upload_to_cloud(cloud, &key, &filename, &bytes).await {
                    Ok(a) => Some(a),
                    Err(e) => return bad(&e).into_response(),
                }
            }
            None => {
                let content_type = headers
                    .get(axum::http::header::CONTENT_TYPE)
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or("application/octet-stream")
                    .split(';')
                    .next()
                    .unwrap_or("application/octet-stream")
                    .trim()
                    .to_string();
                match store.save_attachment_local(&filename, &bytes) {
                    Ok((token, name)) => Some(intercom::Attachment {
                        url: format!("/api/files/{token}/{name}"),
                        name,
                        size: Some(bytes.len() as u64),
                        content_type,
                    }),
                    Err(e) => return bad(&e).into_response(),
                }
            }
        }
    };
    let poll = store.create(question, options, attachment, Some(session.name.clone()));
    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "poll": poll.public(None) })),
    )
        .into_response()
}

async fn handle_polls_vote(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    axum::extract::Path(id): axum::extract::Path<u64>,
    Json(post): Json<PollVotePost>,
) -> impl IntoResponse {
    let Some(store) = &config.polls else {
        return StatusCode::NOT_FOUND.into_response();
    };
    if post.option < 0 {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "option must be >= 0" })),
        )
            .into_response();
    }
    match store.vote(id, post.option as usize, Some(&session.sub)) {
        Ok(poll) => Json(serde_json::json!({
            "poll": poll.public(Some(&session.sub))
        }))
        .into_response(),
        Err(e) => {
            // "already voted" is a conflict (the poll state is fine, the
            // request just cannot be applied) — clients show a friendly
            // hint instead of a generic 400.
            let status = if e.contains("already voted") {
                StatusCode::CONFLICT
            } else {
                StatusCode::BAD_REQUEST
            };
            (status, Json(serde_json::json!({"error": e}))).into_response()
        }
    }
}

/// Serve a local-fallback poll attachment (same guards as intercom).
async fn handle_polls_file(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Path((token, name)): axum::extract::Path<(String, String)>,
) -> impl IntoResponse {
    use axum::http::header::{CACHE_CONTROL, CONTENT_DISPOSITION, CONTENT_TYPE};
    let Some(store) = &config.polls else {
        return StatusCode::NOT_FOUND.into_response();
    };
    match store.read_attachment_local(&token, &name) {
        Some((bytes, ct)) => {
            // Same active-content rule as intercom (shared mime helper).
            let disposition = if intercom::serve_inline(&name) {
                format!("inline; filename=\"{name}\"")
            } else {
                format!("attachment; filename=\"{name}\"")
            };
            (
                [
                    (CONTENT_TYPE, ct),
                    (CONTENT_DISPOSITION, disposition),
                    (
                        CACHE_CONTROL,
                        "private, max-age=31536000, immutable".to_string(),
                    ),
                ],
                bytes,
            )
                .into_response()
        }
        None => StatusCode::NOT_FOUND.into_response(),
    }
}

// ── Cloud files bridge (/api/cloud/*) ────────────────────────────────
//
// Thin wrappers over `cloud::CloudFiles`: relative paths/names are
// validated offline FIRST (400 before any network call — the pure
// helpers live in cloud.rs and are unit-tested there), a missing cloud
// config answers 503, and upstream errors are mapped through
// `cloud_error` (404/409/400/502 passthrough). The routes live in the
// `protected` router, so anonymous callers get 401 from the session
// firewall before any of this runs. Absolute paths under the dav root
// are built by the module's encoder from these relative inputs —
// callers never supply `remote.php/...` text.

/// One relative path (list directory / download+delete file).
#[derive(Deserialize)]
struct CloudPathQuery {
    #[serde(default)]
    path: String,
}

/// Parent directory + single-segment name (mkdir / upload). A blank
/// `dir` means the WebDAV base collection itself (the picker's root).
#[derive(Deserialize)]
struct CloudCreateQuery {
    #[serde(default)]
    path: String,
    #[serde(default)]
    name: String,
}

/// Validate a bridge path where blank = the dav root (`CloudFiles`
/// explicitly supports an empty dir for the base collection; only the
/// page and the JS bridge surface it — deeper invalid paths still 400
/// before any network call).
fn cloud_dir_or_root(raw: &str) -> Result<String, String> {
    if raw.trim().is_empty() {
        Ok(String::new())
    } else {
        cloud::validate_path(raw)
    }
}

/// Join a possibly-root directory with a validated name (`join` keeps
/// its dir-non-empty contract; cloud.rs is out of this task's scope).
fn cloud_child(dir: &str, name: &str) -> String {
    if dir.is_empty() {
        name.to_string()
    } else {
        cloud::join(dir, name)
    }
}

fn cloud_bad(msg: &str) -> axum::response::Response {
    (
        StatusCode::BAD_REQUEST,
        Json(serde_json::json!({ "error": msg })),
    )
        .into_response()
}

/// Every bridge route when CLOUD_WEBDAV_* is not wired up.
fn cloud_not_configured() -> axum::response::Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(serde_json::json!({ "error": "cloud storage not configured" })),
    )
        .into_response()
}

/// Map a `cloud::CloudFiles` error string onto a status + JSON body:
/// upstream 404 → 404, 405/409/423 (exists/locked per RFC 4918) → 409,
/// other upstream 4xx → 400, upstream 5xx and transport failures
/// (`unreachable`) → 502 (the portal is the gateway to the internal
/// cloud host). The message itself is passed through verbatim.
fn cloud_error(err: &str) -> axum::response::Response {
    let status = match cloud::http_status(err) {
        Some(404) => StatusCode::NOT_FOUND,
        Some(405 | 409 | 423) => StatusCode::CONFLICT,
        Some(c) if (400..500).contains(&c) => StatusCode::BAD_REQUEST,
        Some(_) => StatusCode::BAD_GATEWAY,
        None => StatusCode::BAD_GATEWAY,
    };
    (status, Json(serde_json::json!({ "error": err }))).into_response()
}

/// MKCOL answers 405 when the collection already exists — surface that
/// as a clean 409 instead of the raw upstream text.
fn cloud_mkdir_error(err: &str) -> axum::response::Response {
    if err.contains("HTTP 405") {
        return (
            StatusCode::CONFLICT,
            Json(serde_json::json!({ "error": "directory already exists" })),
        )
            .into_response();
    }
    cloud_error(err)
}

/// GET /api/cloud/files?path=<dir> — Depth:1 listing, dirs first.
async fn handle_cloud_list(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<CloudPathQuery>,
) -> impl IntoResponse {
    let dir = match cloud_dir_or_root(&q.path) {
        Ok(d) => d,
        Err(e) => return cloud_bad(&e),
    };
    let Some(cloud) = &config.cloud else {
        return cloud_not_configured();
    };
    match cloud.list(&dir).await {
        Ok(entries) => (
            StatusCode::OK,
            Json(serde_json::json!({ "entries": entries })),
        )
            .into_response(),
        Err(e) => cloud_error(&e),
    }
}

/// POST /api/cloud/files?path=<dir>&name=<n> — mkdir (409 on exists).
async fn handle_cloud_mkdir(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<CloudCreateQuery>,
) -> impl IntoResponse {
    let dir = match cloud_dir_or_root(&q.path) {
        Ok(d) => d,
        Err(e) => return cloud_bad(&e),
    };
    let name = match cloud::validate_name(&q.name) {
        Ok(n) => n,
        Err(e) => return cloud_bad(&e),
    };
    let Some(cloud) = &config.cloud else {
        return cloud_not_configured();
    };
    let full = cloud_child(&dir, &name);
    match cloud.mkdir(&full).await {
        Ok(()) => (
            StatusCode::CREATED,
            Json(serde_json::json!({ "path": full })),
        )
            .into_response(),
        Err(e) => cloud_mkdir_error(&e),
    }
}

/// PUT /api/cloud/files?path=<dir>&name=<n> — raw-body upload (same
/// shape as the intercom/polls picked-file routes; the global
/// DefaultBodyLimit enforces intercom::MAX_UPLOAD, re-checked here).
async fn handle_cloud_upload(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<CloudCreateQuery>,
    headers: axum::http::HeaderMap,
    bytes: axum::body::Bytes,
) -> impl IntoResponse {
    let dir = match cloud_dir_or_root(&q.path) {
        Ok(d) => d,
        Err(e) => return cloud_bad(&e),
    };
    let name = match cloud::validate_name(&q.name) {
        Ok(n) => n,
        Err(e) => return cloud_bad(&e),
    };
    if bytes.is_empty() {
        return cloud_bad("upload body is empty");
    }
    if bytes.len() as u64 > intercom::MAX_UPLOAD {
        return cloud_bad(&format!("upload exceeds {} bytes", intercom::MAX_UPLOAD));
    }
    let Some(cloud) = &config.cloud else {
        return cloud_not_configured();
    };
    let full = cloud_child(&dir, &name);
    let content_type = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("application/octet-stream")
        .split(';')
        .next()
        .unwrap_or("application/octet-stream")
        .trim()
        .to_string();
    match cloud.put(&full, &bytes, &content_type).await {
        Ok(()) => (
            StatusCode::CREATED,
            Json(serde_json::json!({ "path": full })),
        )
            .into_response(),
        Err(e) => cloud_error(&e),
    }
}

/// GET /api/cloud/file?path=<file> — binary download, content-type from
/// the cloud (fallback octet-stream), inline. Active-content extensions
/// (svg/html/…) are forced to `attachment` — same stored-XSS rule as the
/// intercom/polls file routes, since these bytes render in portal origin.
async fn handle_cloud_download(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<CloudPathQuery>,
) -> impl IntoResponse {
    use axum::http::header::{CONTENT_DISPOSITION, CONTENT_TYPE};
    let path = match cloud::validate_path(&q.path) {
        Ok(p) => p,
        Err(e) => return cloud_bad(&e),
    };
    let Some(cloud) = &config.cloud else {
        return cloud_not_configured();
    };
    match cloud.download(&path).await {
        Ok(file) => {
            let content_type = file
                .content_type
                .unwrap_or_else(|| "application/octet-stream".into());
            // header-safe filename (quotes/backslashes would break out
            // of the quoted-string)
            let name = path
                .rsplit('/')
                .next()
                .unwrap_or(&path)
                .replace(['"', '\\'], "");
            let disposition = if intercom::serve_inline(&name) {
                format!("inline; filename=\"{name}\"")
            } else {
                format!("attachment; filename=\"{name}\"")
            };
            (
                [
                    (CONTENT_TYPE, content_type),
                    (CONTENT_DISPOSITION, disposition),
                ],
                file.bytes,
            )
                .into_response()
        }
        Err(e) => cloud_error(&e),
    }
}

/// DELETE /api/cloud/file?path=<file> — 204 / 404 / 400.
async fn handle_cloud_delete(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<CloudPathQuery>,
) -> impl IntoResponse {
    let path = match cloud::validate_path(&q.path) {
        Ok(p) => p,
        Err(e) => return cloud_bad(&e),
    };
    let Some(cloud) = &config.cloud else {
        return cloud_not_configured();
    };
    match cloud.delete(&path).await {
        Ok(()) => StatusCode::NO_CONTENT.into_response(),
        Err(e) => cloud_error(&e),
    }
}

/// POST /api/cloud/share?path=<file> — mint a public read-only share
/// link (OCS). This is what the picker's SELECT hands back: a share URL
/// addresses the cloud by name over https, so it passes the intercom /
/// polls attachment allowlist and the note can reference the file
/// without the portal mediating its bytes.
async fn handle_cloud_share(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<CloudPathQuery>,
) -> impl IntoResponse {
    let path = match cloud::validate_path(&q.path) {
        Ok(p) => p,
        Err(e) => return cloud_bad(&e),
    };
    let Some(cloud) = &config.cloud else {
        return cloud_not_configured();
    };
    match cloud.create_share(&path).await {
        Ok(url) => (StatusCode::CREATED, Json(serde_json::json!({ "url": url }))).into_response(),
        Err(e) => cloud_error(&e),
    }
}

// ── Cloud picker page (GET /files) ────────────────────────────────────
//
// SSR first paint WITHOUT JS: the current directory is listed straight
// from the cloud (cloud::CloudFiles::list), dirs are plain links and
// files carry download links, so a no-JS member can browse and fetch.
// app.js enhances the same page: fresh list via /api/cloud/files,
// upload (PUT), mkdir (POST), delete (DELETE) and — the reason this
// page exists — SELECT: /files?picker=1 writes the picked file's
// canonical URL into localStorage 'cloudPickResult' and closes the tab;
// the host page (intercom card) consumes it cross-tab (same origin).

/// /files query: current directory (`path`, blank = dav root) plus the
/// picker flag (`picker=1` → selection hands the URL back to the opener
/// instead of just browsing).
#[derive(Deserialize)]
struct CloudFilesPageQuery {
    #[serde(default)]
    path: String,
    #[serde(default)]
    picker: Option<String>,
}

/// Build a portal link with form-urlencoded query params (reqwest's
/// `Url` — already a dependency, so no encoder crate comes in).
fn portal_link(path: &str, params: &[(&str, String)]) -> String {
    let mut url = reqwest::Url::parse("https://portal.internal").expect("static base");
    url.set_path(path);
    {
        let mut pairs = url.query_pairs_mut();
        for (k, v) in params {
            pairs.append_pair(k, v);
        }
    }
    match url.query() {
        Some(q) => format!("{path}?{q}"),
        None => path.to_string(),
    }
}

/// /files link for `dir`, preserving picker mode (deep-linkable: the
/// browser URL always carries the full state — reload/back just works).
fn files_link(dir: &str, picker: bool) -> String {
    let mut params: Vec<(&str, String)> = Vec::new();
    if !dir.is_empty() {
        params.push(("path", dir.to_string()));
    }
    if picker {
        params.push(("picker", "1".to_string()));
    }
    portal_link("/files", &params)
}

/// Portal download-bridge link for a file (no-JS row action).
fn cloud_file_link(rel: &str) -> String {
    portal_link("/api/cloud/file", &[("path", rel.to_string())])
}

/// Human size for a file row (mirrors app.js' humanSize / the intercom
/// SSR formatting).
fn cloud_fmt_size(size: Option<u64>) -> String {
    match size {
        None => "—".to_string(),
        Some(b) if b < 1024 => format!("{b} B"),
        Some(b) if b < 1024 * 1024 => format!("{:.1} KB", b as f64 / 1024.0),
        Some(b) => format!("{:.1} MB", b as f64 / (1024.0 * 1024.0)),
    }
}

/// Breadcrumb trail: root link + one link per ancestor segment, current
/// segment as plain text. Every href is a deep link back into the same
/// place on the /files page (picker flag preserved).
fn render_cloud_trail(dir: &str, picker: bool) -> String {
    let mut out = format!(
        r#"<a href="{}">☁ Cloud-Dateien</a>"#,
        files_link("", picker)
    );
    let mut acc = String::new();
    for seg in dir.split('/').filter(|s| !s.is_empty()) {
        if !acc.is_empty() {
            acc.push('/');
        }
        acc.push_str(seg);
        let label = html_escape(seg);
        if acc == dir {
            out.push_str(&format!(
                r#"<span class="cf-sep" aria-hidden="true">›</span><span class="cf-here">{label}</span>"#
            ));
        } else {
            out.push_str(&format!(
                r#"<span class="cf-sep" aria-hidden="true">›</span><a href="{}">{label}</a>"#,
                files_link(&acc, picker)
            ));
        }
    }
    out
}

/// One row of the SSR file grid — markup mirrors exactly what app.js'
/// row() builds (createElement/textContent), so the JS re-render after
/// a fetch is visually identical to the SSR paint.
fn render_cloud_row(dir: &str, entry: &cloud::CloudEntry, picker: bool) -> String {
    let name = html_escape(&entry.name);
    let rel = if dir.is_empty() {
        entry.name.clone()
    } else {
        format!("{dir}/{}", entry.name)
    };
    let rel_attr = html_escape(&rel);
    if entry.is_dir {
        let href = html_escape(&files_link(&rel, picker));
        format!(
            r#"<li class="cf-row cf-dir"><a class="cf-main" href="{href}"><span class="cf-icon" aria-hidden="true">📁</span><span class="cf-name">{name}</span><span class="cf-size">Ordner</span></a><span class="cf-actions"><button class="cf-del" type="button" data-path="{rel_attr}" data-name="{name}" data-is-dir="1" aria-label="Löschen">🗑</button></span></li>"#
        )
    } else {
        let size = html_escape(&cloud_fmt_size(entry.size));
        let dl = html_escape(&cloud_file_link(&rel));
        format!(
            r#"<li class="cf-row cf-file"><span class="cf-main"><span class="cf-icon" aria-hidden="true">📄</span><span class="cf-name">{name}</span><span class="cf-size">{size}</span></span><span class="cf-actions"><a class="cf-dl" href="{dl}">Download</a><button class="cf-select" type="button" data-path="{rel_attr}" data-name="{name}">Auswählen</button><button class="cf-del" type="button" data-path="{rel_attr}" data-name="{name}" data-is-dir="0" aria-label="Löschen">🗑</button></span></li>"#
        )
    }
}

/// Server-render the whole /files document. `notice` carries the
/// not-configured / unreadable / bad-path message (error styling until
/// app.js flips it green).
fn render_cloud_files(
    dir: &str,
    entries: &[cloud::CloudEntry],
    picker: bool,
    notice: Option<&str>,
) -> String {
    let picker_attr = if picker { "1" } else { "0" };
    let dir_attr = html_escape(dir);
    let trail = render_cloud_trail(dir, picker);
    let hint = if picker {
        "Datei auswählen — der Link wird ins Portal übernommen."
    } else {
        "Ordner im Cloud-Speicher durchsehen, Dateien hochladen und teilen."
    };
    let rows = if entries.is_empty() {
        r#"<li class="cf-empty">Dieser Ordner ist leer.</li>"#.to_string()
    } else {
        entries
            .iter()
            .map(|e| render_cloud_row(dir, e, picker))
            .collect::<Vec<_>>()
            .join("\n")
    };
    let notice_html = match notice {
        Some(n) => format!(
            r#"<p class="cf-msg" id="cf-msg" aria-live="polite">{}</p>"#,
            html_escape(n)
        ),
        None => r#"<p class="cf-msg" id="cf-msg" aria-live="polite" hidden></p>"#.to_string(),
    };
    format!(
        r##"<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cloud-Dateien — openSME-Portal</title>
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23005cb9'/%3E%3Ctext x='16' y='22' font-family='system-ui,sans-serif' font-size='18' font-weight='700' fill='white' text-anchor='middle'%3EoS%3C/text%3E%3C/svg%3E">
    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self';">
    <script src="/app.js" defer></script>
    <style>
        :root {{
            --bg: #F3F5F7; --card: #FFFFFF; --border: #E2E6EA;
            --text: #22272E; --text-dim: #5F6B7A;
            --accent: #005CB9; --accent-light: #E8F1FB; --accent-hover: #E5732D;
            --radius: 10px; --shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: "Helvetica Neue", Arial, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; }}
        .cf-page {{ max-width: 760px; margin: 0 auto; padding: 28px 20px 48px; }}
        .cf-head h1 {{ color: var(--accent); font-size: 22px; margin-bottom: 2px; }}
        .cf-sub {{ color: var(--text-dim); font-size: 0.9rem; }}
        .cf-trail {{ margin: 16px 0 10px; font-size: 0.9rem; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
        .cf-trail a {{ color: var(--accent); text-decoration: none; }}
        .cf-trail a:hover {{ text-decoration: underline; }}
        .cf-sep {{ color: var(--text-dim); }}
        .cf-here {{ font-weight: 700; word-break: break-word; }}
        .cf-tools {{
            display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius); padding: 10px 12px;
        }}
        .cf-file-label {{
            background: var(--accent-light); color: var(--accent); border-radius: 999px;
            padding: 6px 14px; font-size: 13px; font-weight: 600; cursor: pointer;
        }}
        .cf-file-label:hover {{ background: #d7e7f8; }}
        .cf-btn {{
            background: var(--accent); color: #fff; border: none; border-radius: 999px;
            padding: 6px 14px; font-size: 13px; font-weight: 600; cursor: pointer; font-family: inherit;
        }}
        .cf-btn:hover {{ background: var(--accent-hover); }}
        .cf-btn-ghost {{
            background: #fff; color: var(--accent); border: 1px solid var(--accent);
            border-radius: 999px; padding: 5px 13px; font-size: 13px; font-weight: 600;
            cursor: pointer; font-family: inherit;
        }}
        .cf-btn-ghost:hover {{ background: var(--accent-light); }}
        .cf-mkdir-name {{
            border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px;
            font-size: 13px; width: 11rem; color: var(--text); font-family: inherit; background: #fff;
        }}
        .cf-file-name {{ color: var(--text-dim); font-size: 0.85rem; }}
        .cf-msg {{ margin: 10px 2px 0; font-size: 0.9rem; color: #A93226; }}
        .cf-msg.is-ok {{ color: #1B7A3D; }}
        .cf-grid {{
            list-style: none; margin-top: 12px; background: var(--card);
            border: 1px solid var(--border); border-radius: var(--radius);
            box-shadow: var(--shadow); padding: 4px 14px;
        }}
        .cf-row {{ display: flex; align-items: center; gap: 0.6rem; border-bottom: 1px solid var(--border); padding: 8px 0; }}
        .cf-row:last-child {{ border-bottom: none; }}
        .cf-main {{ display: flex; align-items: center; gap: 0.6rem; flex: 1; min-width: 0; color: inherit; text-decoration: none; }}
        a.cf-main:hover .cf-name {{ text-decoration: underline; color: var(--accent); }}
        .cf-icon {{ flex: none; }}
        .cf-name {{ flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: 0.92rem; }}
        .cf-size {{ flex: none; color: var(--text-dim); font-size: 0.8rem; }}
        .cf-actions {{ display: flex; align-items: center; gap: 0.4rem; flex: none; }}
        .cf-dl {{ color: var(--accent); font-size: 0.82rem; text-decoration: none; font-weight: 600; }}
        .cf-dl:hover {{ text-decoration: underline; }}
        .cf-select {{ background: #fff; color: var(--accent); border: 1px solid var(--accent); border-radius: 999px; padding: 3px 11px; font-size: 0.8rem; font-weight: 700; cursor: pointer; font-family: inherit; }}
        .cf-select:hover {{ background: var(--accent-light); }}
        .cf-del {{
            background: none; border: none; cursor: pointer; font: inherit;
            font-size: 0.85rem; line-height: 1; color: var(--text-dim);
            border-radius: 4px; padding: 2px 5px;
        }}
        .cf-del:hover, .cf-del:focus-visible {{ background: #A93226; color: #fff; }}
        .cf-empty {{ padding: 14px 0; color: var(--text-dim); font-size: 0.9rem; }}
        .cf-foot {{ display: flex; justify-content: space-between; margin-top: 16px; gap: 0.6rem; }}
        .cf-cancel {{ color: var(--text-dim); text-decoration: none; font-size: 0.9rem; }}
        .cf-cancel:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
<main class="cf-page" id="cloud-page" data-picker="{picker_attr}" data-path="{dir_attr}">
    <header class="cf-head">
        <h1>Cloud-Dateien</h1>
        <p class="cf-sub">{hint}</p>
    </header>
    <nav class="cf-trail" id="cf-trail" aria-label="Ordnerpfad">{trail}</nav>
    <div class="cf-tools">
        <label class="cf-file-label" for="cf-file">📎 Datei…</label>
        <input id="cf-file" type="file" aria-label="Datei zum Hochladen" hidden>
        <button class="cf-btn" id="cf-upload" type="button">Hochladen</button>
        <input class="cf-mkdir-name" id="cf-mkdir-name" type="text" placeholder="Neuer Ordner…" aria-label="Neuer Ordner">
        <button class="cf-btn-ghost" id="cf-mkdir" type="button">Neuer Ordner</button>
        <span class="cf-file-name" id="cf-file-name" aria-live="polite"></span>
    </div>
    {notice_html}
    <ul class="cf-grid" id="cf-grid">
{rows}
    </ul>
    <footer class="cf-foot">
        <a class="cf-cancel" href="/">✕ Abbrechen — zurück zum Portal</a>
        <button class="cf-btn" id="cf-done" type="button">Fertig</button>
    </footer>
</main>
</body>
</html>"##
    )
}

/// GET /files — the cloud picker page (session-gated). SSR lists the
/// current directory so the page is meaningful without JS; invalid
/// paths answer 400 with the same document + notice.
async fn handle_files(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Query(q): axum::extract::Query<CloudFilesPageQuery>,
) -> impl IntoResponse {
    let picker = q.picker.as_deref() == Some("1");
    let (dir, entries, notice, status) = match cloud_dir_or_root(&q.path) {
        Err(e) => (String::new(), Vec::new(), Some(e), StatusCode::BAD_REQUEST),
        Ok(dir) => match &config.cloud {
            None => (
                dir,
                Vec::new(),
                Some("Cloud-Speicher ist nicht konfiguriert (CLOUD_WEBDAV_* fehlt).".to_string()),
                StatusCode::OK,
            ),
            Some(cloud) => match cloud.list(&dir).await {
                Ok(entries) => (dir.clone(), entries, None, StatusCode::OK),
                Err(e) => (
                    dir,
                    Vec::new(),
                    Some(format!("Cloud-Ordner nicht lesbar: {e}")),
                    StatusCode::BAD_GATEWAY,
                ),
            },
        },
    };
    let html = render_cloud_files(&dir, &entries, picker, notice.as_deref());
    (status, Html(html)).into_response()
}

/// Resolve an "attachment from the cloud" URL into a metadata snapshot:
/// SSRF/allowlist guards + HEAD + Content-Disposition/Length/Type. The
/// file itself stays in the cloud — the portal never mediates bodies.
async fn resolve_cloud_attachment(
    config: &AppConfig,
    raw: &str,
) -> Result<Option<intercom::Attachment>, String> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Ok(None);
    }
    let url = intercom::validate_attachment_url(raw, &config.intercom)?;
    let head = config
        .intercom_client
        .head(&url)
        .timeout(Duration::from_secs(10))
        .send()
        .await;
    let head = match head {
        Ok(r) if r.status().is_success() => r,
        Ok(r) => {
            return Err(format!(
                "attachment source answered HTTP {}",
                r.status().as_u16()
            ))
        }
        Err(_) => return Err("attachment source unreachable".to_string()),
    };
    let hdr = |name: &axum::http::HeaderName| {
        head.headers()
            .get(name)
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string())
    };
    let size = head
        .headers()
        .get(axum::http::header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.parse().ok());
    Ok(Some(intercom::parse_attachment_metadata(
        &url,
        hdr(&axum::http::header::CONTENT_TYPE).as_deref(),
        size,
        hdr(&axum::http::header::CONTENT_DISPOSITION).as_deref(),
    )))
}

async fn handle_intercom_post(
    State(config): State<Arc<AppConfig>>,
    axum::extract::Extension(session): axum::extract::Extension<session::Session>,
    Json(post): Json<IntercomPost>,
) -> impl IntoResponse {
    use axum::response::IntoResponse;

    let bad = |msg: &str| {
        (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": msg })),
        )
    };

    let text = match intercom::validate_text(&post.text) {
        Ok(t) => t,
        Err(e) => return bad(&e).into_response(),
    };

    let kind = match intercom::validate_kind(post.kind.as_deref()) {
        Ok(k) => k,
        Err(e) => return bad(&e).into_response(),
    };

    let attachment =
        match resolve_cloud_attachment(&config, post.attachment_url.as_deref().unwrap_or("")).await
        {
            Ok(a) => a,
            Err(e) => return bad(&e).into_response(),
        };

    let msg = config
        .intercom_store
        .push(text, Some(session.name.clone()), attachment, kind);
    metrics::global().inc_intercom_message();
    (
        StatusCode::CREATED,
        Json(serde_json::json!({ "message": msg })),
    )
        .into_response()
}

async fn handle_app_js() -> impl IntoResponse {
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "application/javascript; charset=utf-8",
        )],
        include_str!("app.js"),
    )
}

async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install Ctrl+C handler");
    };

    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }

    info!("shutdown signal received, starting graceful shutdown");
}

fn init_logging() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "opensme_portal=info,tower_http=info".into()),
        )
        .init();
}

fn load_config() -> Arc<AppConfig> {
    let ai_api_url = load_env("AI_API_URL", "");
    let ai = if ai_api_url.is_empty() {
        None
    } else {
        Some(AiConfig {
            api_url: ai_api_url,
            model: load_env("AI_MODEL", ""),
            api_key: std::env::var("AI_API_KEY").ok(),
        })
    };

    let announcements = Arc::new(parse_announcements(&load_env("PORTAL_ANNOUNCEMENTS", "")));

    let intercom_client = {
        let mut builder = reqwest::Client::builder().timeout(Duration::from_secs(15));
        // The metadata HEAD must stay on the allowlisted host: without
        // this, a 302 off the allowed cloud would make reqwest follow
        // redirects and snapshot metadata from an arbitrary host
        // (allowlist bypass). No attachment flow needs redirects.
        builder = builder.redirect(reqwest::redirect::Policy::none());
        // Egress-restricted hosts reach the cloud (attachment metadata
        // HEAD, poll uploads) only through the egress proxy — same
        // wiring as the feed client.
        if let Some(mut proxy) = feeds::proxy_from_env() {
            // Internal targets (docker-net names like opensme-opencloud)
            // must bypass the egress proxy: it cannot resolve bridge
            // names and would fail every internal http request.
            if let Some(np) = std::env::var("PORTAL_NO_PROXY")
                .ok()
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
            {
                proxy = proxy.no_proxy(reqwest::NoProxy::from_string(&np));
            }
            builder = builder.proxy(proxy);
        }
        builder.build().unwrap_or_default()
    };

    Arc::new(AppConfig {
        portal_domain: load_env("PORTAL_DOMAIN", "portal.opensme.graphwiz.ai"),
        opensme_domain: load_env("OPENSME_DOMAIN", "opensme.graphwiz.ai"),
        opencloud_url: load_env("OPENCLOUD_URL", "https://cloud.opensme.graphwiz.ai"),
        mail_url: load_env("MAIL_URL", ""),
        idp_url: load_env("IDP_URL", ""),
        ticketing_url: load_env("TICKETING_URL", ""),
        cms_url: load_env("CMS_URL", ""),
        shop_url: load_env("SHOP_URL", ""),
        bpm_url: load_env("BPM_URL", ""),
        invoice_url: load_env("INVOICE_URL", ""),
        paperless_url: load_env("PAPERLESS_URL", ""),
        chat_url: load_env("CHAT_URL", ""),
        notes_url: load_env("NOTES_URL", ""),
        surveys_url: load_env("SURVEYS_URL", ""),
        calendar_url: load_env("CALENDAR_URL", ""),
        vc_url: load_env("VC_URL", ""),
        announcements,
        ai,
        // One shared client: connection pooling, 30s default timeout.
        // 300s: CPU-served LLMs cold-load for minutes — the first chat
        // after ollama eviction must not surface as "upstream unreachable".
        ai_client: reqwest::Client::builder()
            .timeout(Duration::from_secs(300))
            .build()
            .unwrap_or_default(),
        intercom: intercom::IntercomConfig::from_env(&load_env(
            "OPENSME_DOMAIN",
            "opensme.graphwiz.ai",
        )),
        intercom_store: Arc::new(intercom_store_from_env()),
        polls: polls_store_from_env(&intercom_client),
        // Cloud files bridge client — rides the shared wired client
        // (timeout, no redirects, egress proxy + internal-host bypass).
        cloud: cloud::CloudFiles::from_env().map(|c| c.with_client(intercom_client.clone())),
        session: session::SessionConfig::from_env(),
        intercom_client,
        feed_fetcher: Arc::new(feeds::FeedFetcher::new(feeds::parse_columns(&load_env(
            "PORTAL_FEEDS",
            "",
        )))),
    })
}

#[tokio::main]
async fn main() {
    init_logging();

    let config = load_config();
    // Loud early warning when the interactive surfaces are fail-closed
    // because the session secret is missing or still a CHANGEME placeholder.
    config.session.warn_if_closed();

    info!(
        portal_domain = %config.portal_domain,
        opensme_domain = %config.opensme_domain,
        ai_enabled = config.ai.is_some(),
        announcements = config.announcements.len(),
        polls_enabled = config.polls.is_some(),
        cloud_files = config.cloud.is_some(),
        feed_columns = config.feed_fetcher.columns().len(),
        "starting opensme-portal"
    );

    let app = build_router(config);

    let port: u16 = std::env::var("PORTAL_PORT")
        .ok()
        .and_then(|p| p.trim().parse().ok())
        .unwrap_or(8080);
    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .unwrap_or_else(|e| {
            panic!("failed to bind to {addr}: {e}");
        });

    info!("listening on {addr}");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .unwrap_or_else(|e| {
            panic!("server error: {e}");
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(crate) fn cfg_with(
        ticketing: &str,
        ai: Option<AiConfig>,
        announcements: &str,
    ) -> AppConfig {
        AppConfig {
            opencloud_url: "https://cloud.example".into(),
            mail_url: String::new(),
            idp_url: "https://auth.example".into(),
            ticketing_url: ticketing.into(),
            cms_url: String::new(),
            shop_url: String::new(),
            bpm_url: String::new(),
            invoice_url: String::new(),
            paperless_url: String::new(),
            chat_url: String::new(),
            notes_url: String::new(),
            surveys_url: String::new(),
            calendar_url: String::new(),
            vc_url: String::new(),
            portal_domain: "portal.example".into(),
            opensme_domain: "example".into(),
            announcements: Arc::new(parse_announcements(announcements)),
            ai,
            ai_client: reqwest::Client::new(),
            intercom: intercom::IntercomConfig {
                allowed_hosts: vec!["cloud.example".into()],
                allow_http: false,
            },
            intercom_store: Arc::new(intercom::IntercomStore::default()),
            polls: None,
            cloud: None,
            intercom_client: reqwest::Client::new(),
            feed_fetcher: Arc::new(feeds::FeedFetcher::new(feeds::parse_columns(""))),
            session: session::SessionConfig {
                idp_url: "https://auth.example".into(),
                idp_internal_url: String::new(),
                portal_domain: "portal.example".into(),
                client_id: "opensme-portal".into(),
                client_secret: "client-secret".into(),
                session_secret: b"0123456789abcdef0123456789abcdef".to_vec(),
                http: reqwest::Client::new(),
            },
        }
    }

    #[test]
    fn announcements_empty_for_unset_or_blank() {
        assert!(parse_announcements("").is_empty());
        assert!(parse_announcements("   ").is_empty());
    }

    #[test]
    fn announcements_parse_valid() {
        let raw = r#"[{"level":"warn","text":"Maintenance tonight"},{"text":"Welcome"}]"#;
        let got = parse_announcements(raw);
        assert_eq!(got.len(), 2);
        assert_eq!(got[0].level, "warn");
        assert_eq!(got[1].level, "info"); // default level
    }

    #[test]
    fn announcements_invalid_json_ignored() {
        assert!(parse_announcements("not json").is_empty());
        assert!(parse_announcements(r#"{"level":"info"}"#).is_empty()); // not an array
    }

    #[test]
    fn announcements_capped_and_blank_filtered() {
        let mut raw = String::from("[");
        for i in 0..20 {
            if i > 0 {
                raw.push_str(", ");
            }
            raw.push_str(&format!("{{\"text\":\"m{i}\"}}"));
        }
        raw.push_str(", {\"text\":\"   \"}]"); // 20 valid + 1 blank
        assert_eq!(parse_announcements(&raw).len(), MAX_ANNOUNCEMENTS);
    }

    #[test]
    fn announcements_rendered_escaped() {
        let a = Announcement {
            level: "info".into(),
            text: "<script>alert(1)</script> & \"quotes\"".into(),
        };
        let html = render_announcements(&[a]);
        assert!(html.contains("&lt;script&gt;"));
        assert!(!html.contains("<script>"));
        assert!(html.contains("&amp;"));
        assert!(html.contains("&quot;"));
    }

    #[test]
    fn ticketing_card_gated_on_url() {
        let with = get_services(&cfg_with("https://help.example", None, ""));
        assert!(with.iter().any(|s| s.name == "Support"));

        let without = get_services(&cfg_with("", None, ""));
        assert!(!without.iter().any(|s| s.name == "Support"));
    }

    #[test]
    fn cms_and_shop_cards_gated_on_url() {
        let mut c = cfg_with("", None, "");
        assert!(!get_services(&c).iter().any(|s| s.name == "Website"));
        assert!(!get_services(&c).iter().any(|s| s.name == "Shop"));

        c.cms_url = "https://www.example".into();
        c.shop_url = "https://shop.example".into();
        let services = get_services(&c);
        assert!(services.iter().any(|s| s.name == "Website"));
        assert!(services.iter().any(|s| s.name == "Shop"));
    }

    #[test]
    fn bpm_card_gated_on_url() {
        let mut c = cfg_with("", None, "");
        assert!(!get_services(&c).iter().any(|s| s.name == "Workflow"));

        c.bpm_url = "https://bpm.example".into();
        let services = get_services(&c);
        assert!(services.iter().any(|s| s.name == "Workflow"));
        assert!(services.iter().any(|s| s.description.contains("BPMN")));
    }

    #[test]
    fn identity_and_collabora_never_tiled() {
        // Infrastructure exclusion (portal-catalog spec): auth is SSO
        // plumbing (idp_url may be set for redirects), Collabora is an
        // embedded editor inside OpenCloud — neither ever becomes a tile.
        let c = cfg_with("", None, "");
        let services = get_services(&c);
        assert!(!services.iter().any(|s| s.id == "identity"));
        assert!(!services.iter().any(|s| s.id == "collabora"));
        assert!(!services.iter().any(|s| s.name == "Identity"));
        assert!(!services.iter().any(|s| s.name == "Collabora"));
    }

    #[test]
    fn invoice_archive_chat_notes_gated_on_url() {
        let mut c = cfg_with("", None, "");
        for name in ["Invoice", "Archive", "Chat", "Notes"] {
            assert!(!get_services(&c).iter().any(|s| s.name == name));
        }

        c.invoice_url = "https://invoice.example".into();
        c.paperless_url = "https://docs.example".into();
        c.chat_url = "https://chat.example".into();
        c.notes_url = "https://notes.example".into();
        let services = get_services(&c);
        assert!(services.iter().any(|s| s.name == "Invoice"));
        assert!(services.iter().any(|s| s.name == "Archive"));
        assert!(services.iter().any(|s| s.name == "Chat"));
        assert!(services.iter().any(|s| s.name == "Notes"));
    }

    #[test]
    fn service_ids_are_stable_unique_and_complete() {
        // Fully-configured portal: every card carries a stable id.
        let c = AppConfig {
            mail_url: "https://mail.example".into(),
            ticketing_url: "https://help.example".into(),
            cms_url: "https://www.example".into(),
            shop_url: "https://shop.example".into(),
            bpm_url: "https://bpm.example".into(),
            invoice_url: "https://invoice.example".into(),
            paperless_url: "https://docs.example".into(),
            chat_url: "https://chat.example".into(),
            notes_url: "https://notes.example".into(),
            surveys_url: "https://grist.example".into(),
            calendar_url: "https://calendar.example".into(),
            vc_url: "https://vc.example".into(),
            ..cfg_with("", None, "")
        };
        let services = get_services(&c);
        assert_eq!(services.len(), 13, "all thirteen catalog cards configured");

        let ids: Vec<&str> = services.iter().map(|s| s.id.as_str()).collect();
        assert!(
            ids.iter().all(|id| !id.is_empty()),
            "every service needs an id: {ids:?}"
        );
        let unique: std::collections::HashSet<&str> = ids.iter().copied().collect();
        assert_eq!(
            unique.len(),
            ids.len(),
            "service ids must be unique: {ids:?}"
        );
        assert!(ids.contains(&"opencloud"));
        assert!(ids.contains(&"webmail"));
        assert!(ids.contains(&"workflow"));
        assert!(ids.contains(&"invoice"));
        assert!(ids.contains(&"archive"));
        assert!(ids.contains(&"chat"));
        assert!(ids.contains(&"notes"));
        assert!(ids.contains(&"surveys"));
        assert!(!ids.contains(&"identity"));
        assert!(!ids.contains(&"collabora"));
    }

    #[test]
    fn ai_card_and_page_wiring() {
        let ai = AiConfig {
            api_url: "http://vllm:8000".into(),
            model: "test-model".into(),
            api_key: None,
        };
        let html = build_landing_page(&cfg_with("", Some(ai.clone()), ""), true);
        assert!(html.contains("id=\"ai-card\""));

        let plain = build_landing_page(&cfg_with("", None, ""), true);
        assert!(!plain.contains("id=\"ai-card\""));
    }

    #[test]
    fn announcements_rendered_into_page() {
        let html = build_landing_page(
            &cfg_with("", None, r#"[{"level":"warn","text":"planned outage"}]"#),
            true,
        );
        assert!(html.contains("class=\"announcement warn\""));
        assert!(html.contains("planned outage"));
    }

    #[test]
    fn fast_switch_marker_on_cards() {
        let html = build_landing_page(&cfg_with("", None, ""), true);
        assert!(html.contains("data-pal="));
        assert!(html.contains("/app.js"));
    }

    #[test]
    fn head_carries_favicon_and_meta() {
        let html = build_landing_page(&cfg_with("", None, ""), true);
        // Inline SVG favicon (data URI — CSP allows img-src data:)
        assert!(html.contains("rel=\"icon\""));
        assert!(html.contains("data:image/svg+xml"));
        // Social / search metadata
        assert!(html.contains("name=\"description\""));
        assert!(html.contains("og:title"));
        assert!(html.contains("name=\"theme-color\""));
    }

    #[test]
    fn html_escape_covers_angle_brackets() {
        assert_eq!(html_escape("<b>"), "&lt;b&gt;");
    }

    #[test]
    fn landing_page_mentions_opensme() {
        let html = build_landing_page(&cfg_with("", None, ""), true);
        assert!(html.to_lowercase().contains("opensme"));
        assert!(html.contains("lang=\"de\""));
    }

    #[test]
    fn landing_page_renders_feed_cards_per_configured_column() {
        let html = build_landing_page(&cfg_with("", None, ""), true);
        // Default columns are rendered server-side; the client fills the
        // lists from /api/feeds (no network needed for the shell).
        assert!(html.contains("id=\"feed-startup\""));
        assert!(html.contains("id=\"feed-markt\""));
        assert!(html.contains("id=\"feed-legal\""));
        assert!(html.contains(">Startup</h2>"));
        assert!(html.contains("Aktuelles"));
        assert!(html.contains("Lade Meldungen"));
        assert!(html.contains("func-card"));
    }

    #[tokio::test]
    async fn feeds_api_returns_columns_even_when_upstreams_dead() {
        // FeedFetcher pointed at a dead address: the endpoint must still
        // return 200 with the configured column shape (empty entries),
        // never a 5xx — a broken upstream must not break the page.
        let mut cfg = cfg_with("", None, "");
        cfg.feed_fetcher = Arc::new(feeds::FeedFetcher::new(vec![feeds::FeedColumn {
            id: "test".into(),
            title: "Test".into(),
            urls: vec!["http://127.0.0.1:1/rss".into()],
        }]));
        let resp = respond(cfg, "GET", "/api/feeds", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = body_json(resp).await;
        let cols = body["columns"].as_array().expect("columns array");
        assert_eq!(cols.len(), 1);
        assert_eq!(cols[0]["id"], "test");
        assert_eq!(cols[0]["entries"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn intercom_card_always_on_page() {
        let html = build_landing_page(&cfg_with("", None, ""), true);
        assert!(html.contains("id=\"intercom-card\""));
        assert!(html.contains("ic-send"));
    }

    #[test]
    fn tab_bar_has_dienste_notizen_and_logout() {
        let html = build_landing_page(&cfg_with("", None, ""), true);
        // Dienste + Notizen tabs always present
        assert!(html.contains("data-tab=\"dienste\""));
        assert!(html.contains("data-tab=\"notizen\""));
        // Umfragen tab is optional (gated on polls config) — absent here
        assert!(!html.contains("data-tab=\"umfragen\""));
        // Logout always rendered (POST /api/logout)
        assert!(html.contains("action=\"/api/logout\""));
        assert!(html.contains("method=\"post\""));
        assert!(html.contains("Abmelden"));
    }

    #[test]
    fn anonymous_landing_hides_tiles_and_signedin_chrome() {
        // contracts/tiles.yml, decision 2026-11-01: the tile catalog is
        // membership content — the anonymous landing shows login CTA +
        // public news, never service links or the logout affordance.
        let html = build_landing_page(&cfg_with("", None, ""), false);
        assert!(html.contains("href=\"/api/auth/login\""));
        assert!(html.contains("Anmelden"));
        // public news stays
        assert!(html.contains("Aktuelles"));
        assert!(html.contains("id=\"feed-startup\""));
        // signed-in chrome must NOT leak
        assert!(!html.contains("Abmelden"));
        assert!(!html.contains("action=\"/api/logout\""));
        // tile markup only — CSS class selectors legitimately mention
        // func-card, so assert on the generated tile anchor instead
        assert!(!html.contains("class=\"card func-card\""));
        assert!(!html.contains("data-pal="));
        assert!(!html.contains("id=\"ai-card\""));
        assert!(!html.contains("id=\"intercom-card\""));
        assert!(!html.contains("id=\"tab-notizen\""));
    }

    #[test]
    fn anonymous_landing_needs_no_client_js() {
        // The public page is static shell + server-rendered news; app.js
        // (tabs, intercom, polls, AI) is pointless — and would only fire
        // authenticated fetches that answer 401.
        let html = build_landing_page(&cfg_with("", None, ""), false);
        assert!(!html.contains("/app.js"));
    }

    #[tokio::test]
    async fn root_without_session_renders_public_landing() {
        let cfg = cfg_with("", None, "");
        let resp = respond_anon(cfg, "GET", "/").await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = body_str(resp).await;
        assert!(body.contains("login-cta"));
        assert!(!body.contains("data-pal=")); // no tile anchors
        assert!(!body.contains("Abmelden"));
    }

    #[test]
    fn umfragen_tab_gated_on_polls_config() {
        let plain = build_landing_page(&cfg_with("", None, ""), true);
        assert!(!plain.contains("data-tab=\"umfragen\""));
        assert!(!plain.contains("id=\"tab-umfragen\""));

        let mut c = cfg_with("", None, "");
        c.polls = Some(Arc::new(polls::PollStore::open(None, None, None)));
        let html = build_landing_page(&c, true);
        assert!(html.contains("data-tab=\"umfragen\""));
        assert!(html.contains("id=\"tab-umfragen\""));
        assert!(html.contains("id=\"polls-card\""));
    }

    #[test]
    fn widget_cards_moved_out_of_service_grid() {
        // Widgets live in their own tab panels, AFTER the services grid —
        // the Dienste section never contains intercom/polls markup.
        let mut c = cfg_with("", None, "");
        c.polls = Some(Arc::new(polls::PollStore::open(None, None, None)));
        let html = build_landing_page(&c, true);
        let grid = html.find("func-grid").expect("services grid");
        let notizen = html.find("id=\"tab-notizen\"").expect("notizen panel");
        let umfragen = html.find("id=\"tab-umfragen\"").expect("umfragen panel");
        let intercom = html.find("id=\"intercom-card\"").expect("intercom widget");
        let polls = html.find("id=\"polls-card\"").expect("polls widget");
        assert!(
            intercom > notizen,
            "intercom-card must be inside the Notizen panel"
        );
        assert!(
            polls > umfragen,
            "polls-card must be inside the Umfragen panel"
        );
        assert!(html[..grid].find("intercom-card").is_none());
        assert!(html[..grid].find("polls-card").is_none());
    }

    #[test]
    fn logout_button_has_solid_high_contrast_style() {
        // White-on-#A93226 = 6.62:1 (WCAG AA); the button must NOT regress
        // to a thin red border on blue (1.83:1) or a transparent ghost
        // (white 3.56:1 on the border — both fail AA).
        let html = build_landing_page(&cfg_with("", None, ""), true);
        // Render output: format! already collapsed {{ }} → { }
        let css = &html[html.find(".logout-form { margin-left").expect("logout css")..];
        assert!(css.contains("background: #A93226"), "solid red fill");
        assert!(css.contains("border-color: #A93226"), "border matches fill");
        assert!(
            !css.contains("border-color: #E9573F"),
            "old low-contrast border gone"
        );
        assert!(css.contains("color: #fff"), "white text on the fill");
    }

    #[test]
    fn dienste_tab_is_default_active() {
        let html = build_landing_page(&cfg_with("", None, ""), true);
        assert!(html.contains("class=\"tab-panel is-active\""));
        // Notizen panel starts hidden (aria-label sits between id and hidden)
        let notizen = html.find("id=\"tab-notizen\"").expect("notizen panel");
        assert!(html[notizen..].starts_with(r#"id="tab-notizen" aria-label="Notizen" hidden"#));
    }

    #[test]
    fn intercom_messages_rendered_escaped_into_page() {
        let cfg = cfg_with("", None, "");
        cfg.intercom_store.push(
            "note <script>alert(1)</script>".into(),
            None,
            None,
            intercom::KIND_NOTE.into(),
        );
        let att = intercom::parse_attachment_metadata(
            "https://cloud.example/s/f.pdf",
            Some("application/pdf"),
            Some(2048),
            Some("attachment; filename=\"Rechnung<script>.pdf\""),
        );
        cfg.intercom_store.push(
            "see attached".into(),
            None,
            Some(att),
            intercom::KIND_NOTE.into(),
        );
        let html = build_landing_page(&cfg, true);
        assert!(!html.contains("<script>alert(1)"));
        assert!(html.contains("&lt;script&gt;alert(1)&lt;/script&gt;"));
        assert!(html.contains("Rechnung&lt;script&gt;.pdf"));
        assert!(html.contains("2.0 KB"));
        assert!(html.contains("application/pdf"));
    }

    #[test]
    fn intercom_renders_kind_badges_and_delete_buttons() {
        let cfg = cfg_with("", None, "");
        cfg.intercom_store.push(
            "plain note".into(),
            Some("Ana".into()),
            None,
            intercom::KIND_NOTE.into(),
        );
        cfg.intercom_store.push(
            "maintenance hint".into(),
            Some("Ben".into()),
            None,
            intercom::KIND_INFO.into(),
        );
        cfg.intercom_store.push(
            "cert expiring".into(),
            None,
            None,
            intercom::KIND_WARN.into(),
        );
        let html = build_landing_page(&cfg, true);
        let start = html.find(r#"<ul class="ic-list""#).expect("ic-list ul");
        let end = start + html[start..].find("</ul>").expect("closing ul");
        let list = &html[start..end];
        // info/warn carry a fixed-label pill (never the raw kind string)
        assert!(list.contains(r#"<span class="ic-kind ic-kind-info">INFO</span>"#));
        assert!(list.contains(r#"<span class="ic-kind ic-kind-warn">WARN</span>"#));
        // "note" stays badge-free: exactly the two pills above
        assert_eq!(list.matches(r#"class="ic-kind "#).count(), 2);
        // one dismiss control per message, id echoed, no-JS-safe label
        assert_eq!(list.matches(r#"class="ic-del""#).count(), 3);
        assert!(list.contains(
            r#"<button class="ic-del" type="button" data-id="2" aria-label="Löschen">✕</button>"#
        ));
        // Attributes must not leak literal backslashes (raw-string escaping
        // bug that used to break the class/href of the SSR list items).
        assert!(!list.contains("\\"), "no literal backslash in the list");
        assert!(list.contains(r#"<span class="ic-meta">"#));
        // CSS for the three affordances ships with the page (format!
        // collapses the doubled braces, so the served selector is ".x {")
        for selector in [
            ".ic-del {",
            ".ic-del:hover, .ic-del:focus-visible {",
            ".ic-kind {",
            ".ic-kind-info {",
            ".ic-kind-warn {",
            ".ic-unread {",
            ".ic-unread[hidden] {",
        ] {
            assert!(html.contains(selector), "missing css: {selector}");
        }
    }

    // ── In-process HTTP contract tests (tower oneshot — no sockets) ────
    use axum::body::Body;
    use http_body_util::BodyExt;
    use std::sync::Mutex;
    use tower::ServiceExt;

    async fn respond(
        cfg: AppConfig,
        method: &str,
        uri: &str,
        json: Option<serde_json::Value>,
    ) -> axum::http::Response<Body> {
        // Intercom/polls live behind the signed-session firewall; the tests
        // act as an authenticated user with a freshly minted session cookie.
        let cookie = session::mint_session(&cfg.session, "test-user", "Test User", "t@example");
        let router = build_router(Arc::new(cfg));
        let builder = axum::http::Request::builder()
            .method(method)
            .uri(uri)
            .header(
                "cookie",
                format!("{}={cookie}", session::PORTAL_SESSION_COOKIE),
            );
        let request = match json {
            Some(v) => builder
                .header("content-type", "application/json")
                .body(Body::from(v.to_string()))
                .unwrap(),
            None => builder.body(Body::empty()).unwrap(),
        };
        router.oneshot(request).await.unwrap()
    }

    /// Anonymous variant — no session cookie, exactly what a fresh visitor
    /// sends. Used by the public-landing contract tests.
    async fn respond_anon(cfg: AppConfig, method: &str, uri: &str) -> axum::http::Response<Body> {
        let router = build_router(Arc::new(cfg));
        let request = axum::http::Request::builder()
            .method(method)
            .uri(uri)
            .body(Body::empty())
            .unwrap();
        router.oneshot(request).await.unwrap()
    }

    async fn body_json(resp: axum::http::Response<Body>) -> serde_json::Value {
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        serde_json::from_slice(&bytes).expect("response body is JSON")
    }

    /// HTML bodies (the /files picker page) as plain text.
    async fn body_str(resp: axum::http::Response<Body>) -> String {
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        String::from_utf8(bytes.to_vec()).expect("response body is UTF-8")
    }

    fn header(resp: &axum::http::Response<Body>, name: &str) -> String {
        resp.headers()
            .get(name)
            .expect("expected header present")
            .to_str()
            .map(str::to_string)
            .unwrap()
    }

    // ── Cloud files bridge contract tests ─────────────────────────────
    // Same in-process oneshot pattern, plus an anonymous variant (no
    // session cookie) to pin the firewall 401s and raw bodies for the
    // PUT upload route.
    async fn cloud_res(
        cfg: AppConfig,
        method: &str,
        uri: &str,
        body: Option<Vec<u8>>,
        authenticated: bool,
    ) -> axum::http::Response<Body> {
        let mut builder = axum::http::Request::builder().method(method).uri(uri);
        if authenticated {
            let cookie = session::mint_session(&cfg.session, "test-user", "Test User", "t@example");
            builder = builder.header(
                "cookie",
                format!("{}={cookie}", session::PORTAL_SESSION_COOKIE),
            );
        }
        let request = match body {
            Some(b) => builder.body(Body::from(b)).unwrap(),
            None => builder.body(Body::empty()).unwrap(),
        };
        build_router(Arc::new(cfg)).oneshot(request).await.unwrap()
    }

    fn cloud_cfg(cloud: Option<cloud::CloudFiles>) -> AppConfig {
        let mut cfg = cfg_with("", None, "");
        cfg.cloud = cloud;
        cfg
    }

    #[tokio::test]
    async fn cloud_bridge_is_behind_the_session_firewall() {
        // Anonymous callers never reach the handlers — every bridge
        // route answers 401 from the firewall, even with a bad path.
        let cfg = cloud_cfg(None);
        for (method, uri) in [
            ("GET", "/api/cloud/files?path=docs"),
            ("POST", "/api/cloud/files?path=docs&name=sub"),
            ("PUT", "/api/cloud/files?path=docs&name=a.pdf"),
            ("GET", "/api/cloud/file?path=docs/a.pdf"),
            ("DELETE", "/api/cloud/file?path=docs/a.pdf"),
        ] {
            let resp = cloud_res(cfg.clone(), method, uri, None, false).await;
            assert_eq!(resp.status(), StatusCode::UNAUTHORIZED, "{method} {uri}");
        }
    }

    #[tokio::test]
    async fn cloud_bridge_rejects_bad_paths_with_400_before_any_network() {
        // cloud = None on purpose: path/name/body validation must fire
        // BEFORE the 503 config check and long before any network call.
        // A BLANK path is valid since the picker arrived — it names the
        // dav root collection (cloud_files_bridge_lists_root_on_blank_
        // path pins that); everything else malformed still 400s here.
        let cfg = cloud_cfg(None);
        let cases: &[(&str, &str, Option<Vec<u8>>)] = &[
            // path: absolute / traversal / slash noise
            ("GET", "/api/cloud/files?path=/etc", None),
            ("GET", "/api/cloud/files?path=..", None),
            ("GET", "/api/cloud/files?path=docs/../x", None),
            ("GET", "/api/cloud/files?path=docs//x", None),
            (
                "GET",
                "/api/cloud/file?path=/remote.php/dav/files/u/x",
                None,
            ),
            ("GET", "/api/cloud/file?path=docs/", None),
            ("DELETE", "/api/cloud/file?path=", None),
            // name: missing / traversal / multi-segment
            ("POST", "/api/cloud/files?path=docs", None),
            ("POST", "/api/cloud/files?path=docs&name=..", None),
            ("POST", "/api/cloud/files?path=docs&name=a/b", None),
            ("PUT", "/api/cloud/files?path=docs&name=.", None),
            // upload: empty body (valid path+name, no bytes)
            (
                "PUT",
                "/api/cloud/files?path=docs&name=a.pdf",
                Some(Vec::new()),
            ),
        ];
        for (method, uri, body) in cases {
            let resp = cloud_res(cfg.clone(), method, uri, body.clone(), true).await;
            assert_eq!(resp.status(), StatusCode::BAD_REQUEST, "{method} {uri}");
            let b = body_json(resp).await;
            let err = b["error"].as_str().unwrap_or_default();
            assert!(
                err.starts_with("invalid cloud") || err == "upload body is empty",
                "{method} {uri} → unexpected error {err:?}"
            );
        }
    }

    #[tokio::test]
    async fn cloud_files_bridge_lists_root_on_blank_path() {
        // The picker browses the dav root: a blank `path` must flow
        // through validation to the config check (503 with cloud = None
        // — NOT the 400 the strict pre-picker validator produced), for
        // the listing AND the create routes (mkdir / upload at root).
        let cfg = cloud_cfg(None);
        for (method, uri, body) in [
            ("GET", "/api/cloud/files", None),
            ("GET", "/api/cloud/files?path=", None),
            ("GET", "/api/cloud/files?path=%20%20", None),
            ("POST", "/api/cloud/files?path=&name=sub", None),
            (
                "PUT",
                "/api/cloud/files?path=&name=a.pdf",
                Some(b"x".to_vec()),
            ),
        ] {
            let resp = cloud_res(cfg.clone(), method, uri, body, true).await;
            assert_eq!(
                resp.status(),
                StatusCode::SERVICE_UNAVAILABLE,
                "{method} {uri}"
            );
            let b = body_json(resp).await;
            assert_eq!(b["error"], "cloud storage not configured", "{method} {uri}");
        }
    }

    #[tokio::test]
    async fn cloud_share_route_gates_and_validates() {
        // Anonymous → 401 (firewall), malformed/blank path → 400 before
        // any network, missing cloud config → 503 (share minting needs
        // the OCS endpoint). This route feeds the picker's SELECT.
        let cfg = cloud_cfg(None);
        let resp = cloud_res(
            cfg.clone(),
            "POST",
            "/api/cloud/share?path=docs/a.pdf",
            None,
            false,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        let resp = cloud_res(
            cfg.clone(),
            "POST",
            "/api/cloud/share?path=/etc",
            None,
            true,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let b = body_json(resp).await;
        assert!(b["error"]
            .as_str()
            .unwrap_or_default()
            .starts_with("invalid cloud"));
        // A share needs a concrete file — blank (root) is not shareable.
        let resp = cloud_res(cfg.clone(), "POST", "/api/cloud/share", None, true).await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let resp = cloud_res(cfg, "POST", "/api/cloud/share?path=docs/a.pdf", None, true).await;
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn files_page_is_session_gated() {
        // The picker page drives the cloud bridge — it sits inside the
        // protected router like every other interactive surface.
        let cfg = cloud_cfg(None);
        let resp = cloud_res(cfg, "GET", "/files", None, false).await;
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn files_page_ssr_renders_chrome_and_notice_without_cloud() {
        // Signed-in, cloud unconfigured: 200 + meaningful page (header,
        // breadcrumb, tools, footer) + a visible notice — never a bare
        // error page. picker=1 must be reflected in the page state.
        let cfg = cloud_cfg(None);
        let resp = cloud_res(cfg, "GET", "/files?picker=1", None, true).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = body_str(resp).await;
        assert!(body.contains(r#"<h1>Cloud-Dateien</h1>"#));
        assert!(body.contains(r#"id="cloud-page" data-picker="1""#));
        assert!(body.contains("nicht konfiguriert"));
        assert!(body.contains(r#"id="cf-grid""#));
        assert!(body.contains(r#"id="cf-upload""#));
        assert!(body.contains(r#"id="cf-mkdir""#));
        assert!(body.contains(r#"id="cf-done""#));
        assert!(body.contains("Abbrechen"));
    }

    #[test]
    fn files_page_ssr_escapes_names_and_renders_rows() {
        // Pure render check with real entries: dirs link into /files
        // (picker flag preserved), files carry download + select +
        // delete, and cloud-controlled names never reach the HTML raw.
        let entries = vec![
            cloud::CloudEntry {
                name: "Verträge <2026>".into(),
                path: "/remote.php/dav/files/u/Verträge <2026>".into(),
                is_dir: true,
                size: None,
                mtime: None,
            },
            cloud::CloudEntry {
                name: "Angebot & Co.pdf".into(),
                path: "/remote.php/dav/files/u/Angebot & Co.pdf".into(),
                is_dir: false,
                size: Some(1500),
                mtime: None,
            },
        ];
        let html = render_cloud_files("Docs", &entries, true, None);
        // XSS: raw markup must never appear
        assert!(!html.contains("<2026>"));
        assert!(html.contains("Verträge &lt;2026&gt;"));
        assert!(html.contains("Angebot &amp; Co.pdf"));
        // dir row: deep link with encoded path + picker flag (& escaped
        // in the attribute, as html_escape demands)
        assert!(html.contains("href=\"/files?path=Docs%2FVertr%C3%A4ge+%3C2026%3E&amp;picker=1\""));
        // file row: download bridge link, select + delete with the
        // escaped relative path as data attribute
        assert!(html.contains("href=\"/api/cloud/file?path=Docs%2FAngebot+%26+Co.pdf\""));
        assert!(html.contains(r#"<button class="cf-select" type="button""#));
        assert!(html.contains(r#"data-path="Docs/Angebot &amp; Co.pdf""#));
        assert_eq!(html.matches(r#"class="cf-del""#).count(), 2);
        // sizes render human-readable, breadcrumb shows Docs as current
        assert!(html.contains("1.5 KB"));
        assert!(html.contains(r#"<span class="cf-here">Docs</span>"#));
        // picker mode preserved in the trail's root link
        assert!(html.contains("href=\"/files?picker=1\""));
        // empty directory → a friendly no-JS row, not an empty box
        let empty = render_cloud_files("Leer", &[], false, None);
        assert!(empty.contains("Dieser Ordner ist leer."));
    }

    #[test]
    fn intercom_card_carries_cloud_picker_button_and_css() {
        let html = build_landing_page(&cfg_with("", None, ""), true);
        assert!(html.contains(
            r#"<button class="ic-cloud" type="button" title="Datei aus der Cloud auswählen">☁️ aus der Cloud</button>"#
        ));
        // styling ships with the page (format! collapses doubled braces)
        assert!(html.contains(".ic-cloud {"));
        // the button sits inside the Notizen card's action row, next to
        // the URL input + file label + send button
        let start = html.find(r#"<div class="ic-box">"#).expect("ic-box");
        let end = html[start..]
            .find(r#"</div>"#)
            .map(|i| start + i)
            .expect("ic-box end");
        let box_html = &html[start..end];
        for marker in ["ic-url", "ic-cloud", "ic-file-label", "ic-send"] {
            assert!(box_html.contains(marker), "missing {marker} in ic-box");
        }
    }

    #[tokio::test]
    async fn cloud_bridge_answers_503_when_cloud_is_not_configured() {
        let cfg = cloud_cfg(None); // CLOUD_WEBDAV_* absent → None
        for (method, uri, body) in [
            ("GET", "/api/cloud/files?path=docs", None),
            ("POST", "/api/cloud/files?path=docs&name=sub", None),
            (
                "PUT",
                "/api/cloud/files?path=docs&name=a.pdf",
                Some(b"x".to_vec()),
            ),
            ("GET", "/api/cloud/file?path=docs/a.pdf", None),
            ("DELETE", "/api/cloud/file?path=docs/a.pdf", None),
        ] {
            let resp = cloud_res(cfg.clone(), method, uri, body, true).await;
            assert_eq!(
                resp.status(),
                StatusCode::SERVICE_UNAVAILABLE,
                "{method} {uri}"
            );
            let b = body_json(resp).await;
            assert_eq!(b["error"], "cloud storage not configured", "{method} {uri}");
        }
    }

    #[tokio::test]
    async fn cloud_bridge_maps_upstream_errors_passthrough() {
        // Pure mapping of the exact error strings cloud.rs emits.
        let status = |e: &str| cloud_error(e).status();
        assert_eq!(
            status("GET docs/a.pdf failed: HTTP 404"),
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            status("DELETE docs failed: HTTP 404"),
            StatusCode::NOT_FOUND
        );
        assert_eq!(
            status("MKCOL Docs/neu failed: HTTP 405"),
            StatusCode::CONFLICT
        );
        assert_eq!(status("PUT a.pdf failed: HTTP 409"), StatusCode::CONFLICT);
        assert_eq!(status("MKCOL a failed: HTTP 423"), StatusCode::CONFLICT);
        assert_eq!(
            status("PUT a.pdf failed: HTTP 403"),
            StatusCode::BAD_REQUEST
        );
        assert_eq!(
            status("PROPFIND docs failed: HTTP 507"),
            StatusCode::BAD_GATEWAY
        );
        assert_eq!(
            status("PROPFIND docs unreachable: connection refused"),
            StatusCode::BAD_GATEWAY
        );
        // body passes the message through verbatim
        let resp = cloud_error("GET x failed: HTTP 404");
        assert_eq!(body_json(resp).await["error"], "GET x failed: HTTP 404");

        // mkdir 405 (RFC 4918 "already exists") becomes a clean 409
        let resp = cloud_mkdir_error("MKCOL Docs/neu failed: HTTP 405");
        assert_eq!(resp.status(), StatusCode::CONFLICT);
        assert_eq!(body_json(resp).await["error"], "directory already exists");
        // every other mkdir error flows through the generic mapping
        assert_eq!(
            cloud_mkdir_error("MKCOL a failed: HTTP 404").status(),
            StatusCode::NOT_FOUND
        );
    }

    #[tokio::test]
    async fn cloud_bridge_surfaces_cloud_failures_as_502_json() {
        // CloudFiles wired to a closed local port: the bridge must
        // surface the transport failure as 502 JSON on every route —
        // no panic, no 500. No live cloud involved.
        let cfg = cloud_cfg(Some(cloud::CloudFiles::from_parts(
            "http://127.0.0.1:1/remote.php/dav/files/portal".into(),
            "u".into(),
            "p".into(),
            reqwest::Client::builder()
                .timeout(Duration::from_secs(2))
                .build()
                .unwrap(),
        )));
        for (method, uri, body) in [
            ("GET", "/api/cloud/files?path=docs", None),
            ("POST", "/api/cloud/files?path=docs&name=sub", None),
            (
                "PUT",
                "/api/cloud/files?path=docs&name=a.pdf",
                Some(b"x".to_vec()),
            ),
            ("GET", "/api/cloud/file?path=docs/a.pdf", None),
            ("DELETE", "/api/cloud/file?path=docs/a.pdf", None),
        ] {
            let resp = cloud_res(cfg.clone(), method, uri, body, true).await;
            assert_eq!(resp.status(), StatusCode::BAD_GATEWAY, "{method} {uri}");
            let b = body_json(resp).await;
            assert!(
                b["error"]
                    .as_str()
                    .unwrap_or_default()
                    .contains("unreachable"),
                "{method} {uri} → {b}"
            );
        }
    }

    #[tokio::test]
    async fn root_serves_security_headers() {
        let resp = respond(cfg_with("", None, ""), "GET", "/", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(header(&resp, "x-content-type-options"), "nosniff");
        assert_eq!(header(&resp, "x-frame-options"), "DENY");
        assert_eq!(
            header(&resp, "referrer-policy"),
            "strict-origin-when-cross-origin"
        );
        let csp = header(&resp, "content-security-policy");
        assert!(csp.contains("default-src 'self'"));
        let hsts = header(&resp, "strict-transport-security");
        assert!(hsts.contains("max-age=31536000"), "hsts: {hsts}");
        assert!(hsts.contains("includeSubDomains"), "hsts: {hsts}");
    }

    #[tokio::test]
    async fn intercom_file_serving_carries_nosniff_and_disposition() {
        // Picked-file serving must carry the SAME security headers as the
        // HTML surfaces: nosniff denies browsers content-type sniffing on
        // a served attachment (completes the serve_inline/disposition
        // hardening — an octet-stream answer sniffed as html/script would
        // otherwise still run in the portal origin).
        let dir = std::env::temp_dir().join(format!(
            "ic-http-{}-{}",
            std::process::id(),
            intercom::random_token()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let mut cfg = cfg_with("", None, "");
        cfg.intercom_store = Arc::new(intercom::IntercomStore::open(None, Some(dir.clone())));
        let (token, name) = cfg
            .intercom_store
            .save_attachment("note.txt", b"body")
            .unwrap();

        // Benign extension: inline + nosniff + private immutable caching.
        let resp = respond(
            cfg.clone(),
            "GET",
            &format!("/api/intercom/files/{token}/{name}"),
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(header(&resp, "x-content-type-options"), "nosniff");
        assert_eq!(
            header(&resp, "content-disposition"),
            "inline; filename=\"note.txt\""
        );
        assert_eq!(
            header(&resp, "cache-control"),
            "private, max-age=31536000, immutable"
        );

        // Active content: forced to attachment — never inline.
        let (svg_tok, svg_name) = cfg
            .intercom_store
            .save_attachment("evil.svg", b"<svg/>")
            .unwrap();
        let resp = respond(
            cfg.clone(),
            "GET",
            &format!("/api/intercom/files/{svg_tok}/{svg_name}"),
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(header(&resp, "x-content-type-options"), "nosniff");
        assert_eq!(
            header(&resp, "content-disposition"),
            "attachment; filename=\"evil.svg\""
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn intercom_delete_moderation_404_then_204() {
        let cfg = cfg_with("", None, "");

        // Unknown id → 404 with the error body.
        let resp = respond(cfg.clone(), "DELETE", "/api/intercom/9999", None).await;
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let body = body_json(resp).await;
        assert_eq!(body["error"], "message not found");

        // A real post (banner kind) → 204 and the note is gone.
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": "moderate me", "kind": "warn" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::CREATED);
        let created = body_json(resp).await;
        assert_eq!(created["message"]["kind"], "warn");
        let id = created["message"]["id"].as_u64().expect("numeric id");

        let resp = respond(cfg.clone(), "DELETE", &format!("/api/intercom/{id}"), None).await;
        assert_eq!(resp.status(), StatusCode::NO_CONTENT);
        assert_eq!(
            cfg.intercom_store.list().len(),
            0,
            "deleted note must be gone from the store"
        );

        // Deleting the same id again → 404 (idempotent-ish miss).
        let resp = respond(cfg, "DELETE", &format!("/api/intercom/{id}"), None).await;
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn intercom_kind_flows_through_post_list_and_is_validated() {
        let cfg = cfg_with("", None, "");

        // Invalid kind → 400 with the exact contract message.
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": "hi", "kind": "banana" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body = body_json(resp).await;
        assert_eq!(body["error"], "kind must be one of note, info, warn");

        // Upload route: same validation on the query param.
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom/upload?text=upload&kind=nope",
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let body = body_json(resp).await;
        assert_eq!(body["error"], "kind must be one of note, info, warn");

        // Missing kind defaults to "note"; explicit kind is kept and
        // surfaced in both list and list_since responses.
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": "plain note" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::CREATED);
        let created = body_json(resp).await;
        assert_eq!(created["message"]["kind"], "note");
        let first = created["message"]["id"].as_u64().unwrap();

        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": "banner", "kind": "info" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::CREATED);
        let second = body_json(resp).await["message"]["id"].as_u64().unwrap();

        let resp = respond(cfg.clone(), "GET", "/api/intercom", None).await;
        let body = body_json(resp).await;
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 2);
        assert_eq!(msgs[0]["kind"], "info");
        assert_eq!(msgs[1]["kind"], "note");

        let resp = respond(cfg, "GET", &format!("/api/intercom?since={first}"), None).await;
        let body = body_json(resp).await;
        let msgs = body["messages"].as_array().unwrap();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0]["id"], second);
        assert_eq!(msgs[0]["kind"], "info");
    }

    #[tokio::test]
    async fn intercom_delete_is_session_gated() {
        // The moderation DELETE route lives in the PROTECTED router:
        // anonymous callers get the firewall's 401, never the handler.
        let cfg = cfg_with("", None, "");
        let router = build_router(Arc::new(cfg));
        let req = axum::http::Request::builder()
            .method("DELETE")
            .uri("/api/intercom/1")
            .body(Body::empty())
            .unwrap();
        let anon = router.oneshot(req).await.unwrap();
        assert_eq!(
            anon.status(),
            StatusCode::UNAUTHORIZED,
            "anonymous moderation delete must be gated"
        );
    }

    #[tokio::test]
    async fn polls_file_serving_carries_nosniff_and_disposition() {
        // Same contract as intercom file serving: nosniff (global
        // middleware, pinned here for this route), disposition from the
        // shared serve_inline blocklist, immutable private caching.
        let dir = std::env::temp_dir().join(format!(
            "pl-http-{}-{}",
            std::process::id(),
            intercom::random_token()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let mut cfg = cfg_with("", None, "");
        cfg.polls = Some(Arc::new(polls::PollStore::open(
            None,
            Some(dir.clone()),
            None,
        )));
        let store = cfg.polls.clone().unwrap();

        let (token, name) = store.save_attachment_local("regeln.txt", b"v1").unwrap();
        let resp = respond(
            cfg.clone(),
            "GET",
            &format!("/api/files/{token}/{name}"),
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(header(&resp, "x-content-type-options"), "nosniff");
        assert_eq!(
            header(&resp, "content-disposition"),
            "inline; filename=\"regeln.txt\""
        );
        assert_eq!(
            header(&resp, "cache-control"),
            "private, max-age=31536000, immutable"
        );

        let (svg_tok, svg_name) = store.save_attachment_local("logo.svg", b"<svg/>").unwrap();
        let resp = respond(
            cfg,
            "GET",
            &format!("/api/files/{svg_tok}/{svg_name}"),
            None,
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(header(&resp, "x-content-type-options"), "nosniff");
        assert_eq!(
            header(&resp, "content-disposition"),
            "attachment; filename=\"logo.svg\""
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn json_endpoints_carry_security_headers_too() {
        for uri in ["/health", "/api/services", "/api/announcements"] {
            let resp = respond(cfg_with("", None, ""), "GET", uri, None).await;
            assert_eq!(resp.status(), StatusCode::OK, "GET {uri}");
            assert_eq!(header(&resp, "x-content-type-options"), "nosniff", "{uri}");
        }
    }

    #[tokio::test]
    async fn services_gate_rejects_anonymous_and_serves_signed_in() {
        let cfg = cfg_with("", None, "");
        // Anonymous: the signed-session firewall rejects the tile catalog
        // (tiles are a signed-in surface; the Next.js page skips the fetch
        // entirely when no session cookie is present, so no UI breakage).
        let router = build_router(Arc::new(cfg.clone()));
        let req = axum::http::Request::builder()
            .uri("/api/services")
            .body(Body::empty())
            .unwrap();
        let anon = router.oneshot(req).await.unwrap();
        assert_eq!(
            anon.status(),
            StatusCode::UNAUTHORIZED,
            "anonymous /api/services must be gated"
        );

        // Signed-in (minted session cookie): the catalog is served.
        let resp = respond(cfg, "GET", "/api/services", None).await;
        assert_eq!(resp.status(), StatusCode::OK, "signed-in /api/services");
        let body = body_json(resp).await;
        let services = body["services"].as_array().expect("services array");
        assert!(!services.is_empty(), "tile catalog must be non-empty");
    }

    #[tokio::test]
    async fn ai_chat_route_absent_when_unconfigured() {
        // Route only mounted when AI_API_URL set — axum's default 404
        // (empty body) keeps the endpoint hidden.
        let resp = respond(
            cfg_with("", None, ""),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "ping" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn ai_chat_rejects_empty_and_oversized_questions() {
        // unroutable upstream — validation must short-circuit before any call
        let ai = AiConfig {
            api_url: "http://127.0.0.1:1".into(),
            model: "m".into(),
            api_key: None,
        };
        let resp = respond(
            cfg_with("", Some(ai.clone()), ""),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "   " })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

        let resp = respond(
            cfg_with("", Some(ai), ""),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "x".repeat(4001) })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    /// Spawn a throwaway OpenAI-compatible upstream on an ephemeral port.
    /// Captures the Authorization header + model field of the last call.
    async fn spawn_mock_ai(
        status: StatusCode,
        payload: &'static str,
    ) -> (String, Arc<Mutex<Option<(String, String)>>>) {
        let captured: Arc<Mutex<Option<(String, String)>>> = Arc::new(Mutex::new(None));
        let cap = captured.clone();
        let app = axum::Router::new().route(
            "/v1/chat/completions",
            axum::routing::post(move |headers: axum::http::HeaderMap, body: String| {
                let cap = cap.clone();
                async move {
                    *cap.lock().unwrap() = Some((
                        headers
                            .get("authorization")
                            .and_then(|v| v.to_str().ok())
                            .map(str::to_string)
                            .unwrap_or_default(),
                        serde_json::from_str::<serde_json::Value>(&body)
                            .ok()
                            .and_then(|v| v["model"].as_str().map(str::to_string))
                            .unwrap_or_default(),
                    ));
                    (status, payload)
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{addr}"), captured)
    }

    fn cfg_with_ai(api_url: String) -> AppConfig {
        cfg_with(
            "",
            Some(AiConfig {
                api_url,
                model: "test-model".into(),
                api_key: Some("secret-key".into()),
            }),
            "",
        )
    }

    const PONG_JSON: &str = r#"{"choices":[{"message":{"content":"pong"}}]}"#;

    #[tokio::test]
    async fn ai_chat_happy_path_via_mock_upstream() {
        let (url, captured) = spawn_mock_ai(StatusCode::OK, PONG_JSON).await;
        let resp = respond(
            cfg_with_ai(url),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "  ping  " })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = body_json(resp).await;
        assert_eq!(body["answer"], "pong");

        let (auth, model) = captured.lock().unwrap().clone().unwrap();
        assert_eq!(auth, "Bearer secret-key"); // API key forwarded
        assert_eq!(model, "test-model"); // configured model forwarded
                                         // question was trimmed before proxying
    }

    #[tokio::test]
    async fn ai_chat_upstream_error_maps_to_502() {
        let (url, _) = spawn_mock_ai(StatusCode::INTERNAL_SERVER_ERROR, "boom").await;
        let resp = respond(
            cfg_with_ai(url),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "ping" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_GATEWAY);
        let body = body_json(resp).await;
        assert!(body["error"]
            .as_str()
            .unwrap()
            .contains("upstream returned 500"));
    }

    #[tokio::test]
    async fn ai_chat_upstream_bad_json_maps_to_502() {
        let (url, _) = spawn_mock_ai(StatusCode::OK, "<html>not json</html>").await;
        let resp = respond(
            cfg_with_ai(url),
            "POST",
            "/api/ai/chat",
            Some(serde_json::json!({ "question": "ping" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_GATEWAY);
        let body = body_json(resp).await;
        assert!(body["error"].as_str().unwrap().contains("decode error"));
    }

    // ── Property-based tests (proptest) ───────────────────────────────
    use proptest::prelude::*;

    proptest! {
        /// Any input string must parse without panicking, stay capped,
        /// normalize levels and drop blank texts.
        #[test]
        fn prop_announcements_parser_is_total(
            raw in ".*",
        ) {
            let got = parse_announcements(&raw);
            prop_assert!(got.len() <= MAX_ANNOUNCEMENTS);
            prop_assert!(got.iter().all(|a| a.level == "info" || a.level == "warn"));
            prop_assert!(got.iter().all(|a| !a.text.trim().is_empty()));
        }

        /// Escaped output must never contain raw angle brackets —
        // the XSS invariant, checked over arbitrary attacker input.
        #[test]
        fn prop_html_escape_strips_angle_brackets(s in ".*") {
            let escaped = html_escape(&s);
            prop_assert!(!escaped.contains('<'));
            prop_assert!(!escaped.contains('>'));
        }

        /// Parser vs. model: valid entries survive in order (capped),
        /// blanks are dropped, unknown levels normalize to info.
        #[test]
        fn prop_announcements_matches_model(
            items in proptest::collection::vec((proptest::bool::ANY, "[a-zA-Z0-9 ]{0,30}"), 0..12),
        ) {
            let raw = serde_json::to_string(&items.iter()
                .map(|(warn, text)| serde_json::json!({
                    "level": if *warn { "warn" } else { "curious" },
                    "text": text,
                }))
                .collect::<Vec<_>>()
            ).unwrap();
            let got = parse_announcements(&raw);
            let model: Vec<(bool, String)> = items.iter()
                .filter(|(_, t)| !t.trim().is_empty())
                .take(MAX_ANNOUNCEMENTS)
                .map(|(w, t)| (*w, t.clone()))
                .collect();
            prop_assert_eq!(got.len(), model.len());
            for (a, (warn, text)) in got.iter().zip(&model) {
                prop_assert_eq!(&a.text, text);
                prop_assert_eq!(a.level == "warn", *warn);
            }
        }
    }

    // ── Intercom HTTP contract (in-process) ─────────────────────────

    #[tokio::test]
    async fn intercom_post_then_list_roundtrip() {
        let cfg = cfg_with("", None, "");
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": "  Team-Meet um 10  " })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::CREATED);
        let posted = body_json(resp).await;
        assert_eq!(posted["message"]["text"], "Team-Meet um 10");

        let resp = respond(cfg, "GET", "/api/intercom", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let listed = body_json(resp).await;
        assert_eq!(listed["messages"][0]["text"], "Team-Meet um 10");
        assert!(listed["messages"][0]["id"].is_u64());
    }

    #[tokio::test]
    async fn intercom_rejects_blank_and_oversized_text() {
        let cfg = cfg_with("", None, "");
        for text in ["", "    "] {
            let resp = respond(
                cfg.clone(),
                "POST",
                "/api/intercom",
                Some(serde_json::json!({ "text": text })),
            )
            .await;
            assert_eq!(resp.status(), StatusCode::BAD_REQUEST, "text={text:?}");
        }
        let long = "x".repeat(intercom::MAX_TEXT + 1);
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": long })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        // nothing leaked into the store
        let listed = body_json(respond(cfg, "GET", "/api/intercom", None).await).await;
        assert_eq!(listed["messages"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn intercom_attachment_foreign_host_rejected_without_store_write() {
        let cfg = cfg_with("", None, "");
        let resp = respond(
            cfg.clone(),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({
                "text": "see attached",
                "attachment_url": "https://evil.example/s/abc"
            })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
        let err = body_json(resp).await;
        assert!(err["error"].as_str().unwrap_or("").contains("not allowed"));
        let listed = body_json(respond(cfg, "GET", "/api/intercom", None).await).await;
        assert_eq!(listed["messages"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn intercom_attachment_unreachable_upstream_is_400() {
        let cfg = cfg_with("", None, "");
        // allowlisted host but nothing answers there — the metadata HEAD
        // fails and the message is rejected (no silent text-only post).
        let resp = respond(
            cfg,
            "POST",
            "/api/intercom",
            Some(serde_json::json!({
                "text": "see attached",
                "attachment_url": "https://cloud.example/s/abc"
            })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    // ── Prometheus /metrics ─────────────────────────────────────────

    async fn metrics_body() -> String {
        let resp = respond(cfg_with("", None, ""), "GET", "/metrics", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        String::from_utf8(bytes.to_vec()).expect("metrics body is UTF-8")
    }

    fn metric_value(body: &str, name: &str) -> u64 {
        body.lines()
            .find_map(|line| {
                let rest = line.strip_prefix(name)?.strip_prefix(' ')?;
                rest.parse().ok()
            })
            .expect("metric line present")
    }

    #[tokio::test]
    async fn metrics_endpoint_exposes_all_families() {
        // The track_metrics middleware counts a request only AFTER the handler
        // ran (counter increment happens post-next.run), so a first /metrics
        // scrape never contains itself. Make a normal request first (counted),
        // scrape once, then scrape again: the second body carries both the
        // /health hit and the /metrics scrape itself.
        let _ = respond(cfg_with("", None, ""), "GET", "/health", None).await;
        let _ = metrics_body().await; // scrape #1 (counted by scrape #2)
        let body = metrics_body().await; // scrape #2: full picture
        for name in [
            "http_requests_total",
            "http_request_duration_seconds",
            "active_sessions",
            "intercom_messages_total",
            "sso_logins_total",
            "sso_login_failures_total",
        ] {
            assert!(body.contains(name), "{name} missing from /metrics");
        }
        // an ordinary request and the /metrics scrape are both observed
        // (middleware is active on both routes)
        assert!(
            body.contains("http_requests_total{method=\"GET\",route=\"/health\",status=\"200\"}")
        );
        assert!(
            body.contains("http_requests_total{method=\"GET\",route=\"/metrics\",status=\"200\"}")
        );
    }

    #[tokio::test]
    async fn metrics_observe_regular_requests_too() {
        let resp = respond(cfg_with("", None, ""), "GET", "/health", None).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let body = metrics_body().await;
        assert!(
            body.contains("http_requests_total{method=\"GET\",route=\"/health\",status=\"200\"}")
        );
    }

    #[tokio::test]
    async fn intercom_post_increments_intercom_counter() {
        let before = metric_value(&metrics_body().await, "intercom_messages_total");
        let resp = respond(
            cfg_with("", None, ""),
            "POST",
            "/api/intercom",
            Some(serde_json::json!({ "text": "count me" })),
        )
        .await;
        assert_eq!(resp.status(), StatusCode::CREATED);
        let after = metric_value(&metrics_body().await, "intercom_messages_total");
        assert!(
            after > before,
            "intercom counter should increase: {before} -> {after}"
        );
    }
}
