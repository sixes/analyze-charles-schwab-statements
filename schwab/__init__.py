"""Charles Schwab statement and trade-confirmation analysis.

Trades arrive from eConfirm emails (`schwab.confirms`) and are the primary record;
monthly statement PDFs (`schwab.statements`) are parsed to audit that feed and to
supply everything a confirm cannot know - account value, cash, margin and income.
"""

__all__ = ["domain", "text"]
