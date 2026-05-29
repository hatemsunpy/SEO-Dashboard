# SEO RAT — Design & Roadmap

## Philosophy

Context-aware SEO optimization with human in the loop. Built for developers using Astro, Quarto, FastHTML, and nbdev —
not WordPress.

---

## 🔄 In Progress

## Integerations with other systems

- Wordpress
- Salaa
- Wuilt
- shopfiy
- odoo system

### Keyword Intelligence

- [ ] smart missing queries filter
  - use dspy signature
  - cache the answers if they are not changed

- [ ] Fuzzy keyword matching (`LIKE`) for `keyword_ranking`
- [ ] DSPy filter for cannibalization noise reduction

### On-Page Technical SEO

- [ ] Canonical tag detection
- [ ] Meta robots / noindex detection
- [ ] Hreflang tags

### PageSpeed support

- support PageSpeed for performance and errors suggestion into the report
- store them in a database for reports

### Content Enhancements

- [ ] Alt text suggestions
- [ ] Broken link detection (async httpx 404 checks)
- [ ] Manual focus keyword input + automatic secondary keyword assignment (needs UI)

---

## 📋 Backlog

### Medium Priority

- [ ] Internal linking suggestions (embedding-based)
- [ ] Social meta (OpenGraph, Twitter Cards)
- [ ] llms.txt generator

### Lower Priority

- [ ] Full content near-duplicate detection (MinHash already in place)
- [ ] NLP FAQ extraction (DSPy)
- [ ] Image uniqueness / compression / lazy loading checks
- [ ] PageSpeed tracking per page
- [ ] Email SEO reports (weekly/monthly)
- [ ] robots.txt / noindex controls

### Schema

- [ ] Competitor schema analysis (browser extension — needs $5 store fee)

### Web App (FastHTML)

- [ ] Backlink checker
- [ ] Site explorer

---

## 🔮 Future / Ideas

- LLM-powered per-page improvement suggestions
- BERT query intent classifier
- Agentic orchestrator (plan + execute)
- GSC seasonal/YoY trend analysis
- Bing integration (IndexNow + Bing AI trends)
- MCP / Agent Skill integration
- How do Ahrefs/SEMrush get domain visitor data?
- How to build PC from scratch

---

## 📝 Known Issues

- Some keywords don't match page topic (e.g. `sbak-baldwadmy`)
- Two pages share keyword: شركة عزل بولي يوريا بجدة → cannibalization case

---

## 🏷️ Outreach

- [ ] Share with Sherno
- [ ] Ask Jeremy (fast.ai) for feedback
- [ ] Blog post about SEO RAT
- [ ] Test with fast.ai Quarto blog

## ✅ Shipped (Archive)

### Infrastructure

- [x] Auth: one-time OAuth flow, token cached
- [x] Daily GSC sync with smart gap detection
- [x] Syncthing database backup
- [x] Article path change reflection in DB
- [x] `index.ipynb` documentation

### Content Analysis

- [x] Primary keyword placement (title, H1, first paragraph, URL)
- [x] Heading hierarchy (H2/H3 structure)
- [x] Content freshness detection
- [x] Missing queries per page
- [x] Content gap analysis
- [x] Internal link count (flag < 3)
- [x] Orphan pages detection

### GSC Signals

- [x] Rising vs declining query detection
- [x] Query intent classification (rule-based)
- [x] Green keyword detection
- [x] Rank tracking by country/keyword
- [x] Date range comparison
- [x] Country breakdown

### Schema & Structured Data

- [x] Schema validation (26 Google-supported types)
- [x] FAQ extraction from GSC queries

### Index Tracking

- [x] Not-indexed pages with coverage reasons
- [x] Index status history over time

### CLI (16 commands)

- [x] `seo-rat-sync`
- [x] `seo-rat-report`
- [x] `seo-rat-audit`
- [x] `seo-rat-rank`
- [x] `seo-rat-trend`
- [x] `seo-rat-top-pages`
- [x] `seo-rat-wins`
- [x] `seo-rat-canob`
- [x] `seo-rat-index-check`
- [x] `seo-rat-index-report`
- [x] `seo-rat-index-refresh`
- [x] `seo-rat-crawl-errors`
- [x] `seo-rat-compare`
- [x] `seo-rat-country-breakdown`
- [x] `seo-rat-schema-check`
- [x] `seo-rat-faq`

### Content Mapping

- [x] Markdown files (direct, slug, limax modes)
- [x] HTML fallback via `::fetch::` sentinel
- [x] Tested: kareemai.com, awazly.com, shelid.com, emdadelgaz.com, alainclean.com, smaagarden.com

### Web App (FastHTML)

- [x] Add/manage websites UI
- [x] Data sync UI
- [x] Article CRUD
- [x] SERPWatcher
- [x] SEO Report (with caching + parallel fetch)

---
