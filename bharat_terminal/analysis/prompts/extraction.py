"""
Prompt templates for Stage 2 entity/sector extraction.
"""

SYSTEM_PROMPT = """You are an expert Indian equity market analyst with deep knowledge of:
- BSE/NSE listed companies and their sector classifications
- RBI, SEBI, and other Indian financial regulators
- Indian macroeconomic indicators and their market impact
- Corporate actions, earnings, and M&A in Indian markets

Your task is to extract structured market intelligence from financial news items."""

EXTRACTION_USER_TEMPLATE = """Analyze this Indian financial news and extract structured market intelligence.

Headline: {headline}
Body: {body}
Source: {source}

Extract all relevant market entities, sectors, and the macro theme."""
