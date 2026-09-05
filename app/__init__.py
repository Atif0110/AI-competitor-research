"""AI Competitor Research Tool — agent-driven competitive intelligence pipeline.

Layers:
  1. Scraping    -> resilient page fetch (Firecrawl / Playwright / demo)
  2. Extraction  -> LLM structured output validated against Pydantic schemas
  3. Storage     -> relational (offers) + vector (reviews)
  4. Analysis    -> price trends, undercuts, sentiment, positioning briefs
  5. Output      -> PDF report, Streamlit dashboard, Slack/e-mail alerts
"""

__version__ = "1.0.0"
