"""Small phrase table for deterministic output in English, Hindi and Hinglish.

Used wherever the system writes text without a language model (findings, report
headings, status lines), so the user reads results in the language they wrote in.
"""

from __future__ import annotations

_PHRASES: dict[str, dict[str, str]] = {
    "rows_cols": {
        "en": "'{table}' has {rows} rows and {cols} columns.",
        "hi": "'{table}' में {rows} पंक्तियाँ और {cols} कॉलम हैं।",
        "hinglish": "'{table}' mein {rows} rows aur {cols} columns hain.",
    },
    "duplicates": {
        "en": "{count} duplicate rows found (matched on {keys}).",
        "hi": "{count} डुप्लिकेट पंक्तियाँ मिलीं ({keys} के आधार पर)।",
        "hinglish": "{count} duplicate rows mili ({keys} ke aadhar par).",
    },
    "no_duplicates": {
        "en": "No duplicate rows found.",
        "hi": "कोई डुप्लिकेट पंक्ति नहीं मिली।",
        "hinglish": "Koi duplicate row nahi mili.",
    },
    "numeric": {
        "en": "{name}: total {total}, average {mean}, range {low} to {high}.",
        "hi": "{name}: कुल {total}, औसत {mean}, न्यूनतम {low} से अधिकतम {high}।",
        "hinglish": "{name}: total {total}, average {mean}, range {low} se {high}.",
    },
    "categories": {
        "en": "{name} breakdown: {parts}.",
        "hi": "{name} का वितरण: {parts}।",
        "hinglish": "{name} ka breakdown: {parts}.",
    },
    "blanks": {
        "en": "{name} is blank in {count} rows ({pct}%).",
        "hi": "{name} {count} पंक्तियों में रिक्त है ({pct}%)।",
        "hinglish": "{name} {count} rows mein blank hai ({pct}%).",
    },
    "report_title": {
        "en": "Data report: {table}",
        "hi": "डेटा प्रतिवेदन: {table}",
        "hinglish": "Data report: {table}",
    },
    "h_findings": {"en": "Key findings", "hi": "मुख्य निष्कर्ष", "hinglish": "Key findings"},
    "h_analysis": {"en": "Analysis", "hi": "विश्लेषण", "hinglish": "Analysis"},
    "h_columns": {"en": "Column summary", "hi": "कॉलम सारांश", "hinglish": "Column summary"},
    "h_duplicates": {"en": "Duplicate records", "hi": "डुप्लिकेट अभिलेख", "hinglish": "Duplicate records"},
}


def text_for(language: str, key: str, **values: object) -> str:
    variants = _PHRASES[key]
    template = variants.get(language) or variants["en"]
    return template.format(**values)
