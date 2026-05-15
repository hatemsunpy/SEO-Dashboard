# SEO RAT — Design & Roadmap

## Philosophy

Context-aware SEO optimization with human in the loop. Built for developers using Astro, Quarto, FastHTML, and nbdev —
not WordPress.

---

## ✅ Shipped

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

### CLI

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

---

## 🔄 In Progress

### Keyword Intelligence

- [ ] Fuzzy keyword matching (`LIKE`) for `keyword_ranking`
- [ ] DSPy filter for cannibalization noise reduction

### On-Page Technical SEO

- [ ] Canonical tag detection
- [ ] Meta robots / noindex detection
- [ ] Hreflang tags

### Schema

- [ ] Page-needed schema prediction (DSPy agent)
- [x] Competitor schema analysis (browser extension — needs $5 store fee)
    - I just need to publish it

---

## 📋 Backlog

### High Priority

1. [ ] Alt text suggestions
2. [ ] Broken link detection (async httpx 404 checks)
   3. low priority 
4. [ ] Manual focus keyword input + automatic secondary keyword assignment (needs UI)

### Medium Priority

5. [ ] Internal linking suggestions (embedding-based)
6. [ ] Social meta (OpenGraph, Twitter Cards)
7. [ ] Canonical tag management
8. [ ] llms.txt generator

### Lower Priority

9. [ ] Full content near-duplicate detection (MinHash already in place)
10. [ ] NLP FAQ extraction (DSPy)
11. [ ] Image uniqueness / compression / lazy loading checks
12. [ ] PageSpeed tracking per page
13. [ ] Email SEO reports (weekly/monthly)
14. [ ] robots.txt / noindex controls

---

## 🌐 Web App (FastHTML)

- [ ] Add/manage websites UI
- [ ] Data sync UI
- [ ] Article CRUD
- [ ] SERPWatcher
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
  s