from fasthtml.common import *
from fhdaisy import *
from fhdaisy.core import daisy_hdrs
from seo_rat.sqlite_db import get_session
from seo_rat.models import Website, GSCAnalytics, add_or_update_website, TrackedKeyword, add_tracked_keyword, delete_tracked_keyword, get_tracked_keywords, get_url_mapping, sync_url_mapping
from seo_rat.gsc.queries import get_top_pages, get_top_queries, get_wins, get_country_breakdown, get_trends
from seo_rat.gsc.sync import get_missing_dates, store_single_date
from seo_rat.gsc_client import GSCAuth, get_date_range, get_verified_sites
from seo_rat.insights.trends import detect_query_trends
from seo_rat.insights.intent import classify_page_intents
from seo_rat.content.analysis import find_cannibalized
from seo_rat.index_tracking import get_index_status, get_not_indexed_by_reason, store_index_status, fetch_sitemap_urls, get_index_history
from seo_rat.schema_extractor import extract_faq_queries
from seo_rat.schema_validator import validate_page
from seo_rat.article import get_articles_by_website, Article
from seo_rat.report.generator import generate_seo_report
from seo_rat.article import Article, insert_article
from seo_rat.dspy_infer import infer_article_seo, get_article_content, predict_schemas
import importlib.util
import json
import pycountry
from dotenv import load_dotenv
from sqlmodel import select, func
from pathlib import Path
from threading import Thread
import time
from datetime import datetime
import csv, io

CONFIG_DIR = Path.home() / ".config" / "seo_rat"
SETTINGS_ENV_PATH = CONFIG_DIR / ".env"
REPORT_CACHE_DIR = CONFIG_DIR / "report_cache"

_sync_progress = {}
_index_check_progress = {}
_report_cache: dict[int, dict] = {}  # website_id → report_data, cleared on refresh
_report_progress: dict[int, dict] = {}  # website_id → {status, pct, msg}


def _report_cache_path(website_id: int) -> Path:
    return REPORT_CACHE_DIR / f"{website_id}.json"

def _load_cached_report(website_id: int) -> dict | None:
    data = _report_cache.get(website_id)
    if data is not None:
        return data
    path = _report_cache_path(website_id)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            _report_cache[website_id] = data
            return data
        except (json.JSONDecodeError, OSError):
            return None
    return None

def _save_cached_report(website_id: int, data: dict):
    _report_cache[website_id] = data
    REPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _report_cache_path(website_id)
    path.write_text(json.dumps(data, default=str, ensure_ascii=False))


# ── Export helpers ────────────────────────────────────────────────
def _export_response(data: str, filename: str, fmt: str):
    ct = "text/csv" if fmt == "csv" else "text/plain"
    return Response(data, media_type=ct,
                    headers={"Content-Disposition": f"attachment; filename={filename}.{fmt}"})

def to_csv(headers: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()

def to_markdown(headers: list[str], rows: list[list]) -> str:
    sep = "|" + "|".join("---" for _ in headers) + "|"
    lines = ["|" + "|".join(headers) + "|", sep]
    for r in rows:
        lines.append("|" + "|".join(str(c) for c in r) + "|")
    return "\n".join(lines) + "\n"


def _report_stats(report_data: dict) -> FT:
    return Div(cls="stats shadow mb-6")(
        Div(cls="stat")(
            Div("Pages", cls="stat-title"),
            Div(str(report_data["total_pages"]), cls="stat-value"),
        ),
        Div(cls="stat")(
            Div("Pages with Issues", cls="stat-title"),
            Div(str(report_data["summary"]["pages_with_issues"]), cls="stat-value"),
            Div(cls="stat-desc")(f"{report_data['summary']['total_issues']} total issues"),
        ),
        Div(cls="stat")(
            Div("Duplicate Titles", cls="stat-title"),
            Div(str(report_data["summary"]["duplicate_titles_count"]), cls="stat-value"),
        ),
        Div(cls="stat")(
            Div("Duplicate Descriptions", cls="stat-title"),
            Div(str(report_data["summary"]["duplicate_descriptions_count"]), cls="stat-value"),
        ),
    )

def _report_issues_table(report_data: dict) -> FT:
    return Div(cls="overflow-x-auto")(
        Table(cls="table table-zebra")(
            Thead(Tr(Th("Page"), Th("Issues"))),
            Tbody(*[
                Tr(
                    Td(A(url, href=url, target="_blank", cls="link link-primary text-sm")),
                    Td(Span(", ".join(data["issues"]), cls="text-xs text-error")),
                ) for url, data in sorted(
                    report_data["issues"].items(),
                    key=lambda x: len(x[1]["issues"]),
                    reverse=True
                )[:50]
            ]),
        ),
    )


THEMES = ["dim", "light", "dark", "coffee", "night", "winter"]

THEME_SCRIPT = Script("""
function setTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    localStorage.setItem('theme', t);
    document.querySelectorAll('.theme-btn').forEach(b => {
        b.classList.toggle('active', b.getAttribute('data-theme') === t);
    });
}
document.addEventListener('DOMContentLoaded', function() {
    var saved = localStorage.getItem('theme') || 'dim';
    setTheme(saved);
});
""")

CUSTOM_STYLE = Style("""
.htmx-indicator { opacity: 0; transition: opacity 200ms ease-in; }
.htmx-request .htmx-indicator { opacity: 1; }
.htmx-request.htmx-indicator { opacity: 1; }
.drawer-content .container { animation: fadeIn 0.2s ease-in; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
.theme-btn.active::before { content: "✓ "; }
main.container h1 { font-size: 1.5rem; font-weight: 700; margin-bottom: 0.5rem; }
main.container { padding-top: 0; }
""")

THEME_STYLE = Style("""
[data-theme="dim"] {
color-scheme: dark;
--color-base-100: oklch(30.857% 0.023 264.149);
--color-base-200: oklch(28.036% 0.019 264.182);
--color-base-300: oklch(26.346% 0.018 262.177);
--color-base-content: oklch(82.901% 0.031 222.959);
--color-primary: oklch(86.133% 0.141 139.549);
--color-primary-content: oklch(17.226% 0.028 139.549);
--color-secondary: oklch(73.375% 0.165 35.353);
--color-secondary-content: oklch(14.675% 0.033 35.353);
--color-accent: oklch(74.229% 0.133 311.379);
--color-accent-content: oklch(14.845% 0.026 311.379);
--color-neutral: oklch(24.731% 0.02 264.094);
--color-neutral-content: oklch(82.901% 0.031 222.959);
--color-info: oklch(86.078% 0.142 206.182);
--color-info-content: oklch(17.215% 0.028 206.182);
--color-success: oklch(86.171% 0.142 166.534);
--color-success-content: oklch(17.234% 0.028 166.534);
--color-warning: oklch(86.163% 0.142 94.818);
--color-warning-content: oklch(17.232% 0.028 94.818);
--color-error: oklch(82.418% 0.099 33.756);
--color-error-content: oklch(16.483% 0.019 33.756);
--radius-selector: 1rem;
--radius-field: 0.5rem;
--radius-box: 1rem;
--size-selector: 0.25rem;
--size-field: 0.25rem;
--border: 1px;
--depth: 0;
--noise: 0;
}
[data-theme="coffee"] {
color-scheme: dark;
--color-base-100: oklch(24% 0.023 329.708);
--color-base-200: oklch(21% 0.021 329.708);
--color-base-300: oklch(16% 0.019 329.708);
--color-base-content: oklch(72.354% 0.092 79.129);
--color-primary: oklch(71.996% 0.123 62.756);
--color-primary-content: oklch(14.399% 0.024 62.756);
--color-secondary: oklch(34.465% 0.029 199.194);
--color-secondary-content: oklch(86.893% 0.005 199.194);
--color-accent: oklch(42.621% 0.074 224.389);
--color-accent-content: oklch(88.524% 0.014 224.389);
--color-neutral: oklch(16.51% 0.015 326.261);
--color-neutral-content: oklch(83.302% 0.003 326.261);
--color-info: oklch(79.49% 0.063 184.558);
--color-info-content: oklch(15.898% 0.012 184.558);
--color-success: oklch(74.722% 0.072 131.116);
--color-success-content: oklch(14.944% 0.014 131.116);
--color-warning: oklch(88.15% 0.14 87.722);
--color-warning-content: oklch(17.63% 0.028 87.722);
--color-error: oklch(77.318% 0.128 31.871);
--color-error-content: oklch(15.463% 0.025 31.871);
--radius-selector: 1rem;
--radius-field: 0.5rem;
--radius-box: 1rem;
--size-selector: 0.25rem;
--size-field: 0.25rem;
--border: 1px;
--depth: 0;
--noise: 0;
}
[data-theme="night"] {
color-scheme: dark;
--color-base-100: oklch(20.768% 0.039 265.754);
--color-base-200: oklch(19.314% 0.037 265.754);
--color-base-300: oklch(17.86% 0.034 265.754);
--color-base-content: oklch(84.153% 0.007 265.754);
--color-primary: oklch(75.351% 0.138 232.661);
--color-primary-content: oklch(15.07% 0.027 232.661);
--color-secondary: oklch(68.011% 0.158 276.934);
--color-secondary-content: oklch(13.602% 0.031 276.934);
--color-accent: oklch(72.36% 0.176 350.048);
--color-accent-content: oklch(14.472% 0.035 350.048);
--color-neutral: oklch(27.949% 0.036 260.03);
--color-neutral-content: oklch(85.589% 0.007 260.03);
--color-info: oklch(68.455% 0.148 237.251);
--color-info-content: oklch(0% 0 0);
--color-success: oklch(78.452% 0.132 181.911);
--color-success-content: oklch(15.69% 0.026 181.911);
--color-warning: oklch(83.242% 0.139 82.95);
--color-warning-content: oklch(16.648% 0.027 82.95);
--color-error: oklch(71.785% 0.17 13.118);
--color-error-content: oklch(14.357% 0.034 13.118);
--radius-selector: 1rem;
--radius-field: 0.5rem;
--radius-box: 1rem;
--size-selector: 0.25rem;
--size-field: 0.25rem;
--border: 1px;
--depth: 0;
--noise: 0;
}
[data-theme="winter"] {
color-scheme: light;
--color-base-100: oklch(100% 0 0);
--color-base-200: oklch(97.466% 0.011 259.822);
--color-base-300: oklch(93.268% 0.016 262.751);
--color-base-content: oklch(41.886% 0.053 255.824);
--color-primary: oklch(56.86% 0.255 257.57);
--color-primary-content: oklch(91.372% 0.051 257.57);
--color-secondary: oklch(42.551% 0.161 282.339);
--color-secondary-content: oklch(88.51% 0.032 282.339);
--color-accent: oklch(59.939% 0.191 335.171);
--color-accent-content: oklch(11.988% 0.038 335.171);
--color-neutral: oklch(19.616% 0.063 257.651);
--color-neutral-content: oklch(83.923% 0.012 257.651);
--color-info: oklch(88.127% 0.085 214.515);
--color-info-content: oklch(17.625% 0.017 214.515);
--color-success: oklch(80.494% 0.077 197.823);
--color-success-content: oklch(16.098% 0.015 197.823);
--color-warning: oklch(89.172% 0.045 71.47);
--color-warning-content: oklch(17.834% 0.009 71.47);
--color-error: oklch(73.092% 0.11 20.076);
--color-error-content: oklch(14.618% 0.022 20.076);
--radius-selector: 1rem;
--radius-field: 0.5rem;
--radius-box: 1rem;
--size-selector: 0.25rem;
--size-field: 0.25rem;
--border: 1px;
--depth: 0;
--noise: 0;
}
""")

page_hdrs = daisy_hdrs + (THEME_SCRIPT, CUSTOM_STYLE, THEME_STYLE)


def _sidebar():
    return Ul(cls="menu p-4 w-64 min-h-full bg-base-200 text-base-content gap-1")(
        Li(cls="menu-title text-xs tracking-wider opacity-50 px-4 py-2")("Navigation"),
        Li(A(href="/", cls="flex items-center gap-3 px-4 py-2 rounded-lg transition-colors duration-150 hover:bg-base-300")(
            Span("📊", cls="text-lg"), Span("Dashboard"),
        )),
        Li(A(href="/settings", cls="flex items-center gap-3 px-4 py-2 rounded-lg transition-colors duration-150 hover:bg-base-300")(
            Span("⚙️", cls="text-lg"), Span("Settings"),
        )),
    )


def _navbar():
    return Div(cls="navbar bg-base-100 border-b border-base-300 px-4")(
        Div(cls="navbar-start gap-2")(
            Label(for_="drawer-toggle", cls="btn btn-ghost btn-square lg:hidden")("☰"),
            A("SEO Rat", href="/", cls="btn btn-ghost text-xl font-bold"),
        ),
        Div(cls="navbar-end gap-1")(
            Details(cls="dropdown dropdown-end")(
                Summary(cls="btn btn-ghost btn-sm")("🎨"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box z-[1] p-2 shadow-lg min-w-32")(
                    *[Li(Button(
                        {"dim": "Dim", "light": "Light", "dark": "Dark", "coffee": "Coffee", "night": "Night", "winter": "Winter"}.get(t, t.title()),
                        cls="theme-btn", data_theme=t,
                        onclick=f"setTheme('{t}')",
                    )) for t in THEMES],
                ),
            ),
        ),
    )


def _body_wrap(content, req):
    return Div(cls="min-h-screen bg-base-200 text-base-content")(
        Div(cls="drawer lg:drawer-open")(
            Input(type="checkbox", id="drawer-toggle", cls="drawer-toggle"),
            Div(cls="drawer-content flex flex-col min-h-screen")(
                _navbar(),
                Div(cls="max-w-7xl w-full mx-auto px-4 sm:px-6 py-6")(
                    *content,
                ),
                Footer(cls="footer footer-center bg-base-100 border-t border-base-300 p-4 text-sm text-base-content/40")(
                    Span("SEO Rat — Google Search Console Intelligence"),
                ),
            ),
            Div(cls="drawer-side z-30")(
                Label(for_="drawer-toggle", cls="drawer-overlay"),
                _sidebar(),
            ),
        ),
    )


app, rt = fast_app(
    hdrs=page_hdrs,
    body_wrap=_body_wrap,
    pico=False,
)


def _run_sync(id: int, site_url: str, days: int):
    with get_session() as session:
        secrets = CONFIG_DIR / "client_secrets.json"
        auth = GSCAuth(secrets_file=str(secrets))
        try:
            auth.get_credentials()
        except ValueError:
            _sync_progress[id] = {"status": "error", "msg": "GSC not authenticated"}
            return
        start_date, end_date = get_date_range("last_days", days=days)
        dates = get_missing_dates(session, site_url, start_date, end_date)
        total = len(dates)
        if total == 0:
            _sync_progress[id] = {"status": "done", "days": 0, "records": 0}
            return
        _sync_progress[id] = {"status": "running", "total": total, "done": 0, "records": 0}
        for date in dates:
            count = store_single_date(session, auth, site_url, date)
            _sync_progress[id].update(done=_sync_progress[id]["done"] + 1, records=_sync_progress[id]["records"] + count)
            time.sleep(1)
        p = _sync_progress[id]
        _sync_progress[id] = {"status": "done", "days": p["done"], "records": p["records"]}


def _run_index_check(id: int, site_url: str, sitemap_url: str):
    try:
        urls = fetch_sitemap_urls(sitemap_url)
    except Exception as e:
        _index_check_progress[id] = {"status": "error", "msg": str(e)}
        return
    total = len(urls)
    if total == 0:
        _index_check_progress[id] = {"status": "done", "total": 0, "successful": 0, "failed": 0}
        return
    _index_check_progress[id] = {"status": "running", "total": total, "done": 0, "successful": 0, "failed": 0}
    secrets = CONFIG_DIR / "client_secrets.json"
    auth = GSCAuth(secrets_file=str(secrets))
    try:
        auth.get_credentials()
    except ValueError:
        _index_check_progress[id] = {"status": "error", "msg": "GSC not authenticated"}
        return
    with get_session() as session:
        for i, url in enumerate(urls, 1):
            try:
                store_index_status(session, auth, site_url, url)
                _index_check_progress[id]["successful"] += 1
            except Exception:
                _index_check_progress[id]["failed"] += 1
            _index_check_progress[id]["done"] = i
            time.sleep(1)
    p = _index_check_progress[id]
    _index_check_progress[id] = {"status": "done", "total": total, "successful": p["successful"], "failed": p["failed"]}


NAV_CARD_ICONS = {
    "Top Pages": "📄",
    "Keywords": "🔑",
    "Wins": "🏆",
    "Index Status": "🔍",
    "Countries": "🌍",
    "Cannibalization": "🔗",
    "FAQ": "❓",
    "SERPWatcher": "📊",
    "Schema Check": "✅",
    "Articles": "📝",
    "SEO Report": "📋",
}

def render_nav_cards(id: int):
    items = [
        (top_pages, "Top Pages", "pages ranked by metrics"),
        (keywords, "Keywords", "search queries & trends"),
        (wins, "Wins", "opportunities to capture"),
        (index_status, "Index Status", "coverage & errors"),
        (countries, "Countries", "geo breakdown"),
        (canb, "Cannibalization", "duplicate content"),
        (faq, "FAQ", "schema questions"),
        (serpwatcher, "SERPWatcher", "tracked keywords"),
        (schema_check, "Schema Check", "structured data"),
        (articles, "Articles", "SEO inference & metadata"),
        (report, "SEO Report", "full site audit"),
    ]
    return Div(cls="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-3")(
        *[A(
            Div(cls="card-body items-center text-center px-3 py-4")(
                Span(NAV_CARD_ICONS.get(label, "📊"), cls="text-2xl mb-1"),
                Span(label, cls="font-medium text-sm"),
                Span(desc, cls="text-xs text-base-content/50 leading-tight"),
            ),
            href=route.to(id=id),
            cls="card card-border bg-base-100 hover:bg-base-200 hover:border-primary hover:shadow-md hover:-translate-y-0.5 transition-all duration-200 no-underline",
        ) for route, label, desc in items]
    )


SITE_TYPE_ICONS = {"quarto": "📘", "astro": "🚀", "hugo": "⚡", "wordpress": "🔵", "other": "🌐"}

FAVICON_CACHE = Path.home() / ".config" / "seo_rat" / "favicons"

def _favicon_url(domain: str, size: int = 32) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz={size}&default=emoji"

def render_site_card(w):
    domain = w.url.replace("https://", "").replace("http://", "").rstrip("/")
    fallback_emoji = SITE_TYPE_ICONS.get(w.site_type or "other", "🌐")
    return A(
        Div(cls="card-body p-5")(
            Div(cls="flex items-start justify-between")(
                Div(cls="flex items-center gap-3")(
                    Div(cls="w-8 h-8 flex items-center justify-center")(
                        Img(src=_favicon_url(domain), alt="", cls="w-8 h-8 rounded",
                            loading="lazy", style="display:block",
                            onerror=f"this.style.display='none';this.nextElementSibling.style.display='block'"),
                        Span(fallback_emoji, cls="text-2xl", style="display:none"),
                    ),
                    Div(
                        H3(w.name or domain, cls="card-title text-base"),
                        P(domain, cls="text-sm text-base-content/50 truncate max-w-48"),
                    ),
                ),
                Span(cls="badge badge-soft badge-sm")(w.site_type or "other"),
            ),
            Div(cls="flex items-center gap-2 mt-2 text-xs text-base-content/40")(
                Span(f"ID: {w.id}"),
                Span("·"),
                Span(w.lang or "en"),
            ) if w.desc else "",
        ),
        href=site.to(id=w.id),
        cls="card card-border bg-base-100 hover:bg-base-200 hover:shadow-lg hover:-translate-y-1 transition-all duration-200 no-underline group",
    )


def render_add_form():
    return Card(cls="card-border")(
        CardBody(
            Div(cls="flex items-center gap-3 mb-6")(
                Span("➕", cls="text-2xl"),
                H2("Add Website", cls="card-title text-xl"),
            ),
            Form(
                Div(cls="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4")(
                    Fieldset(FieldsetLegend("URL"),
                        Input(name="url", placeholder="https://example.com", required=True, cls="input w-full")),
                    Fieldset(FieldsetLegend("Name"),
                        Input(name="name", placeholder="My Site", required=True, cls="input w-full")),
                    Fieldset(FieldsetLegend("Site Type"),
                        Select(
                            Option("Quarto", value="quarto", selected=True),
                            Option("Astro", value="astro"),
                            Option("Hugo", value="hugo"),
                            Option("WordPress", value="wordpress"),
                            name="site_type", required=True, cls="select w-full")),
                    Fieldset(FieldsetLegend("Description"),
                        Input(name="desc", placeholder="Optional description", cls="input w-full")),
                    Fieldset(FieldsetLegend("Language"),
                        Input(name="lang", value="en", cls="input w-full")),
                    Fieldset(FieldsetLegend("Content Directory"),
                        Input(name="content_dir", placeholder="/path/to/content", cls="input w-full")),
                ),
                Div(cls="mt-6 flex justify-end gap-2")(
                    Btn("Clear", cls="-ghost", type="reset"),
                    Btn("Add Website", cls="-primary", type="submit"),
                ),
                action=add_website.to(), method="post",
            ),
        ),
    )


@rt
def index():
    with get_session() as session:
        websites = session.exec(select(Website)).all()
        total = len(websites)
        return Titled("SEO Rat Dashboard",
            P("Monitor your sites' Google Search performance", cls="text-base-content/60 mb-6"),
            Div(cls="flex items-center justify-between mb-4")(
                Span(""),
                Span(f"{total} site{'s' if total != 1 else ''}", cls="badge badge-soft badge-lg"),
            ) if websites else "",
            Div(cls="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4")(
                *map(render_site_card, websites),
            ) if websites else Div(cls="alert alert-soft alert-info mb-6")(
                Span("No websites added yet. Add your first website below."),
            ),
            Divider(cls="my-8"),
            render_add_form(),
        )


@rt
def add_website(url: str, name: str, site_type: str, desc: str = "", lang: str = "en", content_dir: str = ""):
    with get_session() as session:
        add_or_update_website(session, url=url, name=name, site_type=site_type, desc=desc, lang=lang, content_dir=content_dir)
    return RedirectResponse("/", status_code=303)


@rt
def delete_website(id: int):
    with get_session() as session:
        website = session.get(Website, id)
        if website:
            session.delete(website)
            session.commit()
    return RedirectResponse("/", status_code=303)


@rt("/site/{id}")
def site_redirect(id: int):
    return RedirectResponse(site.to(id=id), status_code=301)


def render_sync_progress(id: int):
    prog = _sync_progress.get(id)
    if not prog:
        return P("No sync in progress", cls="text-sm")
    if prog["status"] == "running":
        pct = int(prog["done"] / prog["total"] * 100)
        return Div(
            Progress(cls="-primary w-full", value=str(pct), max="100"),
            Span(f"Synced {prog['done']}/{prog['total']} dates ({pct}%)", cls="text-sm"),
            hx_get=sync_progress.to(id=id),
            hx_trigger="every 2s",
            hx_target="this",
            hx_swap="outerHTML",
        )
    if prog["status"] == "done":
        return Div(
            Progress(cls="-success w-full", value="100", max="100"),
            Span(f"Synced {prog['days']} days, {prog['records']} records. ", cls="text-sm text-success"),
            Link("Refresh", href=site.to(id=id), cls="-primary ml-2"),
        )
    if prog["status"] == "error":
        return Alert(Span(prog["msg"]), cls="-error")


@rt
def sync_start(id: int, days: int = 90):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
    _sync_progress[id] = {"status": "running"}
    Thread(target=_run_sync, args=(id, site_url, days), daemon=True).start()
    return Div(
        Progress(cls="-primary w-full", value="0", max="100"),
        Span("Starting sync...", cls="text-sm"),
        hx_get=sync_progress.to(id=id),
        hx_trigger="every 2s",
        hx_target="this",
        hx_swap="outerHTML",
    )


@rt
def sync_progress(id: int):
    return render_sync_progress(id)


@rt
def site(id: int):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))

        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"

        metrics = get_site_metrics(session, site_url)
        start, end = get_date_range("last_days", days=30)
        tp = get_top_pages(session, site_url, start, end, limit=10)

        delete_modal_id = f"delete-modal-{id}"

        return Title(website.name), Main(cls="container")(
            Div(cls="flex flex-col sm:flex-row sm:items-center gap-3 mb-6")(
                Div(cls="flex items-center gap-3")(
                    Img(src=_favicon_url(domain, 48), alt="", cls="w-10 h-10 rounded hidden sm:block",
                        loading="lazy",
                        onerror=f"this.style.display='none'"),
                    Div(
                        H1(website.name, cls="text-2xl font-bold"),
                        P(website.url, cls="text-sm text-base-content/50"),
                    ),
                ),
                Div(cls="sm:ml-auto flex items-center gap-2", id="sync-btn-area")(
                    Button("Delete", cls="btn btn-soft btn-error btn-sm",
                           onclick=f"document.getElementById('{delete_modal_id}').showModal()"),
                    Btn("Sync GSC Data",
                        cls="-outline -primary -sm",
                        hx_post=sync_start.to(id=id),
                        hx_target="#sync-btn-area",
                        hx_swap="innerHTML",
                    ),
                ),
            ),
            Dialog(id=delete_modal_id, cls="modal")(
                Div(cls="modal-box")(
                    H3("Delete Website?", cls="font-bold text-lg mb-2"),
                    P(f"Are you sure you want to delete {website.name}? All associated data will be removed.", cls="text-base-content/60 mb-6"),
                    Div(cls="modal-action")(
                        Form(method="dialog")(Button("Cancel", cls="btn btn-ghost")),
                        Form(method="post", action=delete_website.to(), cls="inline")(
                            Input(type="hidden", name="id", value=str(id)),
                            Button("Delete", cls="btn btn-error", type="submit"),
                        ),
                    ),
                ),
                Form(method="dialog", cls="modal-backdrop")(Button("close")),
            ),
            render_metrics(metrics),
            Details(cls="card card-border bg-base-100 mb-6")(
                Summary(cls="card-body cursor-pointer list-none [&::-webkit-details-marker]:hidden")(
                    Div(cls="flex items-center justify-between")(
                        H2("Top Pages", cls="card-title text-lg"),
                        Span("▼", cls="text-base-content/40 transition-transform [[open]_&]:rotate-180"),
                    ),
                ),
                Div(cls="card-body pt-0")(
                    Div(cls="flex items-center justify-between mb-3")(
                        Span(cls="text-sm text-base-content/60")("pages ranked by metrics"),
                        A("View all →", href=top_pages.to(id=id), cls="link link-primary text-sm"),
                    ),
                    render_top_pages_table(tp),
                ),
            ),
            Div(
                H2("Site Tools", cls="text-lg font-semibold mb-4"),
                render_nav_cards(id),
            ),
        ),


def get_site_metrics(session, site_url, days=30):
    start, end = get_date_range("last_days", days=days)
    q = (
        select(
            func.sum(GSCAnalytics.clicks).label("clicks"),
            func.sum(GSCAnalytics.impressions).label("impressions"),
            func.avg(GSCAnalytics.position).label("avg_position"),
            func.avg(GSCAnalytics.ctr).label("avg_ctr"),
        )
        .where(GSCAnalytics.site_url == site_url)
        .where(GSCAnalytics.date >= start)
        .where(GSCAnalytics.date <= end)
    )
    result = session.exec(q).first()
    return {
        "clicks": result.clicks or 0,
        "impressions": result.impressions or 0,
        "avg_position": round(result.avg_position or 0, 1),
        "avg_ctr": round((result.avg_ctr or 0) * 100, 2),
    }


METRIC_ICONS = {
    "clicks": "👆",
    "impressions": "👁️",
    "avg_position": "📍",
    "avg_ctr": "📈",
}

def render_metrics(metrics):
    items = [
        ("clicks", f"{metrics['clicks']:,}", "Clicks (30d)"),
        ("impressions", f"{metrics['impressions']:,}", "Impressions (30d)"),
        ("avg_position", f"{metrics['avg_position']:.1f}", "Avg Position"),
        ("avg_ctr", f"{metrics['avg_ctr']:.1f}%", "Avg CTR"),
    ]
    return Div(cls="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6")(
        *[Div(cls="stat bg-base-100 rounded-xl shadow-sm border border-base-200 p-4")(
            Div(cls="flex items-center gap-3 mb-2")(
                Span(METRIC_ICONS.get(key, "📊"), cls="text-xl"),
                Span(label, cls="text-sm font-medium text-base-content/60"),
            ),
            Div(value, cls="text-2xl font-bold"),
        ) for key, value, label in items],
    )


def render_top_pages_table(rows):
    if not rows:
        return P("No top pages data")
    return Table(
        Thead(Tr(Th("Page"), Th("Clicks"), Th("Impr"), Th("Pos"), Th("CTR"))),
        Tbody(*[
            Tr(
                Td(r["page"][:60] + "..." if len(r["page"]) > 60 else r["page"]),
                Td(f"{r['total_clicks']:,}"),
                Td(f"{r['total_impressions']:,}"),
                Td(f"{r['avg_position']:.1f}"),
                Td(f"{r['avg_ctr'] * 100:.1f}%"),
            ) for r in rows[:10]
        ]),
        cls="-zebra",
    )


SORT_OPTIONS = [
    ("clicks", "Most Clicks"),
    ("impressions", "Most Impressions"),
    ("ctr", "Best CTR"),
    ("position", "Best Position"),
]

DAYS_OPTIONS = [(7, "7 days"), (30, "30 days"), (90, "90 days"), (365, "1 year")]


def render_top_pages_filters(id: int, current_sort: str, current_days: int, current_country: str, countries: list[tuple[str, str]]):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Sort:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(label, href=top_pages.to(id=id, sort=key, days=current_days, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if key == current_sort else ""))
              for key, label in SORT_OPTIONS]
        ),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=top_pages.to(id=id, sort=current_sort, days=d, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Country:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="sort", value=current_sort, type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Select(
                *[Option(label, value=v, selected=(v == current_country)) for v, label in countries],
                name="country", cls="select select-sm w-44",
                onchange="this.form.submit()",
            ),
            action=top_pages.to(), method="get",
        ),
    )


def render_top_pages_full(rows):
    if not rows:
        return P("No top pages data")
    return Table(
        Thead(Tr(Th("#"), Th("Page"), Th("Clicks"), Th("Impr"), Th("Pos"), Th("CTR"))),
        Tbody(*[
            Tr(
                Td(str(i + 1)),
                Td(r["page"][:60] + "..." if len(r["page"]) > 60 else r["page"]),
                Td(f"{r['total_clicks']:,}"),
                Td(f"{r['total_impressions']:,}"),
                Td(f"{r['avg_position']:.1f}"),
                Td(f"{r['avg_ctr'] * 100:.1f}%"),
            ) for i, r in enumerate(rows[:50])
        ]),
        cls="-zebra",
    )


@rt
def top_pages(id: int, sort: str = "clicks", days: int = 30, country: str = "", export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        start, end = get_date_range("last_days", days=days)
        rows = get_top_pages(session, site_url, start, end, limit=200, country=country or None)

        key = {"clicks": "total_clicks", "impressions": "total_impressions",
               "ctr": "avg_ctr", "position": "avg_position"}.get(sort, "total_clicks")
        reverse = sort != "position"
        rows.sort(key=lambda r: r[key], reverse=reverse)

        if export:
            h = ["Page", "Clicks", "Impressions", "Position", "CTR"]
            d = [[r["page"], r["total_clicks"], r["total_impressions"],
                   round(r["avg_position"], 1), f"{r['avg_ctr']*100:.1f}%"] for r in rows[:50]]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "top_pages", export)

        q = (
            select(GSCAnalytics.country, func.sum(GSCAnalytics.impressions).label("total"))
            .where(GSCAnalytics.site_url == site_url, GSCAnalytics.country != None)
            .group_by(GSCAnalytics.country)
            .order_by(func.sum(GSCAnalytics.impressions).desc())
            .limit(15)
        )
        countries = [("", "All Countries")]
        for r in session.exec(q):
            try:
                name = pycountry.countries.get(alpha_3=r.country.upper()).name
            except:
                name = r.country.upper()
            countries.append((r.country, name))

    label = dict(SORT_OPTIONS).get(sort, "Top Pages")
    days_label = dict(DAYS_OPTIONS).get(days, f"{days} days")
    country_name = dict(countries).get(country, "All Countries")
    return Title(f"{label} - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1(label, cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=top_pages.to(id=id, sort=sort, days=days, country=country, export="csv"))),
                    Li(A("Markdown", href=top_pages.to(id=id, sort=sort, days=days, country=country, export="md"))),
                ),
            ),
        ),
        P(f"{website.url} — {days_label}, {country_name}", cls="text-base-content/60 mb-4"),
        render_top_pages_filters(id, sort, days, country, countries),
        render_top_pages_full(rows),
    ),


INTENTS = ["", "informational", "navigational", "transactional", "commercial", "comparison"]
INTENT_LABELS = {"": "All", "informational": "Info", "navigational": "Nav", "transactional": "Trans",
                 "commercial": "Comm", "comparison": "Comp"}
INTENT_COLORS = {"informational": "badge-info", "navigational": "badge-accent",
                 "transactional": "badge-success", "commercial": "badge-warning", "comparison": "badge-primary"}
TREND_ICONS = {"rising": "📈", "declining": "📉", "stable": "→"}
TREND_COLORS = {"rising": "text-success", "declining": "text-error", "stable": "text-base-content/60"}


def render_keywords_filters(id: int, current_intent: str, green_only: bool, issues_only: bool,
                            current_days: int, current_country: str, countries: list[tuple[str, str]]):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Intent:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(INTENT_LABELS[k], href=keywords.to(id=id, intent=k, green_only=green_only, issues_only=issues_only, days=current_days, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if k == current_intent else ""))
              for k in INTENTS]
        ),
        A("🟢 Green", href=keywords.to(id=id, intent=current_intent, green_only=not green_only, issues_only=issues_only, days=current_days, country=current_country),
          cls="btn btn-xs" + (" btn-success" if green_only else " btn-outline")),
        A("⚠ Issues", href=keywords.to(id=id, intent=current_intent, green_only=green_only, issues_only=not issues_only, days=current_days, country=current_country),
          cls="btn btn-xs" + (" btn-warning" if issues_only else " btn-outline")),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=keywords.to(id=id, intent=current_intent, green_only=green_only, issues_only=issues_only, days=d, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Country:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="intent", value=current_intent, type="hidden"),
            Input(name="green_only", value=str(green_only).lower(), type="hidden"),
            Input(name="issues_only", value=str(issues_only).lower(), type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Select(*[Option(label, value=v, selected=(v == current_country)) for v, label in countries],
                   name="country", cls="select select-sm w-44", onchange="this.form.submit()"),
            action=keywords.to(), method="get",
        ),
    )


def render_keyword_row(r: dict):
    trend = r.get("trend", "stable")
    intent = r.get("intent", "informational")
    return Tr(
        Td(A(r["query"], href=f"https://www.google.com/search?q={r['query']}", target="_blank", cls="link link-primary font-medium")),
        Td(f"{r['total_clicks']:,}"),
        Td(f"{r['total_impressions']:,}"),
        Td(f"{r['avg_position']:.1f}"),
        Td(Span(TREND_ICONS.get(trend, "→"), cls=TREND_COLORS.get(trend, ""))),
        Td(Span(INTENT_LABELS.get(intent, intent), cls=f"badge {INTENT_COLORS.get(intent, 'badge-ghost')} badge-sm")),
        Td("🟢" if r.get("green") else "—"),
        Td("⚠" if r.get("cannibal") else "—"),
        hx_get=keyword_pages.to(id=r["_site_id"], query=r["query"], days=r["_days"], country=r["_country"]),
        hx_target="#keyword-detail",
        hx_swap="innerHTML",
        cls="cursor-pointer hover:bg-base-200",
    )


def render_keywords_table(rows: list[dict]):
    if not rows:
        return Div(id="keywords-rows")(P("No matching keywords.", cls="text-sm text-base-content/60"))
    return Div(id="keywords-rows")(
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Keyword"), Th("Clicks"), Th("Impr"), Th("Pos"), Th("Trend"), Th("Intent"), Th("🟢"), Th("⚠"))),
                Tbody(*map(render_keyword_row, rows)),
                cls="-zebra",
            ),
        ),
    )


@rt
def keywords(id: int, intent: str = "", green_only: bool = False, issues_only: bool = False,
             days: int = 30, country: str = "", export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        start, end = get_date_range("last_days", days=days)

        rows = get_top_queries(session, site_url, start, end, country=country or None, limit=500)
        trends = detect_query_trends(session, site_url, days=days, limit=500)
        intents = classify_page_intents(session, site_url, start, end, country=country or None, limit=500)
        cannib = find_cannibalized(session, id, site_url, start, end)
        cannib_queries = {c["query"] for c in cannib.get("gsc_matches", [])}

        trend_map = {t["query"]: t for t in trends}
        intent_map = {i["query"]: i["intent"] for i in intents}

        table_rows = []
        for r in rows:
            tr = trend_map.get(r["query"], {})
            is_rising = tr.get("trend") == "rising"
            is_green = is_rising and tr.get("recent_impressions", 0) > 500 and tr.get("recent_position", 100) > 8
            row = dict(r, trend=tr.get("trend", "stable"), intent=intent_map.get(r["query"], "informational"),
                       green=is_green, cannibal=r["query"] in cannib_queries,
                       _site_id=id, _days=days, _country=country)
            table_rows.append(row)

        if intent:
            table_rows = [r for r in table_rows if r["intent"] == intent]
        if green_only:
            table_rows = [r for r in table_rows if r["green"]]
        if issues_only:
            table_rows = [r for r in table_rows if r["green"] or r["cannibal"]]

        if export:
            h = ["Keyword", "Clicks", "Impressions", "Position", "CTR", "Trend", "Intent"]
            d = [[r["query"], r.get("total_clicks", r.get("clicks", 0)),
                   r.get("total_impressions", r.get("impressions", 0)),
                   round(r.get("avg_position", r.get("position", 0)), 1),
                   f"{r.get('avg_ctr', r.get('ctr', 0))*100:.1f}%",
                   r["trend"], r["intent"]] for r in table_rows]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "keywords", export)

        q = (select(GSCAnalytics.country, func.sum(GSCAnalytics.impressions).label("total"))
             .where(GSCAnalytics.site_url == site_url, GSCAnalytics.country != None)
             .group_by(GSCAnalytics.country).order_by(func.sum(GSCAnalytics.impressions).desc()).limit(15))
        countries = [("", "All Countries")]
        for rc in session.exec(q):
            try:
                name = pycountry.countries.get(alpha_3=rc.country.upper()).name
            except:
                name = rc.country.upper()
            countries.append((rc.country, name))

    return Title(f"Keywords - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("Keywords", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=keywords.to(id=id, intent=intent, green_only=green_only, issues_only=issues_only, days=days, country=country, export="csv"))),
                    Li(A("Markdown", href=keywords.to(id=id, intent=intent, green_only=green_only, issues_only=issues_only, days=days, country=country, export="md"))),
                ),
            ),
        ),
        P(f"{website.url} — {dict(DAYS_OPTIONS).get(days, f'{days} days')}, {dict(countries).get(country, 'All Countries')}",
          cls="text-base-content/60 mb-4"),
        render_keywords_filters(id, intent, green_only, issues_only, days, country, countries),
        render_keywords_table(table_rows),
        Div(id="keyword-detail", cls="mt-4"),
    ),


@rt
def keywords_rows(id: int, intent: str = "", green_only: bool = False, issues_only: bool = False,
                  days: int = 30, country: str = ""):
    """HTMX fragment: just the table rows for filter refreshes."""
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        start, end = get_date_range("last_days", days=days)

        rows = get_top_queries(session, site_url, start, end, country=country or None, limit=500)
        trends = detect_query_trends(session, site_url, days=days, limit=500)
        intents = classify_page_intents(session, site_url, start, end, country=country or None, limit=500)
        cannib = find_cannibalized(session, id, site_url, start, end)
        cannib_queries = {c["query"] for c in cannib.get("gsc_matches", [])}

        trend_map = {t["query"]: t for t in trends}
        intent_map = {i["query"]: i["intent"] for i in intents}

        table_rows = []
        for r in rows:
            tr = trend_map.get(r["query"], {})
            is_rising = tr.get("trend") == "rising"
            is_green = is_rising and tr.get("recent_impressions", 0) > 500 and tr.get("recent_position", 100) > 8
            row = dict(r, trend=tr.get("trend", "stable"), intent=intent_map.get(r["query"], "informational"),
                       green=is_green, cannibal=r["query"] in cannib_queries,
                       _site_id=id, _days=days, _country=country)
            table_rows.append(row)

        if intent:
            table_rows = [r for r in table_rows if r["intent"] == intent]
        if green_only:
            table_rows = [r for r in table_rows if r["green"]]
        if issues_only:
            table_rows = [r for r in table_rows if r["green"] or r["cannibal"]]

    return render_keywords_table(table_rows)


@rt
def keyword_pages(id: int, query: str, days: int = 30, country: str = ""):
    """HTMX fragment: pages ranking for a keyword."""
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        start, end = get_date_range("last_days", days=days)
        pages = session.exec(
            select(GSCAnalytics.page, func.sum(GSCAnalytics.clicks).label("clicks"),
                   func.sum(GSCAnalytics.impressions).label("impressions"),
                   func.avg(GSCAnalytics.position).label("position"))
            .where(GSCAnalytics.site_url == site_url, GSCAnalytics.query == query,
                   GSCAnalytics.date.between(start, end))
            .group_by(GSCAnalytics.page).order_by(func.sum(GSCAnalytics.clicks).desc()).limit(10)
        ).all()
    return Div(
        H3(f"Pages for: {query}", cls="text-lg font-semibold mb-2"),
        Table(
            Thead(Tr(Th("Page"), Th("Clicks"), Th("Impr"), Th("Pos"))),
            Tbody(*[Tr(Td(p.page[:80] + "..." if len(p.page) > 80 else p.page),
                       Td(f"{p.clicks:,}"), Td(f"{p.impressions:,}"), Td(f"{p.position:.1f}"))
                    for p in pages]) if pages else Tr(Td("No data", colspan="4")),
            cls="-zebra",
        ) if pages else P("No ranking pages found.", cls="text-sm text-base-content/60"),
        cls="border border-base-300 rounded-lg p-4",
    )


def render_wins_filters(id: int, current_intent: str, current_days: int, current_country: str, countries: list[tuple[str, str]]):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Intent:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(INTENT_LABELS[k], href=wins.to(id=id, intent=k, days=current_days, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if k == current_intent else ""))
              for k in INTENTS]
        ),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=wins.to(id=id, intent=current_intent, days=d, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Country:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="intent", value=current_intent, type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Select(*[Option(label, value=v, selected=(v == current_country)) for v, label in countries],
                   name="country", cls="select select-sm w-44", onchange="this.form.submit()"),
            action=wins.to(), method="get",
        ),
    )


def render_wins_rows(rows: list[dict]):
    if not rows:
        return Div(id="wins-rows")(P("No opportunities found. Try a wider date range.", cls="text-sm text-base-content/60"))
    return Div(id="wins-rows")(
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Keyword"), Th("Clicks"), Th("Impr"), Th("Pos"), Th("CTR"), Th("Trend"), Th("Intent"))),
                Tbody(*[
                    Tr(
                        Td(A(r["query"], href=f"https://www.google.com/search?q={r['query']}", target="_blank", cls="link link-primary font-medium")),
                        Td(f"{r['total_clicks']:,}"),
                        Td(f"{r['total_impressions']:,}"),
                        Td(Span(f"{r['avg_position']:.1f}", cls="font-semibold text-warning")),
                        Td(f"{r['avg_ctr'] * 100:.1f}%"),
                        Td(Span(TREND_ICONS.get(r.get("trend", "stable"), "→"), cls=TREND_COLORS.get(r.get("trend", "")))),
                        Td(Span(INTENT_LABELS.get(r.get("intent", "informational"), ""), cls=f"badge {INTENT_COLORS.get(r.get('intent', ''), 'badge-ghost')} badge-sm")),
                    ) for r in rows
                ]),
                cls="-zebra",
            ),
        ),
    )


@rt
def wins(id: int, intent: str = "", days: int = 30, country: str = "", rows_only: bool = False, export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return (P("Website not found") if rows_only
                    else Titled("Website not found", P("Website not found")))
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        start, end = get_date_range("last_days", days=days)

        rows = get_wins(session, site_url, start, end, country=country or None, limit=200)
        trends = detect_query_trends(session, site_url, days=days, limit=500)
        intents = classify_page_intents(session, site_url, start, end, country=country or None, limit=500)
        trend_map = {t["query"]: t for t in trends}
        intent_map = {i["query"]: i["intent"] for i in intents}
        for r in rows:
            r["trend"] = trend_map.get(r["query"], {}).get("trend", "stable")
            r["intent"] = intent_map.get(r["query"], "informational")

        if intent:
            rows = [r for r in rows if r["intent"] == intent]

        if export:
            h = ["Keyword", "Clicks", "Impressions", "Position", "CTR", "Trend", "Intent"]
            d = [[r["query"], r["total_clicks"], r["total_impressions"],
                   round(r["avg_position"], 1), f"{r['avg_ctr']*100:.1f}%",
                   r["trend"], r["intent"]] for r in rows]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "wins", export)

        if rows_only:
            return render_wins_rows(rows)

        q = (select(GSCAnalytics.country, func.sum(GSCAnalytics.impressions).label("total"))
             .where(GSCAnalytics.site_url == site_url, GSCAnalytics.country != None)
             .group_by(GSCAnalytics.country).order_by(func.sum(GSCAnalytics.impressions).desc()).limit(15))
        countries = [("", "All Countries")]
        for rc in session.exec(q):
            try:
                name = pycountry.countries.get(alpha_3=rc.country.upper()).name
            except:
                name = rc.country.upper()
            countries.append((rc.country, name))

    return Title(f"Wins - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("Wins", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=wins.to(id=id, intent=intent, days=days, country=country, export="csv"))),
                    Li(A("Markdown", href=wins.to(id=id, intent=intent, days=days, country=country, export="md"))),
                ),
            ),
        ),
        P(f"High-impression, low-ranking keyword opportunities for {website.url}",
          cls="text-base-content/60 mb-4"),
        render_wins_filters(id, intent, days, country, countries),
        render_wins_rows(rows),
    ),


def render_index_stats(total, indexed, not_indexed):
    return Stats(
        Stat(StatTitle("Total Pages"), StatValue(str(total))),
        Stat(StatTitle("Indexed"), StatValue(str(indexed), cls="text-success")),
        Stat(StatTitle("Not Indexed"), StatValue(str(not_indexed), cls="text-error")),
        cls="shadow w-full mb-6",
    )


def render_index_reason_group(id: int, reason, pages):
    count = len(pages)
    group_id = f"index-group-{hash(reason) % 10000}"
    return Div(cls="mb-6")(
        H2(f"⚠ {reason} ({count})", cls="text-lg font-semibold mb-2"),
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Page URL"), Th("Verdict"), Th("Last Crawl"), Th("Indexing"), Th("Robots"))),
                Tbody(*[
                    Tr(
                        Td(A(p.page_url[:60] + ("..." if len(p.page_url) > 60 else ""),
                             href=p.page_url, target="_blank",
                             cls="link link-primary text-sm")),
                        Td(Span(p.verdict, cls="badge badge-sm badge-error")),
                        Td(p.last_crawl_time[:10] if p.last_crawl_time else "—"),
                        Td(p.indexing_state or "—"),
                        Td(p.robots_txt_state or "—"),
                        hx_get=index_page_history.to(id=id, page_url=p.page_url),
                        hx_target=f"#history-{group_id}",
                        hx_swap="innerHTML",
                        cls="cursor-pointer hover:bg-base-200",
                    ) for p in pages
                ]),
                cls="-zebra",
            ),
        ),
        Div(id=f"history-{group_id}", cls="mt-2"),
    )


def render_index_check_progress(id: int):
    prog = _index_check_progress.get(id)
    if not prog:
        return P("No check in progress", cls="text-sm")
    if prog["status"] == "fetching":
        return Div(
            Progress(cls="-primary w-full", value="0", max="100"),
            Span("Fetching sitemap...", cls="text-sm"),
            hx_get=index_check_progress.to(id=id),
            hx_trigger="every 2s",
            hx_target="this",
            hx_swap="outerHTML",
        )
    if prog["status"] == "running":
        pct = int(prog["done"] / prog["total"] * 100) if prog["total"] else 0
        return Div(
            Progress(cls="-primary w-full", value=str(pct), max="100"),
            Span(f"Checked {prog['done']}/{prog['total']} pages ({pct}%) — {prog['successful']} ok, {prog['failed']} failed", cls="text-sm"),
            hx_get=index_check_progress.to(id=id),
            hx_trigger="every 2s",
            hx_target="this",
            hx_swap="outerHTML",
        )
    if prog["status"] == "done":
        return Div(
            Progress(cls="-success w-full", value="100", max="100"),
            Span(f"Done: {prog['successful']} pages checked, {prog['failed']} failed. ", cls="text-sm text-success"),
            Link("Refresh", href=index_status.to(id=id), cls="-primary ml-2"),
        )
    if prog["status"] == "error":
        return Alert(Span(prog["msg"]), cls="-error")


@rt
def index_check_start(id: int, sitemap_url: str):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
    _index_check_progress[id] = {"status": "fetching"}
    Thread(target=_run_index_check, args=(id, site_url, sitemap_url), daemon=True).start()
    return Div(
        Progress(cls="-primary w-full", value="0", max="100"),
        Span("Checking sitemap...", cls="text-sm"),
        hx_get=index_check_progress.to(id=id),
        hx_trigger="every 2s",
        hx_target="this",
        hx_swap="outerHTML",
    )


@rt
def index_check_progress(id: int):
    return render_index_check_progress(id)


@rt
def index_page_history(id: int, page_url: str):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        history = get_index_history(session, page_url)
    if not history:
        return P("No history for this page.", cls="text-sm text-base-content/60")
    return Div(cls="border border-base-300 rounded-lg p-3 bg-base-200")(
        H3(f"History: {page_url[:80]}{'...' if len(page_url) > 80 else ''}", cls="text-sm font-semibold mb-2"),
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Checked At"), Th("Verdict"), Th("Coverage State"), Th("Last Crawl"), Th("Indexing"), Th("Robots"))),
                Tbody(*[
                    Tr(
                        Td(h.checked_at.strftime("%Y-%m-%d %H:%M") if h.checked_at else "—"),
                        Td(Span(h.verdict, cls="badge badge-sm " + ("badge-success" if h.verdict == "PASS" else "badge-error"))),
                        Td(h.coverage_state or "—"),
                        Td(h.last_crawl_time[:10] if h.last_crawl_time else "—"),
                        Td(h.indexing_state or "—"),
                        Td(h.robots_txt_state or "—"),
                    ) for h in history
                ]),
                cls="-zebra text-xs",
            ),
        ),
    )


@rt
def index_status(id: int):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"

        all_pages = get_index_status(session, site_url)
        indexed = [p for p in all_pages if p.verdict == "PASS"]
        not_indexed = [p for p in all_pages if p.verdict != "PASS"]
        grouped = get_not_indexed_by_reason(session, site_url)

    return Title(f"Index Status - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        H1("Index Status", cls="text-2xl font-bold mb-1"),
        P(f"{website.url}", cls="text-base-content/60 mb-4"),
        Div(cls="mb-6 p-4 border border-base-300 rounded-lg")(
            H2("Check Indexing", cls="text-lg font-semibold mb-2"),
            P("Fetch all pages from a sitemap and re-check their indexing status via GSC.", cls="text-sm text-base-content/60 mb-3"),
            Form(
                Input(name="id", value=str(id), type="hidden"),
                Div(cls="flex gap-2")(
                    Input(name="sitemap_url", placeholder="https://example.com/sitemap.xml", required=True,
                          cls="input input-bordered flex-1"),
                    Btn("Start Check", cls="-primary", type="submit"),
                ),
                hx_post=index_check_start.to(),
                hx_target="#index-check-area",
                hx_swap="innerHTML",
            ),
            Div(id="index-check-area", cls="mt-2"),
        ),
        render_index_stats(len(all_pages), len(indexed), len(not_indexed)),
        Div(cls="mb-6 p-4 border border-base-300 rounded-lg")(
            H2("Page History", cls="text-lg font-semibold mb-2"),
            P("Look up full index check history for a specific page URL.", cls="text-sm text-base-content/60 mb-3"),
            Form(
                Input(name="id", value=str(id), type="hidden"),
                Div(cls="flex gap-2")(
                    Input(name="page_url", placeholder="https://example.com/page", required=True,
                          cls="input input-bordered flex-1"),
                    Btn("View History", cls="-primary", type="submit"),
                ),
                hx_post=index_page_history.to(),
                hx_target="#page-history-area",
                hx_swap="innerHTML",
            ),
            Div(id="page-history-area", cls="mt-2"),
        ),
        Div(cls="space-y-4")(
            *[render_index_reason_group(id, reason, pages) for reason, pages in grouped.items()]
        ) if grouped else (P("All pages are indexed.", cls="text-sm text-success") if all_pages else P("No index tracking data yet.", cls="text-sm text-base-content/60")),
    ),


COUNTRY_SORT_OPTIONS = [
    ("clicks", "Most Clicks"),
    ("impressions", "Most Impressions"),
    ("ctr", "Best CTR"),
    ("position", "Best Position"),
]


def _country_name(code):
    try:
        return pycountry.countries.get(alpha_3=code.upper()).name
    except AttributeError:
        return code.upper()


def _is_underperforming(r, site_avg_ctr):
    return (r["impressions"] > 1000 and r["avg_ctr"] and site_avg_ctr and
            r["avg_ctr"] < site_avg_ctr * 0.5)


def _render_ctr_vs_site(ctr, site_avg_ctr, impressions):
    if not ctr or not site_avg_ctr:
        return Span("—", cls="text-base-content/40")
    ratio = ctr / site_avg_ctr
    if ratio < 0.5 and impressions > 1000:
        return Span(f"{ratio:.1f}x", cls="text-error font-semibold")
    elif ratio > 1.5:
        return Span(f"{ratio:.1f}x", cls="text-success font-semibold")
    return Span(f"{ratio:.1f}x", cls="text-base-content/60")


def render_country_stats(rows, site_avg_ctr):
    total_clicks = sum(r["clicks"] for r in rows)
    total_impressions = sum(r["impressions"] for r in rows)
    underperforming = sum(1 for r in rows if _is_underperforming(r, site_avg_ctr))
    return Stats(
        Stat(StatTitle("Countries"), StatValue(str(len(rows)))),
        Stat(StatTitle("Total Clicks"), StatValue(f"{total_clicks:,}")),
        Stat(StatTitle("Total Impressions"), StatValue(f"{total_impressions:,}")),
        Stat(StatTitle("Underperforming"), StatValue(str(underperforming), cls="text-error")),
        cls="shadow w-full mb-6",
    )


def render_country_filters(id, current_sort, current_days, current_page_url):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Sort:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(label, href=countries.to(id=id, sort=key, days=current_days, page_url=current_page_url),
                cls="join-item btn btn-xs" + (" btn-primary" if key == current_sort else ""))
              for key, label in COUNTRY_SORT_OPTIONS]
        ),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=countries.to(id=id, sort=current_sort, days=d, page_url=current_page_url),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Page:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="sort", value=current_sort, type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Input(name="page_url", value=current_page_url,
                  placeholder="https://...", cls="input input-bordered input-xs w-44"),
            action=countries.to(), method="get",
        ),
    )


def render_country_table(rows, site_avg_ctr):
    if not rows:
        return P("No country data for this period.", cls="text-sm text-base-content/60")
    total_clicks = sum(r["clicks"] for r in rows)
    return Div(cls="overflow-x-auto")(
        Table(
            Thead(Tr(Th("#"), Th("Country"), Th("Clicks"), Th("Share"), Th("Impr"), Th("Pos"), Th("CTR"), Th("vs Site Avg"))),
            Tbody(*[
                Tr(
                    Td(str(i + 1)),
                    Td(_country_name(r["country"])),
                    Td(f"{r['clicks']:,}"),
                    Td(f"{(r['clicks'] / total_clicks * 100):.1f}%" if total_clicks else "0%"),
                    Td(f"{r['impressions']:,}"),
                    Td(f"{r['avg_position']:.1f}" if r["avg_position"] else "—"),
                    Td(f"{r['avg_ctr'] * 100:.1f}%" if r["avg_ctr"] else "—"),
                    Td(_render_ctr_vs_site(r["avg_ctr"], site_avg_ctr, r["impressions"])),
                    cls="hover:bg-base-200" + (" bg-warning/10" if _is_underperforming(r, site_avg_ctr) else ""),
                ) for i, r in enumerate(rows)
            ]),
            cls="-zebra",
        ),
    )


@rt
def countries(id: int, sort: str = "clicks", days: int = 30, page_url: str = "", export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        start, end = get_date_range("last_days", days=days)
        rows = get_country_breakdown(session, site_url, start, end,
                                     page_url=page_url or None, limit=50)

        key = {"clicks": "clicks", "impressions": "impressions",
               "ctr": "avg_ctr", "position": "avg_position"}.get(sort, "clicks")
        reverse = sort != "position"
        rows.sort(key=lambda r: r[key] or 0, reverse=reverse)

        total_clicks = sum(r["clicks"] for r in rows)
        total_impressions = sum(r["impressions"] for r in rows)
        site_avg_ctr = (total_clicks / total_impressions) if total_impressions else None

        if export:
            h = ["Country", "Clicks", "Share", "Impressions", "Position", "CTR"]
            d = []
            for r in rows:
                try:
                    cname = pycountry.countries.get(alpha_3=r["country"].upper()).name
                except:
                    cname = r["country"].upper()
                share = f"{r['clicks']/total_clicks*100:.1f}%" if total_clicks else "0%"
                ctr = f"{r['avg_ctr']*100:.1f}%" if r.get("avg_ctr") is not None else ""
                d.append([cname, r["clicks"], share, r["impressions"],
                          round(r["avg_position"], 1), ctr])
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "countries", export)

    return Title(f"Country Breakdown - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("Country Breakdown", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=countries.to(id=id, sort=sort, days=days, page_url=page_url, export="csv"))),
                    Li(A("Markdown", href=countries.to(id=id, sort=sort, days=days, page_url=page_url, export="md"))),
                ),
            ),
        ),
        P(f"{website.url} — {dict(DAYS_OPTIONS).get(days, f'{days} days')}", cls="text-base-content/60 mb-4"),
        render_country_filters(id, sort, days, page_url),
        render_country_stats(rows, site_avg_ctr),
        render_country_table(rows, site_avg_ctr),
    ),


def _enrich_page_metrics(session, site_url, query, pages, start, end):
    if not pages:
        return {}
    result = session.exec(
        select(
            GSCAnalytics.page,
            func.sum(GSCAnalytics.clicks).label("clicks"),
            func.sum(GSCAnalytics.impressions).label("impressions"),
            func.avg(GSCAnalytics.position).label("avg_position"),
        ).where(
            GSCAnalytics.site_url == site_url,
            GSCAnalytics.query == query,
            GSCAnalytics.date.between(start, end),
            GSCAnalytics.page.in_(pages),
        ).group_by(GSCAnalytics.page)
    ).all()
    return {r.page: {"clicks": r.clicks or 0, "impressions": r.impressions or 0,
                     "avg_position": round(r.avg_position, 1) if r.avg_position else None}
            for r in result}


def render_cannibal_group(group, metrics_map, source_label):
    kw = group.get("keyword") or group.get("query", "")
    pages = group.get("pages", [])
    return Div(cls="mb-4 p-4 border border-base-300 rounded-lg")(
        Div(cls="flex items-center justify-between mb-2")(
            H3(kw, cls="font-semibold text-base"),
            Span(f"{len(pages)} pages · {source_label}", cls="text-xs text-base-content/40"),
        ),
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Page"), Th("Clicks"), Th("Impr"), Th("Pos"))),
                Tbody(*[
                    Tr(
                        Td(A(p[:70] + ("..." if len(p) > 70 else ""),
                             href=p, target="_blank", cls="link link-primary text-sm")),
                        Td(f"{metrics_map.get(p, {}).get('clicks', 0):,}"),
                        Td(f"{metrics_map.get(p, {}).get('impressions', 0):,}"),
                        Td(str(metrics_map.get(p, {}).get("avg_position") or "—")),
                    ) for p in pages
                ]),
                cls="-zebra text-sm",
            ),
        ),
    )


@rt
def canb(id: int, days: int = 30, export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        start, end = get_date_range("last_days", days=days)
        cannib = find_cannibalized(session, id, site_url, start, end)
        exact = cannib.get("exact_matches", [])
        gsc = cannib.get("gsc_matches", [])

        for group in exact:
            group["metrics"] = _enrich_page_metrics(session, site_url, group["keyword"],
                                                    group["pages"], start, end)
        for group in gsc:
            group["metrics"] = _enrich_page_metrics(session, site_url, group["query"],
                                                    group["pages"], start, end)

        if export:
            h = ["Source", "Keyword", "Page", "Clicks", "Impressions", "Position"]
            d = []
            for g in exact:
                kw = g.get("keyword", "")
                for p in g.get("pages", []):
                    m = g["metrics"].get(p, {})
                    d.append(["exact", kw, p, m.get("clicks", 0), m.get("impressions", 0),
                              m.get("avg_position", "—")])
            for g in gsc:
                kw = g.get("query", "")
                for p in g.get("pages", []):
                    m = g["metrics"].get(p, {})
                    d.append(["gsc", kw, p, m.get("clicks", 0), m.get("impressions", 0),
                              m.get("avg_position", "—")])
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "cannibalization", export)

    total = len(exact) + len(gsc)
    return Title(f"Cannibalization - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("Cannibalization", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=canb.to(id=id, days=days, export="csv"))),
                    Li(A("Markdown", href=canb.to(id=id, days=days, export="md"))),
                ),
            ),
        ),
        P(f"{website.url} — {dict(DAYS_OPTIONS).get(days, f'{days} days')}", cls="text-base-content/60 mb-4"),
        Stats(
            Stat(StatTitle("Total Groups"), StatValue(str(total))),
            Stat(StatTitle("Exact Keyword Matches"), StatValue(str(len(exact)))),
            Stat(StatTitle("GSC Ranking Overlap"), StatValue(str(len(gsc)))),
            cls="shadow w-full mb-6",
        ) if total else P("No cannibalization detected.", cls="text-sm text-success"),
        Div(cls="space-y-4")(
            *[render_cannibal_group(g, g["metrics"], "focus keyword")
              for g in exact],
            *[render_cannibal_group(g, g["metrics"], "GSC overlap")
              for g in gsc],
        ) if total else "",
    ),


FAQ_SORT_OPTIONS = [
    ("impressions", "Most Impressions"),
    ("clicks", "Most Clicks"),
    ("position", "Best Position"),
]


def render_faq_filters(id, current_sort, current_days, current_page_url):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Sort:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(label, href=faq.to(id=id, sort=key, days=current_days, page_url=current_page_url),
                cls="join-item btn btn-xs" + (" btn-primary" if key == current_sort else ""))
              for key, label in FAQ_SORT_OPTIONS]
        ),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=faq.to(id=id, sort=current_sort, days=d, page_url=current_page_url),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Page:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="sort", value=current_sort, type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Input(name="page_url", value=current_page_url,
                  placeholder="https://...", cls="input input-bordered input-xs w-44"),
            action=faq.to(), method="get",
        ),
    )


def render_faq_table(rows):
    if not rows:
        return P("No FAQ-type queries found for this period.", cls="text-sm text-base-content/60")
    return Div(cls="overflow-x-auto")(
        Table(
            Thead(Tr(Th("#"), Th("Question"), Th("Impr"), Th("Clicks"), Th("Pos"))),
            Tbody(*[
                Tr(
                    Td(str(i + 1)),
                    Td(r["query"], cls="font-medium"),
                    Td(f"{r['total_impressions']:,}"),
                    Td(f"{r['total_clicks']:,}"),
                    Td(f"{r['avg_position']:.1f}"),
                ) for i, r in enumerate(rows)
            ]),
            cls="-zebra",
        ),
    )


@rt
def faq(id: int, sort: str = "impressions", days: int = 30, page_url: str = "", export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        start, end = get_date_range("last_days", days=days)
        rows = get_top_queries(session, site_url, start, end,
                               page_path=page_url or None, limit=200,
                               sort_by="impressions")
        faqs = extract_faq_queries(rows)
        faq_set = set(faqs)
        faq_rows = [r for r in rows if r["query"] in faq_set]

        key = {"impressions": "total_impressions", "clicks": "total_clicks",
               "position": "avg_position"}.get(sort, "total_impressions")
        reverse = sort != "position"
        faq_rows.sort(key=lambda r: r[key] or 0, reverse=reverse)

        if export:
            h = ["Question", "Impressions", "Clicks", "Position"]
            d = [[r["query"], r["total_impressions"], r["total_clicks"],
                   round(r["avg_position"], 1)] for r in faq_rows]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "faq", export)

    total_imp = sum(r["total_impressions"] for r in faq_rows)
    total_clicks = sum(r["total_clicks"] for r in faq_rows)
    days_label = dict(DAYS_OPTIONS).get(days, f"{days} days")
    page_label = f" — {page_url}" if page_url else ""
    return Title(f"FAQ Opportunities - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("FAQ Schema Opportunities", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=faq.to(id=id, sort=sort, days=days, page_url=page_url, export="csv"))),
                    Li(A("Markdown", href=faq.to(id=id, sort=sort, days=days, page_url=page_url, export="md"))),
                ),
            ),
        ),
        P(f"Questions people search for that could become FAQ schema on your pages — {days_label}{page_label}",
          cls="text-base-content/60 mb-4"),
        Stats(
            Stat(StatTitle("FAQ Queries"), StatValue(str(len(faq_rows)))),
            Stat(StatTitle("Total Impressions"), StatValue(f"{total_imp:,}")),
            Stat(StatTitle("Total Clicks"), StatValue(f"{total_clicks:,}")),
            cls="shadow w-full mb-6",
        ),
        render_faq_filters(id, sort, days, page_url),
        render_faq_table(faq_rows),
    ),


def _render_mover_row(m):
    change = m.get("change", 0)
    is_improvement = change < 0
    icon = "▲" if is_improvement else "▼"
    color = "text-success" if is_improvement else "text-error"
    return Div(cls="flex items-center justify-between text-sm")(
        Span(m["query"], cls="truncate max-w-[200px]"),
        Span(f"{icon} {abs(change):.1f} pos", cls=f"{color} font-medium"),
    )


def _compute_dominance_index(keywords_data):
    if not keywords_data:
        return 0, "No data"
    positions = [d.get("avg_position") for d in keywords_data if d.get("avg_position")]
    if not positions:
        return 0, "No position data"
    avg_pos = sum(positions) / len(positions)
    score = max(0, round(100 - (avg_pos - 1) * 100 / 19))
    label = "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair" if score >= 40 else "Needs Work"
    return score, label


def _render_position_history(history):
    if not history:
        return P("No position data yet.", cls="text-xs text-base-content/40")
    recent = history[-30:]
    positions = [h.get("avg_position") for h in recent if h.get("avg_position")]
    min_pos = min(positions) if positions else 1
    max_pos = max(positions) if positions else 20
    pos_range = max(max_pos - min_pos, 5)
    return Div(
        Div(cls="flex gap-0.5 items-end h-7", style="position: relative;")(
            *[_render_position_bar(h.get("avg_position"), h.get("date", ""), min_pos, pos_range)
              for h in recent]
        ),
        Div(cls="flex justify-between text-[10px] text-base-content/30 mt-0.5")(
            Span(recent[0]["date"][5:] if recent else ""),
            Span(f"pos {min_pos:.0f}–{max_pos:.0f}"),
            Span(recent[-1]["date"][5:] if recent else ""),
        ),
    )


def _render_position_bar(pos, date, min_pos=1, pos_range=10):
    if not pos:
        return Div(cls="w-3 h-1 bg-base-300 rounded", data_tip="no data")
    rel = (pos - min_pos) / pos_range if pos_range else 0
    height = max(3, min(28, int(28 - rel * 24)))
    color = "bg-success" if pos <= 3 else "bg-warning" if pos <= 10 else "bg-error"
    return Div(cls=f"tooltip w-3 {color} rounded-sm", style=f"height: {height}px",
               data_tip=f"{date}: #{pos:.1f}")


@rt
def serpwatcher_add_keyword(id: int, keyword: str):
    with get_session() as session:
        add_tracked_keyword(session, id, keyword)
    return Div(
        Span(f"Added '{keyword}'", cls="text-sm text-success"),
        hx_get=serpwatcher_keywords_list.to(id=id),
        hx_target="#serpwatcher-keywords",
        hx_swap="innerHTML",
        hx_trigger="load",
    )


@rt
def serpwatcher_delete_keyword(id: int, keyword_id: int):
    with get_session() as session:
        delete_tracked_keyword(session, keyword_id)
    return Div(
        hx_get=serpwatcher_keywords_list.to(id=id),
        hx_target="#serpwatcher-keywords",
        hx_swap="innerHTML",
        hx_trigger="load",
    )


@rt
def serpwatcher_keywords_list(id: int, days: int = 30):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        keywords = get_tracked_keywords(session, id)
        if not keywords:
            return P("No keywords being tracked. Add one above.", cls="text-sm text-base-content/60")

        start, end = get_date_range("last_days", days=days)
        kw_set = {k.keyword.lower() for k in keywords}
        trends = get_trends(session, site_url, start, end, dimension="query")
        from collections import defaultdict
        by_kw = defaultdict(list)
        for r in trends:
            if r["query"] and r["query"].lower() in kw_set:
                by_kw[r["query"]].append({"date": r["date"], "avg_position": round(r["avg_position"], 1) if r["avg_position"] else None,
                                           "clicks": r["clicks"] or 0, "impressions": r["impressions"] or 0})

    cards = []
    for kw in keywords:
        hist = by_kw.get(kw.keyword, [])
        hist.sort(key=lambda h: h["date"])
        latest = hist[-1] if hist else {}
        first = hist[0] if hist else {}
        cur_pos = latest.get("avg_position")
        avg_pos = round(sum(h["avg_position"] for h in hist if h["avg_position"]) / len([h for h in hist if h["avg_position"]]), 1) if any(h["avg_position"] for h in hist) else None
        total_clicks = sum(h["clicks"] for h in hist)
        total_impressions = sum(h["impressions"] for h in hist)

        trend_icon = Span("—", cls="text-base-content/30 text-xs")
        trend_text = ""
        if cur_pos and first.get("avg_position") and cur_pos != first["avg_position"]:
            diff = cur_pos - first["avg_position"]
            direction = "up" if diff < 0 else "down"
            cls = "text-success" if diff < 0 else "text-error"
            icon = "▲" if diff < 0 else "▼"
            trend_icon = Span(f"{icon} {abs(diff):.1f}", cls=f"{cls} text-xs font-semibold")
            trend_text = f"{'better' if diff < 0 else 'worse'}"

        cards.append(Div(cls="card card-bordered bg-base-100 mb-3")(
            Div(cls="card-body p-4")(
                Div(cls="flex items-center justify-between")(
                    Div(cls="flex items-center gap-3")(
                        H3(kw.keyword, cls="card-title text-base font-semibold"),
                        Span(f"now #{cur_pos:.1f}" if cur_pos else "no rank", cls="badge badge-sm " + ("badge-success" if cur_pos and cur_pos <= 3 else "badge-warning" if cur_pos and cur_pos <= 10 else "badge-error" if cur_pos else "badge-ghost")),
                        trend_icon,
                    ),
                    Btn("✕", cls="btn btn-ghost btn-xs text-error",
                        hx_post=serpwatcher_delete_keyword.to(id=id, keyword_id=kw.id),
                        hx_target="#serpwatcher-keywords",
                        hx_swap="innerHTML",
                    ),
                ),
                Div(cls="flex gap-4 text-xs text-base-content/60 mt-1")(
                    Span(f"avg {avg_pos}" if avg_pos else ""),
                    Span(f"{total_clicks:,} clicks"),
                    Span(f"{total_impressions:,} impressions"),
                    Span(f"{days}d range"),
                ),
                Div(cls="mt-2"),
                _render_position_history(hist),
            ),
        ))

    return Div(*cards)


@rt
def serpwatcher(id: int, days: int = 30):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"
        keywords = get_tracked_keywords(session, id)

        all_data = []
        if keywords:
            start, end = get_date_range("last_days", days=days)
            kw_set = {k.keyword.lower() for k in keywords}
            trends = get_trends(session, site_url, start, end, dimension="query")
            query_positions = {}
            for r in trends:
                if r["query"] and r["query"].lower() in kw_set and r["avg_position"]:
                    if r["query"] not in query_positions:
                        query_positions[r["query"]] = []
                    query_positions[r["query"]].append(r["avg_position"])
            for q, positions in query_positions.items():
                all_data.append({"query": q, "avg_position": round(sum(positions) / len(positions), 1)})

    dominance_score, dominance_label = _compute_dominance_index(all_data)
    days_label = dict(DAYS_OPTIONS).get(days, f"{days} days")

    top3 = sum(1 for d in all_data if d["avg_position"] and d["avg_position"] <= 3)
    top10 = sum(1 for d in all_data if d["avg_position"] and d["avg_position"] <= 10)
    losing = sum(1 for d in all_data if d["avg_position"] and d["avg_position"] > 20)

    movers = []
    if keywords:
        start, end = get_date_range("last_days", days=days)
        kw_set = {k.keyword.lower() for k in keywords}
        trends2 = get_trends(session, site_url, start, end, dimension="query")
        kw_trend = {}
        for r in trends2:
            if r["query"] and r["query"].lower() in kw_set and r["avg_position"]:
                kw_trend.setdefault(r["query"], []).append(r["avg_position"])
        for q, pos_list in kw_trend.items():
            pos_list.sort()
            if len(pos_list) >= 2:
                movers.append({"query": q, "first": pos_list[0], "last": pos_list[-1],
                               "change": pos_list[-1] - pos_list[0]})
        movers.sort(key=lambda x: abs(x["change"]), reverse=True)

    return Title(f"SERPWatcher - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        H1("SERPWatcher", cls="text-2xl font-bold mb-1"),
        P(f"Track keyword positions over time — {days_label}", cls="text-base-content/60 mb-4"),
        Div(cls="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6")(
            Div(cls="stat shadow rounded-lg p-4")(
                Div("Tracked Keywords", cls="stat-title"),
                Div(str(len(keywords)), cls="stat-value"),
            ),
            Div(cls="stat shadow rounded-lg p-4")(
                Div("Dominance Index", cls="stat-title"),
                Div(str(dominance_score), cls="stat-value"),
                Div(dominance_label, cls="stat-desc"),
            ),
            Div(cls="stat shadow rounded-lg p-4")(
                Div("Rank Distribution", cls="stat-title"),
                Div(cls="flex gap-3 text-sm mt-1")(
                    Span(f"🥇 {top3} top 3"),
                    Span(f"📋 {top10 - top3} top 10"),
                    Span(f"⚠️ {losing} >20"),
                ),
            ),
            Div(cls="stat shadow rounded-lg p-4")(
                Div("Period", cls="stat-title"),
                Div(days_label, cls="stat-value text-lg"),
                Div(cls="join mt-2")(
                    *[A(dl, href=serpwatcher.to(id=id, days=d),
                        cls="join-item btn btn-xs" + (" btn-primary" if d == days else ""))
                      for d, dl in [(7, "7d"), (30, "30d"), (90, "90d")]]
                ),
            ),
        ),
        Div(cls="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6")(
            Div(cls="p-4 border border-base-300 rounded-lg")(
                H2("Add Keyword", cls="text-lg font-semibold mb-1"),
                P("Track any keyword to monitor its position trend in GSC.", cls="text-xs text-base-content/50 mb-2"),
                Form(
                    Input(name="id", value=str(id), type="hidden"),
                    Div(cls="flex gap-2")(
                        Input(name="keyword", placeholder="e.g. seo tools", required=True,
                              cls="input input-bordered flex-1"),
                        Btn("Track", cls="-primary", type="submit"),
                    ),
                    hx_post=serpwatcher_add_keyword.to(),
                    hx_target="#serpwatcher-add-result",
                    hx_swap="innerHTML",
                ),
                Div(id="serpwatcher-add-result"),
            ),
            Div(cls="p-4 border border-base-300 rounded-lg")(
                H2("Top Movers", cls="text-lg font-semibold mb-1"),
                P("Biggest position changes in this period.", cls="text-xs text-base-content/50 mb-2"),
                Div(cls="space-y-1 max-h-32 overflow-y-auto")(
                    *[_render_mover_row(m) for m in movers[:8]] if movers else [P("Not enough data yet.", cls="text-xs text-base-content/40")],
                ) if movers else P("Track keywords to see movers.", cls="text-xs text-base-content/40"),
            ),
        ),
        Div(id="serpwatcher-keywords")(
            Div(
                Span("Loading...", cls="text-sm text-base-content/60"),
                hx_get=serpwatcher_keywords_list.to(id=id, days=days),
                hx_trigger="load",
                hx_swap="innerHTML",
            ),
        ),
    ),


def _schema_status_badge(s):
    if s.get("error"):
        return Span("Fetch Failed", cls="badge badge-error badge-sm")
    if not s["summary"]["total_schemas"]:
        return Span("No Schema", cls="badge badge-ghost badge-sm")
    valid = s["summary"]["valid_count"]
    total = s["summary"]["total_schemas"]
    cls = "badge-success" if valid == total else "badge-warning" if valid > 0 else "badge-error"
    return Span(f"{valid}/{total} valid", cls=f"badge {cls} badge-sm")


@rt
def schema_check(id: int, tab: str = "pages"):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
    tab_pages = "tab tab-active" if tab == "pages" else "tab"
    tab_url = "tab tab-active" if tab == "url" else "tab"
    return Title(f"Schema Check - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4 block"),
        H1("Schema Checker", cls="text-2xl font-bold mb-1"),
        P("Validate structured data against Google Rich Results spec.", cls="text-base-content/60 mb-4"),
        Div(cls="tabs tabs-bordered mb-6")(
            A("Site Pages", href=schema_check.to(id=id, tab="pages"), cls=tab_pages),
            A("Live URL", href=schema_check.to(id=id, tab="url"), cls=tab_url),
        ),
        Div(id="schema-content")(
            _render_schema_tab(id, tab),
        ),
    ),


def _render_schema_tab(id: int, tab: str):
    if tab == "url":
        return Div(
            Form(
                Input(name="id", value=str(id), type="hidden"),
                Div(cls="flex gap-2")(
                    Input(name="url", placeholder="https://example.com/page", required=True,
                          cls="input input-bordered flex-1"),
                    Btn("Validate", cls="-primary", type="submit"),
                ),
                hx_post=schema_check_url.to(),
                hx_target="#schema-result",
                hx_swap="innerHTML",
                hx_indicator="#schema-spinner",
            ),
            Div("Fetching and validating...",
                id="schema-spinner", cls="htmx-indicator mt-2 text-sm text-base-content/40"),
            Div(id="schema-result", cls="mt-4"),
        )
    with get_session() as session:
        from seo_rat.models import URLMapping, get_url_mapping
        articles = get_articles_by_website(session, id)
        mapping = get_url_mapping(session, id)
    seen = set()
    pages = []
    for a in articles:
        if a.url and a.url not in seen:
            seen.add(a.url)
            pages.append({"url": a.url, "label": a.url.rsplit("/", 1)[-1] or a.url, "id": f"art-{a.id}"})
    for m in mapping:
        if m.url not in seen:
            seen.add(m.url)
            pages.append({"url": m.url, "label": m.url.rsplit("/", 1)[-1] or m.url, "id": f"map-{m.id}"})
    if not pages:
        return P("No pages found. Run `seo-rat-report` via CLI first to sync URL mapping, or add articles.",
                 cls="text-sm text-base-content/60")
    return Div(
        Div(cls="flex items-center justify-between mb-2")(
            Span(f"{len(pages)} pages", cls="text-sm text-base-content/60"),
            Div(cls="flex gap-2")(
                Btn("Predict All", cls="btn btn-primary btn-sm",
                    hx_post=schema_predict_all.to(id=id),
                    hx_target="#schema-all-results",
                    hx_swap="innerHTML",
                    hx_indicator="#predict-all-spinner"),
                Btn("Validate All", cls="btn btn-outline btn-sm",
                    hx_post=schema_check_validate_all.to(id=id),
                    hx_target="#schema-all-results",
                    hx_swap="innerHTML",
                    hx_indicator="#validate-all-spinner"),
            ),
        ),
        Div(id="predict-all-spinner", cls="htmx-indicator text-sm text-base-content/40 inline-flex items-center gap-1")(
            Span(cls="loading loading-spinner loading-xs"),
            "Predicting schemas for all pages...",
        ),
        Div(id="validate-all-spinner", cls="htmx-indicator text-sm text-base-content/40 inline-flex items-center gap-1")(
            Span(cls="loading loading-spinner loading-xs"),
            "Validating all pages...",
        ),
        Div(cls="overflow-x-auto")(
            Table(cls="table table-zebra table-sm")(
            Thead(
                Tr(Th("URL"), Th("Status"), Th("Action"))
            ),
            Tbody(
                *[Tr(
                    Td(A(p["url"], href=p["url"], target="_blank",
                         cls="link link-primary text-sm truncate max-w-[400px] block")),
                    Td(Div(id=f"schema-status-{p['id']}", cls="text-sm")),
                    Td(Div(cls="flex flex-col gap-1")(
                        Div(cls="flex gap-1")(
                            Btn("Validate", cls="btn btn-outline btn-xs",
                                hx_post=schema_check_page.to(id=id, page_url=p["url"]),
                                hx_target=f"#schema-result-{p['id']}",
                                hx_swap="innerHTML"),
                            Btn("Predict Schema", cls="btn btn-outline btn-xs",
                                hx_post=schema_predict.to(id=id, page_url=p["url"]),
                                hx_target=f"#schema-result-{p['id']}",
                                hx_swap="innerHTML"),
                        ),
                        Div(id=f"schema-result-{p['id']}", cls="text-xs"),
                    )),
                ) for p in pages]
            ),
        )),
        Div(id="schema-all-results"),
    )


@rt
def schema_check_page(id: int, page_url: str):
    try:
        result = validate_page(page_url)
        return _render_schema_result(result)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Error: {e}"),
        )


@rt
def schema_predict(id: int, page_url: str):
    try:
        from trafilatura import fetch_url, extract
        html = fetch_url(page_url)
        if not html:
            return Div(cls="alert alert-warning text-sm")(
                Span("Could not fetch page content"),
            )
        content = extract(html, output_format="markdown", favor_recall=True,
                          include_tables=True, include_links=False, include_images=False)
        if not content:
            return Div(cls="alert alert-warning text-sm")(
                Span("Could not extract content from page"),
            )
        result = predict_schemas(content)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Prediction failed: {e}"),
        )
    items = result.get("suggestions", [])
    reasoning = result.get("reasoning", "")
    return Div(cls="space-y-1")(
        Div(cls="flex flex-wrap gap-x-4 gap-y-1")(
            *[Div(cls="flex items-center gap-2")(
                Span(item["type"], cls="text-xs font-medium w-20"),
                Progress(cls="-primary h-2", value=str(item["score"]), max="100"),
                Span(f"{item['score']}%", cls="text-xs text-base-content/60 w-8"),
            ) for item in items],
        ),
        P(reasoning, cls="text-xs text-base-content/50 mt-1"),
    )


@rt
def schema_check_validate_all(id: int):
    try:
        with get_session() as session:
            from seo_rat.models import get_url_mapping
            articles = get_articles_by_website(session, id)
            mapping = get_url_mapping(session, id)
        seen = set()
        urls = []
        for a in articles:
            if a.url and a.url not in seen:
                seen.add(a.url)
                urls.append(a.url)
        for m in mapping:
            if m.url not in seen:
                seen.add(m.url)
                urls.append(m.url)
        results = []
        for url in urls:
            result = validate_page(url)
            results.append(Div(
                H3(A(url, href=url, target="_blank", cls="link link-primary text-sm"), cls="text-sm font-semibold mb-2 mt-4"),
                _render_schema_result(result),
                Div(cls="divider"),
            ))
        if not results:
            return P("No pages found.", cls="text-sm text-base-content/60")
        return Div(*results)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Error: {e}"),
        )


@rt
def schema_predict_all(id: int):
    try:
        with get_session() as session:
            from seo_rat.models import get_url_mapping
            articles = get_articles_by_website(session, id)
            mapping = get_url_mapping(session, id)
        seen = set()
        urls = []
        for a in articles:
            if a.url and a.url not in seen:
                seen.add(a.url)
                urls.append(a.url)
        for m in mapping:
            if m.url not in seen:
                seen.add(m.url)
                urls.append(m.url)
        results = []
        for url in urls:
            try:
                from trafilatura import fetch_url, extract
                html = fetch_url(url)
                content = extract(html, output_format="markdown", favor_recall=True,
                                  include_tables=True, include_links=False, include_images=False) if html else ""
                if not content:
                    pred = {"suggestions": [], "reasoning": "Could not fetch or extract content"}
                else:
                    pred = predict_schemas(content)
            except Exception as e:
                pred = {"suggestions": [], "reasoning": str(e)}
            suggestions = pred.get("suggestions", [])
            total = sum(s["score"] for s in suggestions) / max(len(suggestions), 1)
            results.append(Div(
                H3(A(url, href=url, target="_blank", cls="link link-primary text-sm"), cls="text-sm font-semibold mb-2 mt-4"),
                Div(cls="space-y-1")(
                    Div(cls="flex flex-wrap gap-x-4 gap-y-1")(
                        *[Div(cls="flex items-center gap-2")(
                            Span(item["type"], cls="text-xs font-medium w-20"),
                            Progress(cls="-primary h-2", value=str(item["score"]), max="100"),
                            Span(f"{item['score']}%", cls="text-xs text-base-content/60 w-8"),
                        ) for item in suggestions],
                    ),
                    P(pred.get("reasoning", ""), cls="text-xs text-base-content/50 mt-1"),
                ),
            ))
        if not results:
            return P("No pages found.", cls="text-sm text-base-content/60")
        return Div(*results)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Error: {e}"),
        )


@rt
def schema_check_url(id: int, url: str):
    try:
        result = validate_page(url)
        return _render_schema_result(result)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Error: {e}"),
        )


def _render_schema_result(result: dict):
    if result.get("error"):
        return Div(cls="alert alert-error")(
            Span(f"Failed to fetch URL (HTTP {result['fetch_status']}): {result['error']}"),
        )
    summary = result.get("summary", {})
    return Div(
        Div(cls="flex items-center gap-3 mb-4")(
            _schema_status_badge(result),
            Span(f"{summary.get('total_schemas', 0)} schema(s) found", cls="text-sm"),
            Span(f"Types: {', '.join(summary.get('types_found', [])) or 'none'}", cls="text-sm text-base-content/60"),
            Span(f"Google supported: {'Yes' if summary.get('has_google_supported') else 'No'}",
                 cls=f"text-sm {'text-success' if summary.get('has_google_supported') else 'text-warning'}"),
        ),
        *[_render_schema_block(s) for s in result.get("schemas_found", [])],
    )


def _render_schema_block(s: dict):
    status_icon = Span("✓", cls="text-success") if s["is_valid"] else Span("✗", cls="text-error")
    items = []
    if s["fields_missing_required"]:
        items.append(Div(cls="text-xs text-error")(
            Span("Missing required: "),
            Span(", ".join(s["fields_missing_required"]), cls="font-mono"),
        ))
    if s["fields_missing_recommended"]:
        items.append(Div(cls="text-xs text-warning")(
            Span("Missing recommended: "),
            Span(", ".join(s["fields_missing_recommended"]), cls="font-mono"),
        ))
    for w in s["warnings"]:
        items.append(Div(w, cls="text-xs text-warning"))
    return Div(cls="collapse collapse-arrow border border-base-300 rounded-lg mb-2")(
        Input(type="checkbox", cls="peer"),
        Div(cls="collapse-title text-sm font-medium flex items-center gap-2")(
            status_icon,
            Span(f"{s['type']} ({s['format']})"),
            Span(cls="badge badge-xs " + ("badge-success" if s["google_supported"] else "badge-ghost"))(
                "Google supported" if s["google_supported"] else "Not supported"
            ),
        ),
        Div(cls="collapse-content")(
            *items,
            Pre(cls="mt-2 p-2 bg-base-200 rounded text-xs overflow-x-auto max-h-48 overflow-y-auto")(
                json.dumps(s["raw"], indent=2, ensure_ascii=False),
            ) if s.get("raw") else "",
        ),
    )




def load_or_setup_mapper(session, website):
    "Try to load mapper, return ('ok', mapping) or ('missing', None) or ('error', domain, message)."
    domain = website.url.removeprefix("https://").removeprefix("http://").rstrip("/")
    mapper_path = Path.home() / ".config" / "seo_rat" / "mappers" / domain / "mapper.py"
    if not mapper_path.exists():
        return ("missing", domain, mapper_path)
    try:
        spec = importlib.util.spec_from_file_location("mapper", mapper_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        mapping = module.get_url_file_mapping()
        sync_url_mapping(session, website.id, mapping)
        for url, file_path in mapping.items():
            existing = session.exec(select(Article).where(
                Article.website_id == website.id, Article.url == url
            )).first()
            if not existing:
                insert_article(session, Article(website_id=website.id, file_path=file_path, url=url))
        return ("ok", domain, mapping)
    except Exception as e:
        return ("error", domain, str(e))


@rt
def report(id: int, refresh: bool = False, export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Not found", P("Website not found"))
        domain = website.url.removeprefix("https://").removeprefix("http://").rstrip("/")
        if refresh:
            from seo_rat.models import URLMapping
            for m in session.exec(select(URLMapping).where(URLMapping.website_id == id)):
                session.delete(m)
            session.commit()
            _report_cache.pop(id, None)
            REPORT_CACHE_DIR.joinpath(f"{id}.json").unlink(missing_ok=True)
        status, domain_or_result, result = load_or_setup_mapper(session, website)
        if status == "missing":
            return _report_mapper_setup(id, domain_or_result, result, website)
        if status == "error":
            return Titled("Mapper Error",
                A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4 block"),
                H1("Mapper Failed", cls="text-2xl font-bold mb-2"),
                P(f"Failed to load mapper for {website.url}:", cls="text-base-content/60 mb-2"),
                Div(cls="mockup-code mb-4")(
                    *[Div(cls="px-4")(Span(line, cls="text-error")) for line in result.split("\n")],
                ),
                P("Check your mapper file or internet connection and try again.",
                  cls="text-base-content/60"))

        cached = _load_cached_report(id)
        if cached is not None:
            if export:
                return _report_export_response(cached, export)
            return _report_page(website, domain, cached)

        if export:
            return Div(P("Report not yet generated. Please load the report first.", cls="text-warning"))

        # Loading shell – HTMX fetches content in background
        return Title(f"SEO Report - {website.name}"), Main(cls="container")(
            Div(cls="flex items-center gap-2 mb-4")(
                A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary"),
                Span(cls="grow"),
                A("Edit Mapper", href=edit_mapper.to(id=id), cls="btn btn-outline btn-sm"),
                A("Refresh from Sitemap", href=report.to(id=id, refresh=True),
                  cls="btn btn-outline btn-sm"),
            ),
            H1("SEO Report", cls="text-2xl font-bold mb-1"),
            P(f"{website.url}", cls="text-base-content/60 mb-4"),
            Div(id="report-content", hx_get=f"/report_content?id={id}", hx_trigger="load")(
                Div(cls="flex items-center gap-3 justify-center py-12")(
                    Span(cls="loading loading-spinner loading-lg"),
                    Span("Generating report...", cls="text-base-content/60"),
                ),
            ),
        ),


@rt
def report_content(id: int, refresh: bool = False, export: str = ""):
    if refresh:
        _report_cache.pop(id, None)
        REPORT_CACHE_DIR.joinpath(f"{id}.json").unlink(missing_ok=True)

    if not refresh:
        cached = _load_cached_report(id)
        if cached is not None:
            if export:
                return _report_export_response(cached, export)
            return _report_content_fragment(cached)

    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        is_quarto = website.site_type == "quarto"
        domain = website.url.removeprefix("https://").removeprefix("http://").rstrip("/")
        status, domain_or_result, result = load_or_setup_mapper(session, website)
        if status != "ok":
            return Div(
                P("Mapper not configured.", cls="text-warning"),
                A("Set up mapper", href=report.to(id=id), cls="link link-primary"),
            )
    _report_progress[id] = {"status": "running", "pct": 0, "msg": "Starting..."}
    Thread(target=_run_report, args=(id, is_quarto), daemon=True).start()
    return Div(
        Progress(value="0", max="100", cls="w-full"),
        Span("Starting...", cls="text-sm ml-2"),
        hx_get=report_progress.to(id=id),
        hx_trigger="every 1s",
        hx_target="this",
        hx_swap="outerHTML",
    )


def _run_report(id: int, is_quarto: bool):
    def cb(pct: int, msg: str):
        _report_progress[id] = {"status": "running", "pct": pct, "msg": msg}
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            _report_progress[id] = {"status": "error", "pct": 0, "msg": "Website not found"}
            return
        domain = website.url.removeprefix("https://").removeprefix("http://").rstrip("/")
        report_data = generate_seo_report(
            session=session,
            website_id=id,
            domain=domain,
            is_quarto=is_quarto,
            title_is_h1=is_quarto,
            progress_callback=cb,
        )
        _save_cached_report(id, report_data)
        _report_progress[id] = {"status": "done", "pct": 100, "msg": "Done"}


@rt
def report_progress(id: int):
    prog = _report_progress.get(id)
    if not prog:
        return P("No report in progress", cls="text-sm")
    if prog["status"] == "running":
        return Div(
            Progress(value=str(prog["pct"]), max="100", cls="w-full"),
            Span(f"{prog['pct']}% — {prog['msg']}", cls="text-sm ml-2"),
            hx_get=report_progress.to(id=id),
            hx_trigger="every 1s",
            hx_target="this",
            hx_swap="outerHTML",
        )
    if prog["status"] == "done":
        return Div(
            hx_get=report_content.to(id=id),
            hx_trigger="load",
            hx_target="this",
            hx_swap="outerHTML",
        )(
            Div(cls="flex items-center gap-3 justify-center py-12")(
                Span(cls="loading loading-spinner loading-lg"),
                Span("Loading report...", cls="text-base-content/60"),
            ),
        )
    if prog["status"] == "error":
        return Alert(Span(prog.get("msg", "Unknown error")), cls="alert-error")


def _report_export_response(report_data: dict, fmt: str) -> Response:
    issues = report_data.get("issues", {})
    h = ["Page", "Issues"]
    d = [[url, ", ".join(data["issues"])] for url, data in sorted(
        issues.items(), key=lambda x: len(x[1]["issues"]), reverse=True)[:50]]
    return _export_response(to_csv(h, d) if fmt == "csv" else to_markdown(h, d), "seo_report", fmt)


def _report_page(website: Website, domain: str, report_data: dict) -> FT:
    return Title(f"SEO Report - {website.name}"), Main(cls="container")(
        Div(cls="flex items-center gap-2 mb-4")(
            A("← Back to Dashboard", href=site.to(id=website.id), cls="link link-primary"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=report.to(id=website.id, export="csv"))),
                    Li(A("Markdown", href=report.to(id=website.id, export="md"))),
                ),
            ),
            Button("Regenerate", cls="btn btn-outline btn-sm",
                   hx_get=f"/report_content?id={website.id}&refresh=1", hx_target="#report-content",
                   hx_swap="innerHTML"),
            A("Edit Mapper", href=edit_mapper.to(id=website.id), cls="btn btn-outline btn-sm"),
            A("Refresh from Sitemap", href=report.to(id=website.id, refresh=True),
              cls="btn btn-outline btn-sm"),
        ),
        H1("SEO Report", cls="text-2xl font-bold mb-1"),
        P(f"{website.url}", cls="text-base-content/60 mb-4"),
        Div(id="report-content")(
            _report_stats(report_data),
            _report_issues_table(report_data),
        ),
    ),


def _report_content_fragment(report_data: dict) -> FT:
    return Div(
        _report_stats(report_data),
        _report_issues_table(report_data),
    )


def _report_mapper_setup(id: int, domain_or_result: str, result, website: Website) -> FT:
    mapper_path = result
    domain = domain_or_result
    local_mode = "direct" if website.site_type in ("quarto", "nbdev") else "slug" if website.site_type == "astro" else "slug"

    local_code = """from seo_rat.content_mapper import map_all_urls_to_files
from seo_rat.index_tracking import fetch_sitemap_urls


def get_url_file_mapping() -> dict[str, str]:
    urls = fetch_sitemap_urls("https://{0}/sitemap.xml")
    return map_all_urls_to_files(
        base_path="/path/to/your/content",
        site_url="https://{0}",
        urls=urls,
        mode="{1}",
    )""".format(domain, local_mode)

    fetch_code = """from seo_rat.index_tracking import fetch_sitemap_urls


def get_url_file_mapping() -> dict[str, str]:
    urls = fetch_sitemap_urls("https://{0}/sitemap.xml")
    return {{url: "::fetch::" for url in urls}}""".format(domain)

    return Title(f"Setup Mapper - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4 block"),
        H1("Mapper Setup", cls="text-2xl font-bold mb-2"),
        P(f"To generate an SEO report for {website.url}, you need a mapper file.",
          cls="text-base-content/60 mb-1"),
        P(Span("The file will be saved to:", cls="font-semibold"), cls="mb-4"),
        Div(cls="mockup-code mb-6 text-sm")(
            *[Div(cls="px-4")(
                Span(f"~/.config/seo_rat/mappers/{domain}/mapper.py", cls="text-primary")
            )],
        ),
        Form(method="POST", action="/save_mapper")(
            Input(type="hidden", name="id", value=str(id)),
            Input(type="hidden", name="domain", value=domain),
            Div(cls="tabs tabs-bordered mb-4")(
                Input(type="radio", name="mode_cls", cls="tab", aria_label="Local Files",
                      value="local", checked=True,
                      onchange="document.getElementById('tc').value=lc"),
                Input(type="radio", name="mode_cls", cls="tab", aria_label="CMS / Remote",
                      value="fetch",
                      onchange="document.getElementById('tc').value=fc"),
            ),
            Textarea(local_code,
                     id="tc", name="code",
                     cls="textarea textarea-bordered font-mono text-xs w-full mb-4",
                     rows=20, style="min-height:400px"),
            Input(type="hidden", id="lc", value=local_code),
            Input(type="hidden", id="fc", value=fetch_code),
            Input(type="hidden", name="mode", id="hf-mode", value="fetch"),
            Button("Save & Generate Report",
                   type="submit", cls="btn btn-primary"),
        ),
        Script("""
const lc = document.getElementById('lc').value;
const fc = document.getElementById('fc').value;
document.querySelectorAll('input[name="mode_cls"]').forEach(r => {
    r.addEventListener('change', function() {
        document.getElementById('tc').value = this.value === 'local' ? lc : fc;
        document.getElementById('hf-mode').value = this.value;
    });
});
"""),
    )


@rt
def edit_mapper(id: int):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Not found", P("Website not found"))
        domain = website.url.removeprefix("https://").removeprefix("http://").rstrip("/")
        local_mode = "direct" if website.site_type in ("quarto", "nbdev") else "slug"
        name = website.name
        url = website.url
        mapper_path = Path.home() / ".config" / "seo_rat" / "mappers" / domain / "mapper.py"
        existing_code = mapper_path.read_text() if mapper_path.exists() else ""
    return Title(f"Edit Mapper - {name}"), Main(cls="container")(
        A("← Back to Report", href=report.to(id=id), cls="link link-primary mb-4 block"),
        H1("Edit Mapper", cls="text-2xl font-bold mb-2"),
        P(f"Edit the URL-to-file mapping for {url}.", cls="text-base-content/60 mb-4"),
        Form(method="POST", action="/save_mapper")(
            Input(type="hidden", name="id", value=str(id)),
            Input(type="hidden", name="domain", value=domain),
            Textarea(existing_code, name="code",
                     cls="textarea textarea-bordered font-mono text-xs w-full mb-4",
                     rows=20, style="min-height:400px"),
            Input(type="hidden", name="mode", value="local"),
            Button("Save & Regenerate Report", type="submit", cls="btn btn-primary"),
        ),
    )


@rt
def save_mapper(id: int, domain: str, code: str, mode: str = "local"):
    mapper_dir = Path.home() / ".config" / "seo_rat" / "mappers" / domain
    mapper_dir.mkdir(parents=True, exist_ok=True)
    mapper_path = mapper_dir / "mapper.py"
    mapper_path.write_text(code)
    _report_cache.pop(id, None)
    REPORT_CACHE_DIR.joinpath(f"{id}.json").unlink(missing_ok=True)
    with get_session() as session:
        from seo_rat.models import URLMapping
        for m in session.exec(select(URLMapping).where(URLMapping.website_id == id)):
            session.delete(m)
        session.commit()
    return RedirectResponse(f"/report?id={id}", status_code=303)



@rt
def articles(id: int, export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Not found", P("Website not found"))
        articles_list = get_articles_by_website(session, id)

        if export:
            h = ["URL", "Focus Keyword", "Target Goal"]
            d = [[a.url or a.file_path, a.focus_keyword or "", a.target_goal or ""] for a in articles_list]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "articles", export)

    return Title(f"Articles - {website.name}"), Main(cls="container")(
        Div(cls="flex items-center gap-2 mb-4")(
            A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=articles.to(id=id, export="csv"))),
                    Li(A("Markdown", href=articles.to(id=id, export="md"))),
                ),
            ),
        ),
        H1("Article SEO Metadata", cls="text-2xl font-bold mb-1"),
        P(f"{website.url} — {len(articles_list)} articles", cls="text-base-content/60 mb-4"),
        Div(cls="overflow-x-auto")(
            Table(cls="table table-zebra")(
                Thead(Tr(Th("File / URL"), Th("Focus Keyword"), Th("Target Goal"), Th("Actions"))),
                Tbody(*[
                    Tr(
                        Td(A(a.url or a.file_path, href=a.url or "#", target="_blank",
                             cls="link link-primary text-sm truncate max-w-[300px] block")),
                        Td(a.focus_keyword or "—", cls="text-sm"),
                        Td(a.target_goal or "—", cls="text-sm max-w-[200px] truncate"),
                        Td(Div(cls="flex gap-1")(
                            Btn("Infer", cls="btn btn-outline btn-xs",
                                hx_post=article_infer.to(article_id=a.id),
                                hx_target=f"#infer-result-{a.id}",
                                hx_swap="innerHTML"),
                        )),
                        id=f"article-row-{a.id}",
                    ) for a in articles_list
                ]),
            ),
        ),
        *[Div(id=f"infer-result-{a.id}", cls="mb-4") for a in articles_list],
    ),


@rt
def article_infer(article_id: int):
    with get_session() as session:
        article = session.get(Article, article_id)
        if not article:
            return P("Article not found", cls="text-error")
        try:
            content = get_article_content(article)
        except Exception as e:
            return P(f"Failed to load content: {e}", cls="text-error")
        if not content:
            return Div(cls="alert alert-error text-sm")(
                Span(f"Could not load content (file_path={article.file_path}, url={article.url})"),
            )
        try:
            result = infer_article_seo(content)
        except Exception as e:
            return Div(cls="alert alert-error text-sm")(Span(f"Inference failed: {e}"))
    kw = ", ".join(result.get("secondary_keywords", []))
    return Div(cls="border border-primary rounded-lg p-4 mb-4 bg-base-200", id=f"infer-form-{article_id}")(
        H3("Inferred SEO Metadata", cls="text-sm font-semibold mb-3"),
        Form(
            Input(type="hidden", name="article_id", value=str(article_id)),
            Div(cls="grid grid-cols-1 gap-3")(
                Fieldset(FieldsetLegend("Focus Keyword"),
                    Input(name="focus_keyword", value=result.get("focus_keyword", ""),
                          cls="input input-bordered input-sm w-full")),
                Fieldset(FieldsetLegend("Secondary Keywords (comma-separated)"),
                    Input(name="secondary_keywords", value=kw,
                          cls="input input-bordered input-sm w-full")),
                Fieldset(FieldsetLegend("Target Goal"),
                    Textarea(result.get("target_goal", ""), name="target_goal",
                             cls="textarea textarea-bordered textarea-sm w-full", rows=2)),
            ),
            Div(cls="mt-3 flex justify-end")(
                Btn("Save", cls="btn btn-primary btn-sm", type="submit"),
            ),
            hx_post=article_save.to(),
            hx_target=f"#article-row-{article_id}",
            hx_swap="outerHTML",
        ),
    )


@rt
def article_save(article_id: int, focus_keyword: str = "", secondary_keywords: str = "", target_goal: str = ""):
    with get_session() as session:
        article = session.get(Article, article_id)
        if not article:
            return P("Article not found", cls="text-error")
        kw_list = [k.strip() for k in secondary_keywords.split(",") if k.strip()]
        article.focus_keyword = focus_keyword or None
        article.secondary_keywords = kw_list or None
        article.target_goal = target_goal or None
        article.last_optimized = datetime.now()
        session.add(article)
        session.commit()
        session.refresh(article)

    return Tr(
        Td(A(article.url or article.file_path, href=article.url or "#", target="_blank",
             cls="link link-primary text-sm truncate max-w-[300px] block")),
        Td(article.focus_keyword or "—", cls="text-sm"),
        Td(article.target_goal or "—", cls="text-sm max-w-[200px] truncate"),
        Td(Div(cls="flex gap-1")(
            Btn("Infer", cls="btn btn-outline btn-xs",
                hx_post=article_infer.to(article_id=article.id),
                hx_target=f"#infer-result-{article.id}",
                hx_swap="innerHTML"),
        )),
        id=f"article-row-{article.id}",
    )


_SETTINGS_KEYS = ["LLM_MODEL", "LLM_API_KEY", "LLM_API_BASE", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SEO_RAT_DB_URL"]


def _read_settings_env() -> dict[str, str]:
    vals = {k: "" for k in _SETTINGS_KEYS}
    if not SETTINGS_ENV_PATH.exists():
        return vals
    for line in SETTINGS_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            if key in vals:
                vals[key] = val.strip().strip("\"'")
    return vals


def _write_settings_env(data: dict[str, str]):
    SETTINGS_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_ENV_PATH.exists():
        lines = SETTINGS_ENV_PATH.read_text().splitlines()
        written = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in data:
                    new_lines.append(f"{key}={data[key]}")
                    written.add(key)
                else:
                    new_lines.append(line)
        for k, v in data.items():
            if k not in written:
                new_lines.append(f"{k}={v}")
        SETTINGS_ENV_PATH.write_text("\n".join(new_lines) + "\n")
    else:
        SETTINGS_ENV_PATH.write_text("\n".join(f"{k}={v}" for k, v in data.items()) + "\n")


@rt
def settings(msg: str = ""):
    vals = _read_settings_env()
    # fall back to current process env for values not yet saved
    for k in _SETTINGS_KEYS:
        if not vals[k]:
            vals[k] = os.getenv(k, "")
    return Titled("Settings",
        P("Environment variables and API credentials.", cls="text-base-content/60 mb-4"),
        Div(id="settings-msg", cls="alert alert-success mb-4")(
            Span(msg),
        ) if msg else "",
        Form(method="POST", action="/settings_save", cls="max-w-xl")(
            Fieldset(
                FieldsetLegend("LLM Configuration (DSPy / LiteLLM)"),
                P("Uses LiteLLM format: ", cls="text-xs text-base-content/60 mb-2"),
                Pre("  openai/gpt-4o\n  groq/llama-3.3-70b-versatile\n  anthropic/claude-sonnet-4-20250514\n  deepseek/deepseek-chat\n  together/meta-llama/Llama-3.3-70B-Instruct-Turbo",
                    cls="text-xs mb-3"),
                Label("Model", cls="text-sm font-medium"),
                Input(name="LLM_MODEL", value=vals.get("LLM_MODEL", "groq/llama-3.3-70b-versatile"),
                      placeholder="groq/llama-3.3-70b-versatile",
                      cls="input input-bordered w-full mb-2"),
                Div(cls="grid grid-cols-1 md:grid-cols-2 gap-2 mb-2")(
                    Div(
                        Label("API Key", cls="text-sm font-medium"),
                        Input(name="LLM_API_KEY", value=vals.get("LLM_API_KEY", ""),
                              type="password", cls="input input-bordered w-full"),
                    ),
                    Div(
                        Label("API Base URL (optional)", cls="text-sm font-medium"),
                        Input(name="LLM_API_BASE", value=vals.get("LLM_API_BASE", ""),
                              placeholder="https://api.groq.com/openai/v1",
                              cls="input input-bordered w-full"),
                    ),
                ),
                Div(cls="flex items-center gap-2")(
                    Btn("Test LLM", cls="btn btn-outline btn-sm",
                        hx_post=settings_test_llm.to(),
                        hx_include="[name='LLM_MODEL'],[name='LLM_API_KEY'],[name='LLM_API_BASE']",
                        hx_target="#llm-test-result",
                        hx_swap="innerHTML"),
                    Span(id="llm-test-result", cls="text-sm"),
                ),
                cls="border border-base-300 rounded-lg p-4 mb-4",
            ),
            Fieldset(
                FieldsetLegend("Google OAuth (for GSC API)"),
                Div(cls="grid grid-cols-1 md:grid-cols-2 gap-3 mb-2")(
                    Div(
                        Label("Client ID", cls="text-sm font-medium"),
                        Input(name="GOOGLE_CLIENT_ID", value=vals.get("GOOGLE_CLIENT_ID", ""),
                              type="text", cls="input input-bordered w-full"),
                    ),
                    Div(
                        Label("Client Secret", cls="text-sm font-medium"),
                        Input(name="GOOGLE_CLIENT_SECRET", value=vals.get("GOOGLE_CLIENT_SECRET", ""),
                              type="password", cls="input input-bordered w-full"),
                    ),
                ),
                Div(cls="flex items-center gap-2")(
                    Btn("Test GSC", cls="btn btn-outline btn-sm",
                        hx_post=settings_test_gsc.to(),
                        hx_include="[name='GOOGLE_CLIENT_ID'],[name='GOOGLE_CLIENT_SECRET']",
                        hx_target="#gsc-test-result",
                        hx_swap="innerHTML"),
                    Span(id="gsc-test-result", cls="text-sm"),
                ),
                cls="border border-base-300 rounded-lg p-4 mb-4",
            ),
            Fieldset(
                FieldsetLegend("Database"),
                Label("SEO_RAT_DB_URL", cls="text-sm font-medium"),
                Input(name="SEO_RAT_DB_URL", value=vals.get("SEO_RAT_DB_URL", ""),
                      type="text", cls="input input-bordered w-full"),
                cls="border border-base-300 rounded-lg p-4 mb-4",
            ),
            Div(cls="flex justify-end")(
                Btn("Save Settings", cls="btn btn-primary", type="submit"),
            ),
        ),
    )


@rt
def settings_save(LLM_MODEL: str = "", LLM_API_KEY: str = "", LLM_API_BASE: str = "",
                  GOOGLE_CLIENT_ID: str = "", GOOGLE_CLIENT_SECRET: str = "",
                  SEO_RAT_DB_URL: str = ""):
    # keep existing env values as fallback when form fields are empty
    LLM_MODEL = LLM_MODEL or os.getenv("LLM_MODEL", "groq/llama-3.3-70b-versatile")
    LLM_API_KEY = LLM_API_KEY or os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY", "")
    LLM_API_BASE = LLM_API_BASE or os.getenv("LLM_API_BASE", "")
    GOOGLE_CLIENT_ID = GOOGLE_CLIENT_ID or os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = GOOGLE_CLIENT_SECRET or os.getenv("GOOGLE_CLIENT_SECRET", "")
    SEO_RAT_DB_URL = SEO_RAT_DB_URL or os.getenv("SEO_RAT_DB_URL", "")
    data = {
        "LLM_MODEL": LLM_MODEL,
        "LLM_API_KEY": LLM_API_KEY,
        "LLM_API_BASE": LLM_API_BASE,
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
        "SEO_RAT_DB_URL": SEO_RAT_DB_URL,
    }
    _write_settings_env(data)
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        _write_gsc_secrets(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    load_dotenv(SETTINGS_ENV_PATH, override=True)
    return RedirectResponse("/settings?msg=Settings+saved+successfully", status_code=303)


def _write_gsc_secrets(client_id: str, client_secret: str):
    """Write GOOGLE_CLIENT_ID/SECRET to ~/.config/seo_rat/client_secrets.json for GSCAuth."""
    import json
    secrets = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }
    path = CONFIG_DIR / "client_secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(secrets, indent=2))


@rt
def settings_test_llm(LLM_MODEL: str = "", LLM_API_KEY: str = "", LLM_API_BASE: str = ""):
    try:
        import dspy
        kwargs = dict(model=LLM_MODEL, api_key=LLM_API_KEY)
        if LLM_API_BASE:
            kwargs["api_base"] = LLM_API_BASE
        lm = dspy.LM(**kwargs)
        dspy.configure(lm=lm)
        response = lm("Reply with exactly: LLM is working!")
        text = response[0] if isinstance(response, list) else str(response)
        return Div(cls="text-success text-sm")(
            Span(f"✓ {text[:200]}"),
        )
    except Exception as e:
        return Div(cls="text-error text-sm")(
            Span(f"✗ {e}"),
        )


@rt
def settings_test_gsc(GOOGLE_CLIENT_ID: str = "", GOOGLE_CLIENT_SECRET: str = ""):
    secrets = CONFIG_DIR / "client_secrets.json"
    if not secrets.exists():
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            return Div(cls="text-warning text-sm")(
                Span("No client_secrets.json found. Enter credentials and Save first."),
            )
        _write_gsc_secrets(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    try:
        auth = GSCAuth(secrets_file=str(secrets))
        creds = auth.get_credentials()
        sites = get_verified_sites(auth)
        if sites:
            return Div(cls="text-success text-sm")(
                Span("✓ Authenticated. Verified sites:"),
                Ul(*[Li(f"{s['site_url']} — {s['permission_level']}") for s in sites],
                   cls="mt-1 text-xs"),
            )
        return Div(cls="text-warning text-sm")(
            Span("✓ Authenticated but no verified sites found."),
        )
    except ValueError as e:
        return Div(cls="text-warning text-sm")(
            Span(f"✗ {e}"),
        )
    except Exception as e:
        return Div(cls="text-error text-sm")(
            Span(f"✗ {e}"),
        )


serve(port=5002)
