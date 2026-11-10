"""Deterministic screening of user input, before any model call.

Two kinds of input never reach the model:

- restricted requests: attempts to pull internal or other people's data out of the
  system (system prompt, API keys, other customers' data, database dumps, the provider
  directory's contact list, source code). These are refused by code, the attempt is
  stored in `security_flags`, and after `block_after_flags` attempts the user is blocked.
- gibberish: input with no recognisable words (keyboard mashing, only symbols).

Both checks are deliberately conservative: anything ambiguous goes to the model, which
is the second line of defence (and has nothing secret it could leak through a tool).
Screening also saves cost, because none of these messages produce a model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from errand_agent.mocks.catalog import AREAS, SERVICE_ALIASES

# --------------------------------------------------------------------- restricted requests

_ASK = (r"(show|tell|give|send|share|print|reveal|repeat|output|display|list|dump|leak|"
        r"expose|export|paste|copy|read out|what(?:'s| is| are)|translate|quote|recite|"
        r"encode|summari[sz]e|rewrite|spell out|type out|write out|respond with|reply with|"
        r"dikhao|dikha do|batao|bata do|bhejo|bhej do|de do)")
_NOT_MINE = r"(?!\s+(i|we)\s+(gave|said|sent|mentioned|wrote))"  # "...the instructions I gave"

_RESTRICTED: dict[str, list[str]] = {
    "prompt_extraction": [
        r"\b(system|developer|hidden)\s+(prompt|message|instructions?)\b",
        r"\b(initial|original|internal|first|starting|secret)\s+(prompt|instructions)\b",
        rf"\b{_ASK}\b.{{0,40}}\b(your|apne|apna|apni)\s+(instructions|prompt|configuration|"
        r"config|context( window)?|programming|directives)\b",
        r"\b(repeat|output|print|show|quote|recite|paste)\b.{0,40}\b(verbatim|word for word|"
        r"exactly as (written|given))\b",
        r"\b(above|before)\s+(this|the)\s+(line|conversation|chat|first message)\b",
        r"\b(were|have)\s+you\s+(been\s+)?(given|told|instructed|programmed)\b.{0,40}\b(start|"
        r"beginning|initially|before (this|the|i)|to (do|say|behave)|by (your|the) "
        r"(developers?|creators?|company|team))\b",
        r"\b(json|text|data|content|context|messages?|instructions)\s+(that\s+)?(you were|"
        r"you've been|you have been|you got|you received)\s+(given|sent|provided)?",
        r"\b(tool|function)\s+(schemas?|definitions?|specs?|list)\b",
        # Hinglish puts the verb last: "apne instructions batao"
        r"\b(apne|apna|apni|your)\s+(\w+\s+)?(instructions|prompt|configuration|config)\b"
        r".{0,25}\b(dikhao|dikha do|batao|bata do|bhejo|bhej do|de do|translate)",
    ],
    "jailbreak": [
        r"\bignore\s+(all\s+|any\s+)?((your|the|previous|prior|above|earlier|preceding)\s+)+"
        rf"(instructions|rules|prompts?|guidelines|directives|system){_NOT_MINE}\b",
        r"\b(disregard|override|bypass)\s+(all\s+)?(your|the|previous|prior|above|any)\s+"
        rf"(instructions|rules|prompts?|guidelines|restrictions|safety){_NOT_MINE}\b",
        r"\byou are now\s+(an?\s+|in\s+)?\w*(bot|gpt|ai|dan|mode)\b",
        r"\b(developer|debug|god|admin|maintenance|jailbreak|unrestricted|dan)\s+mode\b",
        r"\b(act|behave|respond|roleplay|role-play)\s+(as|like)\s+(an?\s+|the\s+)?(unrestricted|"
        r"unfiltered|jailbroken|evil|developer|admin|root|system|debug\w*)\b",
        r"\bpretend\s+(you'?re|you are|to be)\s+(an?\s+|the\s+)?(unrestricted|unfiltered|"
        r"jailbroken|developer|admin|root|system|debug\w*)\b",
        r"\bfrom now on,?\s+you\s+(will|must|are|should|shall)\s+(ignore|act|respond|answer|"
        r"pretend|reveal|obey|print|start)\b",
        r"\bjailbr(eak|oken)\b",
    ],
    "secrets": [
        r"\b(api|secret|private|access|auth|bearer)[\s_-]?(keys?|tokens?)\b",
        r"(^|\s)\.env\b|\benv(ironment)?\s+(file|var(iable)?s?)\b",
        r"\b(admin|database|db|server|root|your)\s+(password|credentials?|login)\b",
        r"\bgemini[\s_-]?(api|key)\b",
        r"\b(your|the|its|with)\s+(\w+\s+)?(credentials|connection strings?|config(uration)? "
        r"files?|admin (panel|dashboard)|tracebacks?|stack ?traces?)\b",
        r"\byour\s+(secret|admin|master|internal)\s+(password|key|code|command|login|panel)s?\b",
        r"\b(list|show|print|cat|read)\b.{0,30}\b(files|directory|folders?)\b.{0,30}\b(your|"
        r"server|working|system)\b",
    ],
    "other_users_data": [
        r"\b(other|another|all|every|previous|past)\s+(\w+\s+)?(users?|customers?|clients?|"
        r"people)(?:'s|s'|s)?\b.{0,40}\b(data|details|bookings?|booked|address(es)?|phone|numbers?|"
        r"names?|emails?|info(rmation)?|records?|history|memor(y|ies)|messages?|chats?|"
        r"conversations?)\b",
        r"\b(data|details|bookings?|address(es)?|phone numbers?|info(rmation)?|records?|"
        r"emails?|messages?|chats?|conversations?)\b.{0,30}\b(of|for|from|belonging to)\s+"
        r"(the\s+)?(other|another|all|every|previous|last)\s+(users?|customers?|people|clients?)\b",
        r"\b(previous|last|other|another|next)\s+(user|customer|person)(?:'s)?\b.{0,30}\b(sent|"
        r"said|asked|booked|wrote|message|chat|conversation|data|details|address|phone|number)\b",
        r"\b(user|customer|account)[\s_-]?(id\s*)?#?(?!1\b)\d+\b",
        r"(?<!my )\b(user|customer)\s+(profiles|records|entries|database|table)\b",
        r"\b(sample|example|list)\b.{0,30}\b(customer|user)s?(?:'s|s')?\s+(addresses|phone|"
        r"numbers|data|details|profiles?|entr(y|ies)|records?)\b",
        r"\b(saare|sabhi|baaki|dusre|doosre)\s+(customers?|users?|logon|logo|log|grahak)\b.{0,40}"
        r"\b(phone|number|address|details|data|bookings?|naam|jaankari)\b",
        r"\b(dusre|doosre|kisi aur)\s+(ke|ki|ka)\b.{0,30}\b(phone|number|address|booking|"
        r"details)\b",
    ],
    "bulk_data": [
        r"\b(dump|export|download|extract|scrape|leak)\b.{0,40}\b(database|db|sql|schema|"
        r"all (the )?(data|records|users|customers|providers))\b",
        r"\b(select\s+\*\s+from|drop\s+table|union\s+select|insert\s+into)\b",
        r"\bschema\b|\b(database|db)\s+(structure|tables|connection)\b",
        r"\b(list|show|give|send)\b.{0,15}\ball\b.{0,40}\b(registered|in your (system|"
        r"database|records))\b",
        r"\b(all|every|full|complete|entire|saare|sabhi)\b.{0,25}\b(providers?|technicians?|"
        r"vendors?|partners?|plumbers?|electricians?|cleaners?|carpenters?)(?:'s|s')?\b.{0,30}"
        r"\b(phone|numbers?|contacts?|emails?|addresses|commissions?|payouts?)\b",
        r"\b(every|all)\s+(booking|bookings|order|orders|user|users|customer|customers)\s+in\s+"
        r"the\s+(system|database|app)\b",
        r"\b(database|db)\b.{0,20}\b(bhejo|de do|dikhao|nikalo|dump)\b",
    ],
    "internal_business": [
        r"\b(your|the company'?s?|orange'?s?|internal)\s+(commission|revenue|margins?|"
        r"payouts?|profits?|pricing (model|formula|algorithm))\b",
        rf"\b{_ASK}\b.{{0,30}}\b(source code|codebase|your code|your repo(sitory)?)\b",
    ],
}
_RESTRICTED_RE = {k: [re.compile(p, re.I) for p in ps] for k, ps in _RESTRICTED.items()}


def detect_restricted(text: str) -> list[str]:
    """Categories of restricted data the message asks for (empty list = none)."""
    return [cat for cat, pats in _RESTRICTED_RE.items() if any(p.search(text) for p in pats)]


# --------------------------------------------------------------------- gibberish

_VOWELS = set("aeiouy")
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
# Words that often appear without vowels or look odd but are real input here.
_KNOWN = {w for a in AREAS for w in a.lower().split()} | {
    w for k in SERVICE_ALIASES for w in k.split()} | {
    "hsr", "btm", "ac", "pls", "plz", "thx", "tmrw", "tmr", "hmm", "ok", "okk", "yes", "yep",
    "nah", "nope", "brb", "asap", "rs", "inr", "am", "pm", "hrs", "bk", "nal", "hai", "kya"}


def _wordlike(tok: str) -> bool:
    t = tok.lower()
    if t in _KNOWN:
        return True
    if re.search(r"(.)\1{3,}", t):                       # "aaaaa", "zzzz"
        return False
    if len(t) >= 4 and any(t in row or t[::-1] in row for row in _ROWS):  # "asdf", "lkjh"
        return False
    vowels = sum(c in _VOWELS for c in t)
    if vowels == 0:
        return False
    if re.search(r"[^aeiouy]{5,}", t):                  # 5+ consonants in a row
        return False
    return vowels / len(t) >= 0.15


def is_gibberish(text: str) -> bool:
    """True only when the message clearly carries no meaning.

    - nothing but symbols/emoji/punctuation (and no digits), or
    - every word of 3+ letters is non-word-like, with either two such words or one long one.
    """
    s = text.strip()
    if not s:
        return True
    if not re.search(r"[A-Za-z0-9ऀ-ॿ]", s):     # Devanagari counts as language
        return True
    tokens = [t for t in re.findall(r"[A-Za-z]+", s) if len(t) >= 3]
    if not tokens or re.search(r"[ऀ-ॿ]", s):
        return False
    bad = [t for t in tokens if not _wordlike(t)]
    if len(bad) != len(tokens):
        return False
    return len(bad) >= 2 or max(len(t) for t in bad) >= 8


# --------------------------------------------------------------------- language

_HINDI = {"mera", "mere", "meri", "mujhe", "aap", "aapka", "aapke", "karo", "karna", "karke",
          "hai", "hain", "ke", "ki", "ka", "mein", "chahiye", "nahi", "nahin", "kya", "batao",
          "dikhao", "bhej", "bhejo", "kal", "aaj", "parso", "subah", "shaam", "dopahar",
          "saare", "sabhi", "dusre", "doosre", "apne", "apna", "apni", "wala", "wali", "raha",
          "rahi", "gaya", "kuch", "bahut", "ghar", "nal", "bijli", "safai", "theek", "haan",
          "yaad", "rakhna", "rehta", "hoon", "se", "kam", "ko", "aur", "sabse", "sasta", "kab",
          "shanivar", "ravivar", "kaise", "kitna", "koi", "sakta", "abhi", "bhai", "ji"}


def looks_hinglish(text: str) -> bool:
    """Hindi or Hinglish message (Devanagari, or 2+ common Hindi words in Latin script)."""
    if re.search(r"[\u0900-\u097F]", text):
        return True
    words = set(re.findall(r"[a-z]+", text.lower()))
    return len(words & _HINDI) >= 2


# --------------------------------------------------------------------- combined


@dataclass
class Screen:
    kind: str | None = None                  # None | "restricted" | "gibberish"
    categories: list[str] = field(default_factory=list)


def screen(text: str) -> Screen:
    cats = detect_restricted(text)
    if cats:
        return Screen("restricted", cats)
    if is_gibberish(text):
        return Screen("gibberish")
    return Screen()
