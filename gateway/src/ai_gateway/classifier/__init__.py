"""The injection classifier: a small model judges free text for instructions aimed at an AI.

`prompt` holds the fixed prompt and what a unit of text is, `judge` makes the calls (through
agent-core's ModelClient, replay by default) and keeps the books, and `corpus` lists every text
the committed recordings must cover. The layer that uses it is `pipeline/layers/classifier.py`.
"""
