"""SEO Rat Dashboard - FastHTML UI."""

from fasthtml.common import *
from seo_rat.sqlite_db import get_session
from seo_rat.models import Website, GSCAnalytics
from seo_rat.gsc_storage import get_top_pages
from seo_rat.gsc_client import get_date_range
from sqlmodel import select, func

app, rt = fast_app(pico=True)  # Use Pico CSS (built-in)


def render_site_card(w):
    domain = w.url.replace("https://", "").replace("http://", "").rstrip("/")
    return Article(
        H3(w.name),
        P(domain, cls="text-muted"),
        A("View Dashboard", href=f"/site/{w.id}", cls="button"),
    )


@rt("/")
def get():
    with get_session() as session():
        websites = session.exec(select(Website)).all()
        return Title("SEO Rat Dashboard"), Container(
            H1("SEO Rat Dashboard"),
            P(f"{len(websites)} websites tracked"),
            Grid(*[render_site_card(w) for w in websites])
            if websites
            else P("No websites added. Run: seo_rat-add-website", cls="text-muted"),
        )


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


def render_metrics(metrics):
    return Grid(
        Article(B(H2(f"{metrics['clicks']:,}"), cls="text-h3"), P("Clicks (30d)")),
        Article(
            B(H2(f"{metrics['impressions']:,}"), cls="text-h3"), P("Impressions (30d)")
        ),
        Article(
            B(H2(f"{metrics['avg_position']:.1f}"), cls="text-h3"), P("Avg Position")
        ),
        Article(B(H2(f"{metrics['avg_ctr']:.1f}%"), cls="text-h3"), P("Avg CTR")),
    )


def render_top_pages_table(rows):
    if not rows:
        return P("No top pages data", cls="text-muted")
    return Table(
        Thead(Tr(Th("Page"), Th("Clicks"), Th("Impr"), Th("Pos"), Th("CTR"))),
        Tbody(
            *[
                Tr(
                    Td(r["page"][:60] + "..." if len(r["page"]) > 60 else r["page"]),
                    Td(f"{r['total_clicks']:,}"),
                    Td(f"{r['total_impressions']:,}"),
                    Td(f"{r['avg_position']:.1f}"),
                    Td(f"{r['avg_ctr'] * 100:.1f}%"),
                )
                for r in rows[:10]
            ]
        ),
    )


@rt("/site/{site_id}")
def site_dashboard(site_id: int):
    with get_session() as session:
        website = session.get(Website, site_id)
        if not website:
            return Title("Website not found"), P("Website not found")

        domain = website.url.replace("https://", "").replace("http://", "").rstrip("/")
        site_url = f"sc-domain:{domain}"

        metrics = get_site_metrics(session, site_url)
        start, end = get_date_range("last_days", days=30)
        top_pages = get_top_pages(session, site_url, start, end, limit=10)

        return Title(website.name), Container(
            H1(website.name),
            P(website.url, cls="text-muted"),
            render_metrics(metrics),
            Details(Summary("Top Pages"), render_top_pages_table(top_pages)),
        )


serve(port=5002)
