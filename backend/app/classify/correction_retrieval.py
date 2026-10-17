from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import ClassifyCorrection, EmailMessage


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+-]{2,}", re.I)
_STOP = {
    "and", "are", "but", "for", "from", "have", "not", "our", "that", "the",
    "this", "to", "was", "we", "with", "you", "your", "www", "http", "https",
}


@dataclass(frozen=True)
class CorrectionExample:
    corrected_label: str
    subject: str
    sender: str
    excerpt: str
    direction: str
    tokens: frozenset[str]


def _tokens(*values: str) -> frozenset[str]:
    return frozenset(
        token
        for value in values
        for token in _TOKEN_RE.findall((value or "").casefold())
        if token not in _STOP
    )


def load_correction_corpus(db: Session) -> list[CorrectionExample]:
    """Load the latest non-evaluation correction for each email."""
    rows = (
        db.query(ClassifyCorrection, EmailMessage.folder)
        .join(EmailMessage, EmailMessage.id == ClassifyCorrection.email_id)
        .filter(ClassifyCorrection.evaluation_holdout.is_not(True))
        .order_by(ClassifyCorrection.id.desc())
        .all()
    )
    seen: set[int] = set()
    corpus: list[CorrectionExample] = []
    for row, folder in rows:
        if row.email_id in seen:
            continue
        seen.add(row.email_id)
        excerpt = " ".join((row.snippet or row.body_text or "").split())[:700]
        direction = "outgoing" if folder == "sent" else "incoming"
        corpus.append(CorrectionExample(
            corrected_label=row.corrected_label,
            subject=" ".join((row.subject or "").split())[:180],
            sender=" ".join((row.sender or "").split())[:180],
            excerpt=excerpt,
            direction=direction,
            tokens=_tokens(row.subject or "", row.sender or "", excerpt),
        ))
    return corpus


def retrieve_relevant_corrections(
    corpus: list[CorrectionExample],
    *,
    subject: str,
    sender: str,
    body_text: str,
    snippet: str,
    likely_label: str | None = None,
    limit: int = 6,
) -> list[CorrectionExample]:
    query = _tokens(subject, sender, snippet, body_text[:1800])
    if not query:
        return []
    sender_domain = sender.rsplit("@", 1)[-1].casefold().strip(" >") if "@" in sender else ""
    scored: list[tuple[float, int, CorrectionExample]] = []
    for index, example in enumerate(corpus):
        overlap = len(query & example.tokens)
        if not overlap:
            continue
        union = len(query | example.tokens) or 1
        score = overlap / union + min(overlap, 8) * 0.035
        if likely_label and example.corrected_label == likely_label:
            score += 0.08
        if sender_domain and sender_domain in example.sender.casefold():
            score += 0.16
        scored.append((score, -index, example))
    scored.sort(reverse=True, key=lambda item: (item[0], item[1]))

    # Keep examples useful for decision boundaries, not six copies of one label.
    selected: list[CorrectionExample] = []
    per_label: dict[str, int] = {}
    for score, _index, example in scored:
        if score < 0.08 or per_label.get(example.corrected_label, 0) >= 2:
            continue
        selected.append(example)
        per_label[example.corrected_label] = per_label.get(example.corrected_label, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def format_retrieved_corrections(examples: list[CorrectionExample]) -> str:
    if not examples:
        return "No closely related human corrections were found."
    return "\n".join(
        f"- direction={item.direction}; subject={item.subject!r}; excerpt={item.excerpt!r}; "
        f"correct_label={item.corrected_label}"
        for item in examples
    )
