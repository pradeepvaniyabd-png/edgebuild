"""Lightweight language detection for Hindi, Hinglish (romanised Hindi) and English."""

from __future__ import annotations

import re

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_WORD = re.compile(r"[a-zA-Z']+")

# Common romanised-Hindi function words. Chosen to be rare in English prose.
HINGLISH_MARKERS = frozenset(
    """hai hain ho hoga hogi hun hoon tha thi the karo karna kar karke kiya kiye karta karti karte karega karenge
    bana banao banana banado bata batao bataiye de do dena dijiye le lo lena chahiye chahta chahti
    ka ki ke ko se mein me par pe aur ya bhi nahi nahin mat kya kyu kyun kyon kaise kab kahan kaun kitna kitne
    yeh ye woh wo is us in un mera meri mere tera hamara apna apni apne tum aap main hum
    ek do teen sab kuch bahut thoda zyada accha achha theek sahi abhi ab phir fir wala wali wale
    uska uski unka iska iski liye saath baad pehle tak jab tab agar lekin magar kyunki isliye
    dikhao dikha nikalo nikal likho likh padho bhejo bhej kholo chalao lagao hatao jodo""".split()
)


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text))


def detect_language(text: str) -> str:
    """Return ``hi`` (Devanagari), ``hinglish`` (romanised Hindi) or ``en``."""
    if not text.strip():
        return "en"
    devanagari = len(_DEVANAGARI.findall(text))
    letters = sum(1 for ch in text if ch.isalpha())
    if letters and devanagari / letters > 0.3:
        return "hi"
    words = [w.lower() for w in _WORD.findall(text)]
    if not words:
        return "hi" if devanagari else "en"
    hits = sum(1 for word in words if word in HINGLISH_MARKERS)
    if hits >= 2 and hits / len(words) >= 0.15:
        return "hinglish"
    if hits >= 1 and len(words) <= 4:
        return "hinglish"
    return "hi" if devanagari else "en"


def speech_language(text: str, hint: str | None = None) -> str:
    """Language code for a TTS engine: Devanagari text is always spoken with a Hindi voice."""
    if has_devanagari(text):
        return "hi"
    if hint and hint.lower().startswith("hi") and detect_language(text) == "hi":
        return "hi"
    return "en"
