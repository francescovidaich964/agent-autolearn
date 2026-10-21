"""Write-time content scan for agent-authored memory, profile and skill text.

The reviewer reads transcripts that contain web pages and tool output, and
whatever it writes into memory or skills is injected into EVERY future
session. That makes the store a persistence channel for prompt injection:
one poisoned page can become a standing instruction. This module scans text
before it is written.

Verdicts per finding: ``block`` rejects the write, ``warn`` lets it through
but is logged. Override for humans only: ``AUTOLEARN_SAFETY=warn`` downgrades
blocks to warnings (never set it for agent processes).

Spec: docs/designs/robust-learning-loop/LLD.md (RL-INT-005).
"""
from __future__ import annotations

import os
import re

# Zero-width, bidi controls and Unicode tag characters (used for hidden
# instructions that render as nothing).
INVISIBLE_RE = re.compile(
    "[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]"
)
OVERRIDE_RE = re.compile(
    r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}?"
    r"\b(all|any|previous|prior|above|earlier|system|developer|safety)\b[^.\n]{0,25}?"
    r"\b(instructions?|prompts?|rules?|guidelines?|messages?)\b",
    re.I,
)
SPECIAL_TOKEN_RE = re.compile(
    r"<\|?\s*(im_start|im_end|endoftext|system)\s*\|?>|</?\s*system\s*>|\[/?INST\]",
    re.I,
)
EXFIL_CMD_RE = re.compile(
    r"\b(curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm|nc|scp)\b[^\n]{0,160}?"
    r"(?P<secret>\$\{?\w*(KEY|TOKEN|SECRET|PASS(WORD)?)\w*\}?|\S*\.ssh/\S*|\S*id_(rsa|ed25519)\S*|"
    r"\S*\.aws/credentials|\S*auth\.json|\S*\.netrc|\S*\.git-credentials)",
    re.I,
)
# `ssh/scp -i ~/.ssh/key` USES a key as an identity; it does not ship it.
IDENTITY_FLAG_RE = re.compile(r"(-i|--identity(-file)?|IdentityFile)[\s=]+$")
EXFIL_PROSE_RE = re.compile(
    r"\b(send|post|upload|exfiltrate|leak|forward|email)\b[^.\n]{0,60}?"
    r"\b(credentials?|api[ _-]?keys?|tokens?|secrets?|passwords?|ssh keys?|cookies?)\b"
    r"[^.\n]{0,60}?\b(to|at)\s+(https?://|[\w.+-]+@[\w-]+\.)",
    re.I,
)
CREDENTIAL_RE = re.compile(
    r"\b(sk-(ant-)?[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|"
    r"github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.)"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
PIPE_TO_SHELL_RE = re.compile(r"\b(curl|wget|iwr|irm)\b[^|\n]{1,200}\|\s*(sudo\s+)?(ba|z|da)?sh\b|\|\s*iex\b", re.I)
PERSONA_RE = re.compile(r"\byou are now\b|\bnew system (prompt|instructions?)\b|\bact as (an? )?(unrestricted|jailbroken)\b", re.I)

RULES = [
    ("invisible-unicode", "block", INVISIBLE_RE),
    ("instruction-override", "block", OVERRIDE_RE),
    ("special-token", "block", SPECIAL_TOKEN_RE),
    ("exfiltration-command", "block", EXFIL_CMD_RE),
    ("exfiltration-prose", "block", EXFIL_PROSE_RE),
    ("credential", "block", CREDENTIAL_RE),
    ("pipe-to-shell", "warn", PIPE_TO_SHELL_RE),
    ("persona-override", "warn", PERSONA_RE),
]


def scan(text: str) -> list[dict]:
    """Return findings ``[{rule, verdict, excerpt}]`` for ``text``."""
    findings: list[dict] = []
    if not text:
        return findings
    downgrade = os.environ.get("AUTOLEARN_SAFETY", "").lower() == "warn"
    for rule, verdict, rx in RULES:
        m = rx.search(text)
        if m and rule == "exfiltration-command":
            m = next((x for x in rx.finditer(text)
                      if not IDENTITY_FLAG_RE.search(text[:x.start("secret")])), None)
        if not m:
            continue
        start = max(0, m.start() - 20)
        excerpt = text[start:m.end() + 20]
        if rule == "invisible-unicode":
            excerpt = excerpt.encode("unicode_escape").decode("ascii")
        if rule == "credential":
            excerpt = m.group(0)[:6] + "…[redacted]"
        findings.append({
            "rule": rule,
            "verdict": "warn" if (downgrade and verdict == "block") else verdict,
            "excerpt": excerpt.replace("\n", " ")[:160],
        })
    return findings


def blocking(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["verdict"] == "block"]
