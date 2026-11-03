"""Crowd Price on the web: the buyer's drop page and the maker's view.

A thin layer over the engine (src/crowd). It never sets or computes a price or a total: every number it shows is one the
engine produced. It can place and release a buyer's own hold (join, leave), which the engine guards as it does for any
caller. It cannot settle, exclude or bind: those move money and go through Togi, where a person approves them.
"""
