# openoutreach/core/content_style.py
import re
import unicodedata
from django.core.exceptions import ValidationError

# Prohibited canned phrases and unsupported relational claims (case-insensitive)
PROHIBITED_PHRASES = [
    "i hope this message finds you well",
    "i hope this email finds you well",
    "hope this message finds you well",
    "hope this email finds you well",
    "i hope you are doing well",
    "i hope you're doing well",
    "hope you are doing well",
    "hope all is well",
    "hope this finds you well",
    "i am thrilled to apply",
    "i am deeply passionate about",
    "i would be a perfect fit",
    "leveraging my extensive expertise",
    "leverage my expertise",
    "leverage our expertise",
    "leveraging our expertise",
    "my expertise includes",
    "dynamic and results-driven professional",
    "in today's fast-paced world",
    "ai magic",
    "supercharge",
    "revolutionary",
    "game-changing",
    "let ai do it",
    "thanks for the connect",
    "appreciate the connect",
    "appreciate connecting",
    "great to connect and exchange insights",
    "would be great to connect and exchange insights",
    "would be great to connect",
    "exchange insights",
    "follow your insights",
    "follow insights",
    "valuable insights",
    "share insights",
    "swap insights",
    "i would love to connect",
    "love to connect",
    "i came across your impressive profile",
    "came across your profile",
    "impressive profile",
    "your remarkable experience",
    "remarkable experience",
    "your extensive experience",
    "extensive experience",
    "extensive professional experience",
    "your professional journey",
    "professional journey",
    "i am reaching out because",
    "i am reaching out",
    "reaching out because",
    "reaching out to connect",
    "i noticed your impressive background",
    "impressive background",
    "i'd be delighted to connect",
    "i would be delighted to connect",
    "delighted to connect",
    "i'd be glad to connect",
    "i would be glad to connect",
    "glad to connect",
    "excited to connect",
    "thrilled to connect",
    "look forward to connecting",
    "looking forward to connecting",
    "explore synergies",
    "synergies",
    "connect with like-minded professionals",
    "like-minded professionals",
    "mutually beneficial",
    "highly relevant to my goals",
    "relevant to my goals",
    "connect and learn from you",
    "connect and learn",
    "learn from your experience",
    "learn from your insights",
    "regarding my previous correspondence",
    "i wanted to follow up regarding my previous correspondence",
    "best regards",
    "warm regards",
    "kind regards",
    "sincerely",
    "yours truly",
    # Unsupported relational/familiarity claims (prohibited unless proven by evidence)
    "following your work",
    "followed your work",
    "been following your work",
    "following your posts",
    "followed your posts",
    "been following your posts",
    "seen your recent posts",
    "saw your recent post",
    "admired your work",
    "admire your work",
    "keeping up with your company",
    "loved your article",
    "read your article",
    "saw your talk",
    "watched your talk",
    "we share a passion",
    "we both value",
    "we have mutual interests",
    "your work inspired me",
    "inspired by your work",
]

UNSUPPORTED_RELATIONAL_PATTERNS = [
    re.compile(r"\b(been\s+)?following\s+your\s+(work|posts?|content|updates)\b", re.IGNORECASE),
    re.compile(r"\b(seen|saw|read|loved|watched)\s+your\s+(recent\s+)?(posts?|articles?|talks?)\b", re.IGNORECASE),
    re.compile(r"\badmired?\s+your\s+work\b", re.IGNORECASE),
    re.compile(r"\bkeeping\s+up\s+with\s+your\s+company\b", re.IGNORECASE),
    re.compile(r"\bwe\s+share\s+a\s+passion\b", re.IGNORECASE),
    re.compile(r"\bwe\s+both\s+value\b", re.IGNORECASE),
    re.compile(r"\bwe\s+have\s+mutual\s+interests\b", re.IGNORECASE),
    re.compile(r"\b(your\s+work\s+inspired\s+me|inspired\s+by\s+your\s+work)\b", re.IGNORECASE),
]

GENERIC_CORPORATE_PATTERNS = [
    re.compile(r"\bwould\s+be\s+great\s+to\s+connect\b", re.IGNORECASE),
    re.compile(r"\bexchange\s+insights\b", re.IGNORECASE),
    re.compile(r"\bexplore\s+synergies\b", re.IGNORECASE),
    re.compile(r"\blike-minded\s+professionals\b", re.IGNORECASE),
    re.compile(r"\bimpressive\s+(profile|background|experience)\b", re.IGNORECASE),
    re.compile(r"\bremarkable\s+experience\b", re.IGNORECASE),
    re.compile(r"\bi\s*(?:am|['’]?m)\s+reaching\s+out\s+because\b", re.IGNORECASE),
    re.compile(r"\b(?:glad|delighted|excited|thrilled|pleased)\s+to\s+connect\b", re.IGNORECASE),
    re.compile(r"\b(?:follow|share|exchange|swap)\s+(?:your\s+)?insights\b", re.IGNORECASE),
    re.compile(r"\b(?:highly\s+)?relevant\s+to\s+my\s+goals\b", re.IGNORECASE),
    re.compile(r"\b(?:extensive|remarkable|vast|wealth\s+of)\s+(?:professional\s+)?experience\b", re.IGNORECASE),
    re.compile(r"\bconnect\s+and\s+learn\s+(?:from\s+you)?\b", re.IGNORECASE),
    re.compile(r"\bleverage\s+(?:my|our)\s+expertise\b", re.IGNORECASE),
    re.compile(r"\bmutually\s+beneficial\b", re.IGNORECASE),
    re.compile(r"\bprofessional\s+journey\b", re.IGNORECASE),
    re.compile(r"\bthank\s+you\s+for\s+your\s+(?:insightful|valuable|thoughtful)\s+(?:response|reply|message|perspective)\b", re.IGNORECASE),
    re.compile(r"\bgreatly\s+appreciate\s+your\s+perspective\b", re.IGNORECASE),
    re.compile(r"\bwould\s+be\s+delighted\s+to\s+discuss\s+(?:this\s+)?further\b", re.IGNORECASE),
    re.compile(r"\bregarding\s+my\s+previous\s+correspondence\b", re.IGNORECASE),
]

FORMAL_OPENING_PATTERNS = [
    re.compile(r"^Hello\b", re.IGNORECASE),
    re.compile(r"^Dear\s+[A-Z][a-z]+\b", re.IGNORECASE),
    re.compile(r"^Hi\s+[A-Z][a-z]+,\s+I\s+saw\s+you\s+work\b", re.IGNORECASE),
    re.compile(r"^Greetings\b", re.IGNORECASE),
    re.compile(r"^To\s+whom\s+it\s+may\s+concern\b", re.IGNORECASE),
]

RESUME_DUMP_PATTERNS = [
    re.compile(r"\b(?:completing|pursuing|holding|have|completed)\s+(?:a\s+)?degrees?\s+in\b", re.IGNORECASE),
    re.compile(r"\bI\s*(?:'m|\s+am)\s+a\s+.*(?:graduate|student|alumnus|alumna)\b", re.IGNORECASE),
    re.compile(r"\bI\s+have\s+a\s+background\s+in\b", re.IGNORECASE),
    re.compile(r"\bMy\s+background\s+is\s+in\b", re.IGNORECASE),
    re.compile(r"\bMy\s+expertise\s+(?:includes|lies\s+in|spans)\b", re.IGNORECASE),
    re.compile(r"\b(?:building|founder|startup)\b.*\b(?:competed|challenges?|physics-informed|degree|alumnus)\b", re.IGNORECASE),
    re.compile(r"\bi\s*['’]?m\s+building\b.*\band\s+recently\b", re.IGNORECASE),
    re.compile(r"\bincluding\s+a\s+.*startup\s+and\s+recently\b", re.IGNORECASE),
    re.compile(r"\b(?:completing|degrees?|graduate)\b.*\b(?:building|startup|tools)\b", re.IGNORECASE),
]

FORMAL_CLOSING_PATTERNS = [
    re.compile(r"\b(?:best\s+regards|warm\s+regards|kind\s+regards|sincerely|yours\s+truly|respectfully)\b", re.IGNORECASE),
    re.compile(r"\blook\s+forward\s+to\s+(?:connecting|hearing\s+from\s+you|your\s+reply)\b", re.IGNORECASE),
]

STIFF_OPENER_PATTERNS = FORMAL_OPENING_PATTERNS

# Synthetic examples only; no private correspondence is included.
CANDIDATE_LINKEDIN_CORPUS = [{'tag': 'OPENING', 'text': 'hey Alex, noticed your work on data tools. what are you building this month?'}, {'tag': 'CONNECTION', 'text': 'hey Morgan, your work on queue reliability caught my eye. happy to compare notes.'}, {'tag': 'FOLLOW_UP', 'text': 'hey Alex, circling back in case this got buried. still interested in comparing notes?'}, {'tag': 'TECHNICAL_REPLY', 'text': 'that makes sense. separate worker queues helped us keep retries predictable.'}, {'tag': 'INTEREST_REPLY', 'text': 'sounds useful. happy to talk through the approach when you have time.'}]

PLACEHOLDER_PATTERN = re.compile(r'\[[a-zA-Z0-9\s_]+\]')
MALFORMED_FRAGMENT_PATTERNS = [
    re.compile(r"\bI'm,\s*a\s*at\b", re.IGNORECASE),
    re.compile(r"\bI'm\s+a\s+at\b", re.IGNORECASE),
    re.compile(r",\s*a\s+at\b", re.IGNORECASE),
    re.compile(r"\bat\s*\.", re.IGNORECASE),
    re.compile(r"\bI'm\s*,\s*", re.IGNORECASE),
]

APPROVED_SLANG_TERMS = [
    "tbh", "rn", "yeah", "nah", "mate", "fair call", "fair question",
    "pretty cool", "proper useful", "you guys", "appreciate it", "caught my eye",
    "saw what you guys", "swap notes", "super clean", "ping me", "all good", "got you"
]

APPROVED_EMOJIS = ["👀", "😅", "🙌", "🙂"]


def tokenize_prose(text):
    """
    Splits text into (is_prose, segment) pairs.
    Exempts fenced code, inline code, URLs, file paths, command-line flags, and technical identifiers.
    """
    text = unicodedata.normalize("NFC", text)
    
    patterns = [
        r'(```[\s\S]*?```)',  # Fenced code blocks
        r'(`[^`\n]+`)',       # Inline code
        r'(https?://\S+)',     # URLs
        r'(file://\S+)',       # file URLs
        r'((?:\.\.?/|/)[a-zA-Z0-9_\-\./]+)', # File paths starting with / or ./ or ../
        r'(\b[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b)', # Filenames with extensions like script.py
        r'(--[a-zA-Z0-9_\-]+)', # Command-line flags (e.g. --dry-run)
        r'(\b[a-zA-Z0-9_\-]+=[a-zA-Z0-9_\-]+\b)', # key=value assignments
        r'(\b[A-Z0-9_]{3,}\b)', # Technical constants/screaming snake case
    ]
    
    combined_pattern = re.compile('|'.join(patterns))
    
    last_idx = 0
    segments = []
    
    for match in combined_pattern.finditer(text):
        start, end = match.span()
        if start > last_idx:
            segments.append((True, text[last_idx:start]))
        segments.append((False, text[start:end]))
        last_idx = end
        
    if last_idx < len(text):
        segments.append((True, text[last_idx:]))
        
    return segments


def validate_user_facing_content_style(text, content_type, context=None):
    """
    Validates text against prohibited characters, corporate clichés, resume dumps, formal openings,
    and LinkedIn length constraints.
    Returns:
        dict: {'valid': bool, 'cleaned_text': str, 'errors': list, 'error_codes': list, 'style_metrics': dict}
    """
    if not text or not text.strip():
        return {
            "valid": False,
            "cleaned_text": "",
            "errors": ["Message is empty or whitespace only."],
            "error_codes": ["EMPTY_MESSAGE"],
            "style_metrics": {"length": 0, "question_count": 0, "is_chat_like": False},
        }
        
    errors = []
    error_codes = []
    segments = tokenize_prose(text)
    
    for is_prose, seg in segments:
        if not is_prose:
            continue
            
        # Check EM DASH, EN DASH, double hyphens as punctuation
        if "\u2014" in seg:
            errors.append("Prohibited em dash (U+2014) found in prose.")
            error_codes.append("PROHIBITED_PUNCTUATION")
        if "\u2013" in seg:
            errors.append("Prohibited en dash (U+2013) found in prose.")
            error_codes.append("PROHIBITED_PUNCTUATION")
        if "--" in seg:
            errors.append("Prohibited double hyphen used as punctuation.")
            error_codes.append("PROHIBITED_PUNCTUATION")
            
        # Check canned phrases
        seg_lower = seg.lower()
        if content_type == "connection_note" and ("thanks for connecting" in seg_lower or "great to connect" in seg_lower):
            errors.append("Premature 'thanks for connecting' in connection request note before connecting.")
            error_codes.append("FORMAL_OPENING")

        for phrase in PROHIBITED_PHRASES:
            if phrase in seg_lower:
                errors.append(f"Prohibited canned phrase found: '{phrase}'")
                error_codes.append("CORPORATE_LANGUAGE")
                
        # Check placeholders
        if PLACEHOLDER_PATTERN.search(seg):
            errors.append("Placeholder matching [PlaceHolder] pattern found.")
            error_codes.append("PLACEHOLDER_FOUND")

        # Check unsupported relational/familiarity patterns
        for pattern in UNSUPPORTED_RELATIONAL_PATTERNS:
            if pattern.search(seg):
                errors.append(f"Prohibited unsupported relational claim matching '{pattern.pattern}' found.")
                error_codes.append("UNSUPPORTED_FAMILIARITY")

        # Check malformed fragments
        for pattern in MALFORMED_FRAGMENT_PATTERNS:
            if pattern.search(seg):
                errors.append(f"Malformed prose fragment matching pattern {pattern.pattern} found.")
                error_codes.append("MALFORMED_FRAGMENT")

        # Check generic corporate patterns
        for pattern in GENERIC_CORPORATE_PATTERNS:
            if pattern.search(seg):
                errors.append(f"Generic corporate cliché matching '{pattern.pattern}' found.")
                error_codes.append("CORPORATE_LANGUAGE")

        # Check resume dumping and formal opening (in opening messages / connection notes / followups)
        if content_type in ("connection_note", "opener", "job_outreach", "follow_up", "first_message"):
            for pattern in RESUME_DUMP_PATTERNS:
                if pattern.search(seg):
                    errors.append(f"Resume-dump detected: multiple self-promotional credentials in message ({pattern.pattern}).")
                    error_codes.append("RESUME_DUMP")
            for pattern in FORMAL_OPENING_PATTERNS:
                if pattern.search(seg.strip()):
                    errors.append(f"Stiff formal opening detected ({pattern.pattern}). Use casual, natural greeting (e.g. 'hey', 'saw', 'noticed').")
                    error_codes.append("FORMAL_OPENING")
            for pattern in FORMAL_CLOSING_PATTERNS:
                if pattern.search(seg):
                    errors.append(f"Formal corporate closing detected ({pattern.pattern}). Remove formal sign-off.")
                    error_codes.append("OVERPOLISHED")

    # Multi-fact stacking check for opening messages
    if content_type in ("connection_note", "opener", "first_message"):
        context_signals = 0
        seg_lower = text.lower()
        if any(w in seg_lower for w in ["degrees in", "completing", "graduated", "studying"]):
            context_signals += 1
        if any(w in seg_lower for w in ["geosentry", "startup", "tools for", "founder"]):
            context_signals += 1
        if any(w in seg_lower for w in ["relevant to my goals", "my career", "my goals"]):
            context_signals += 1
        if any(w in seg_lower for w in ["experience in", "background in"]):
            context_signals += 1
        if context_signals >= 3:
            errors.append("Too much contextual stacking in opener — keep personalization sparse (1-2 details max).")
            error_codes.append("TOO_MUCH_CONTEXT")

    # Question count constraint: Maximum 1 question per message
    q_count = text.count("?")
    if q_count > 1:
        errors.append(f"Too many questions asked in message ({q_count}) — limit is maximum 1 question.")
        error_codes.append("QUESTION_LIMIT_EXCEEDED")

    # Validate channel length constraints
    if content_type in ("connection_note", "opener", "first_message"):
        max_len = 300
        if context and "max_length" in context:
            max_len = context["max_length"]
        if len(text) > max_len:
            errors.append(f"Content length {len(text)} exceeds limit of {max_len} characters.")
            error_codes.append("LENGTH_EXCEEDED")

    # Compute positive style metrics
    text_stripped = text.strip()
    starts_lower = bool(text_stripped and (text_stripped[0].islower() or text_stripped.lower().startswith(("hey ", "saw ", "noticed ", "thanks ", "that ", "fair ", "yeah ", "appreciate "))))
    has_ellipsis = "..." in text or "…" in text
    has_plus = " + " in text or "+" in text
    has_slang = any(slang in text_stripped.lower() for slang in APPROVED_SLANG_TERMS)
    has_emoji = any(em in text for em in APPROVED_EMOJIS)
    
    style_metrics = {
        "length": len(text_stripped),
        "question_count": q_count,
        "starts_lowercase": starts_lower,
        "has_ellipsis": has_ellipsis,
        "has_plus_sign": has_plus,
        "has_slang": has_slang,
        "has_emoji": has_emoji,
        "is_chat_like": len(errors) == 0 and (starts_lower or has_ellipsis or has_plus or has_slang),
    }

    # Deduplicate errors and codes while preserving order
    unique_errors = []
    for e in errors:
        if e not in unique_errors:
            unique_errors.append(e)
            
    unique_codes = []
    for c in error_codes:
        if c not in unique_codes:
            unique_codes.append(c)

    return {
        "valid": len(unique_errors) == 0,
        "cleaned_text": text,
        "errors": unique_errors,
        "error_codes": unique_codes,
        "style_metrics": style_metrics,
    }


def validate_candidate_message_style(text: str, content_type: str = "connection_note", context: dict = None) -> dict:
    """Validates message against Candidate's authentic personal writing style.

    Rejects:
    - Empty or whitespace-only messages
    - Corporate stiffness and sales templates
    - Generic outreach clichés ('exchange insights', 'explore synergies', 'highly relevant to my goals')
    - Resume dumping before establishing conversation ('completing degrees in...', 'building X startup...')
    - Stiff formal openings ('Hello, ...', 'Dear ...')
    - Formal closings ('Best regards', 'Sincerely', 'Look forward to connecting')
    - Unsupported familiarity ('been following your work')
    - More than 1 question
    - Prohibited punctuation (em dashes, double hyphens)
    """
    return validate_user_facing_content_style(text, content_type, context=context)


def validate_generated_content_claims(text, context=None):
    """
    Compares generated statements against approved CandidateFact records, profile versions,
    verified job facts, and permitted source documents to detect unsupported claims.
    Enforces strict prospect claim-ledger grounding.
    """
    if not text or not text.strip():
        return {"valid": False, "errors": ["Message is empty or whitespace only."]}
        
    errors = []
    if context and context.get("identity_unverified", False):
        errors.append("Identity is unverified, cannot make verified outreach claims.")
    if context and context.get("incident_hold_active", False):
        errors.append("Incident hold is active, cannot authorize outreach.")
        
    forbidden = "idwsdsjtasdkk"
    if forbidden in text:
        errors.append("Unsupported/contaminated identity reference found in text.")

    text_lower = text.lower()

    if context:
        lead_title = str(context.get("title") or context.get("current_title") or context.get("headline") or "").lower()
        lead_company = str(context.get("company") or "").lower()
        lead_about = str(context.get("about") or context.get("summary") or "").lower()
        
        industry_raw = context.get("industry")
        if isinstance(industry_raw, dict):
            lead_industry = str(industry_raw.get("name") or "").lower()
        else:
            lead_industry = str(industry_raw or "").lower()

        observed_evidence = " ".join([
            lead_title,
            lead_company,
            lead_about,
            lead_industry,
            " ".join([str(p.get("title", "")) + " " + str(p.get("company_name", "")) for p in (context.get("positions") or [])])
        ]).lower()

        # 1. Non-technical marketing/sales/operations role claiming technical product infrastructure
        non_tech_keywords = ["marketing", "sales", "hr", "recruiter", "talent", "finance", "legal", "writer", "operations associate"]
        is_non_tech = any(k in lead_title for k in non_tech_keywords) and not any(t in lead_title for t in ["engineer", "developer", "cto", "tech lead"])
        
        if is_non_tech:
            tech_infra_claims = ["product infrastructure", "backend systems", "engineering architecture", "distributed python workers", "raw tile indexing"]
            for claim in tech_infra_claims:
                if f"working on {claim}" in text_lower or f"leading {claim}" in text_lower or f"your {claim}" in text_lower or f"focus on {claim}" in text_lower:
                    errors.append(f"Factual claim conflict: lead title is '{lead_title}' but message asserts they work on '{claim}'.")

        # 2. Individual contributor role claiming leadership / leading engineering
        is_ic = any(k in lead_title for k in ["engineer", "developer", "analyst", "associate", "specialist", "student"]) and not any(k in lead_title for k in ["lead", "head", "director", "vp", "manager", "chief", "cto", "ceo", "founder", "principal", "co-founder"])
        if is_ic:
            leadership_claims = ["leading engineering", "running engineering", "leading the engineering", "heading engineering", "leading product", "software engineering leadership"]
            for claim in leadership_claims:
                if claim in text_lower:
                    errors.append(f"Factual claim conflict: lead title is individual contributor '{lead_title}' but message asserts '{claim}'.")

        # 3. AI / Automation assertion about prospect without evidence
        if any(pat in text_lower for pat in ["saw your work on ai", "saw your work on automation", "saw your focus on ai", "noticed your work on ai", "work on ai/automation", "focus on ai/automation", "experience in ai/automation"]):
            if not any(k in observed_evidence for k in ["ai", "artificial intelligence", "automation", "machine learning", "ml", "nlp", "llm"]):
                errors.append("Unsupported claim: message asserts prospect works on AI/automation without provider evidence.")

        # 4. Python / Backend / Architecture premise without evidence
        if any(pat in text_lower for pat in ["backend python", "python architecture", "backend systems", "python projects", "distributed python"]):
            if not any(k in observed_evidence for k in ["python", "architect", "backend", "software developer", "software engineer"]):
                errors.append("Unsupported claim: message asserts prospect works on Python/backend architecture without provider evidence.")

        # 5. Management consulting assertion without evidence
        if "management consulting" in text_lower or "consulting trends" in text_lower:
            if "consulting" not in observed_evidence and "consultant" not in observed_evidence:
                errors.append("Unsupported claim: message asserts management consulting without provider evidence.")

        # 6. Real-time data processing assertion without evidence
        if "real-time data" in text_lower or "data processing" in text_lower:
            if not any(k in observed_evidence for k in ["data", "etl", "pipeline", "analytics", "real-time", "realtime"]):
                errors.append("Unsupported claim: message asserts real-time data processing without provider evidence.")

        # 7. CI/CD assertion without evidence
        if "ci/cd" in text_lower or "cicd" in text_lower:
            if not any(k in observed_evidence for k in ["ci/cd", "cicd", "devops", "infrastructure", "cloud"]):
                errors.append("Unsupported claim: message asserts CI/CD scaling without provider evidence.")

        # 8. Mismatched title assertions
        if "software developer" in text_lower and "developer" not in lead_title and "engineer" not in lead_title:
            errors.append(f"Factual claim conflict: text claims 'software developer' but lead title is '{lead_title}'.")

        # 9. Production models / running in prod operational assumptions (including in questions)
        prod_model_patterns = [
            r"\b(?:what|which)\s+models?\s+(?:you\s+guys\s+are|are\s+you|are\s+you\s+guys|is\s+your\s+team)\s+(?:running|deploying|serving|training|building)\b",
            r"\b(?:models?|services?|workloads?|apps?)\s+(?:you(?:'re|\s+are|\s+guys\s+are)?\s+)?(?:running|deploying|serving)\s+in\s+prod\b",
            r"\b(?:running|run|deploying|deploy)\s+(?:in\s+prod|in\s+production)\b",
            r"\b(?:production|prod)\s+(?:ml\s+)?models?\b",
            r"\b(?:the\s+models\s+your\s+team\s+(?:is\s+)?(?:deploying|running))\b",
            r"\b(?:your\s+production\s+models?)\b",
            r"\b(?:models?\s+in\s+prod)\b",
        ]
        if any(re.search(pat, text_lower) for pat in prod_model_patterns):
            if not any(k in observed_evidence for k in ["production model", "models in prod", "running in prod", "prod model", "production ml", "production deployment"]):
                errors.append("Unsupported claim: message assumes/presupposes prospect runs models in production without provider evidence.")

        # 10. Deploying AI agents assumption (including in questions)
        agent_patterns = [
            r"\b(?:how\s+are\s+you(?:'re|\s+guys)?\s+deploying\s+agents)\b",
            r"\b(?:deploying|building|running)\s+(?:ai\s+)?agents\b",
            r"\b(?:your\s+agent\s+framework)\b",
        ]
        if any(re.search(pat, text_lower) for pat in agent_patterns):
            if not any(k in observed_evidence for k in ["agent", "agents", "autonomous", "llm agent"]):
                errors.append("Unsupported claim: message assumes/presupposes prospect deploys AI agents without provider evidence.")

        # 11. Kubernetes / Specific tech stack assumption (including in questions)
        if re.search(r"\b(?:how\s+are\s+you\s+handling\s+kubernetes|handling\s+k8s|kubernetes\s+cluster|your\s+kubernetes)\b", text_lower):
            if not any(k in observed_evidence for k in ["kubernetes", "k8s", "container orchestration"]):
                errors.append("Unsupported claim: message assumes/presupposes Kubernetes usage without provider evidence.")

        if re.search(r"\b(?:what\s+stack\s+are\s+you\s+using|what\s+tech\s+stack\s+are\s+you\s+using|your\s+tech\s+stack|your\s+stack)\b", text_lower):
            if not any(k in observed_evidence for k in ["tech stack", "stack", "fullstack", "full stack"]):
                errors.append("Unsupported claim: message assumes/presupposes specific tech stack inquiry without provider evidence.")

        # 12. Pipeline scaling assumption (including in questions)
        if re.search(r"\b(?:how\s+are\s+you\s+scaling\s+the\s+pipeline|scaling\s+the\s+pipeline|scaling\s+pipelines)\b", text_lower):
            if not any(k in observed_evidence for k in ["pipeline", "scaling", "data engineer", "etl", "data pipeline"]):
                errors.append("Unsupported claim: message assumes/presupposes pipeline scaling without provider evidence.")

        # 13. ML platform team / Recommendation systems / Backend architecture assumptions
        if re.search(r"\b(?:your\s+ml\s+platform|the\s+ml\s+platform|(?:ml|ai)\s+platform\s+team)\b", text_lower):
            if not any(k in observed_evidence for k in ["platform", "mlops", "infra"]):
                errors.append("Unsupported claim: message assumes prospect works on ML platform team without provider evidence.")

        if re.search(r"\b(?:recommendation\s+(?:systems?|engine|pipeline)s?|recsys)\b", text_lower):
            if not any(k in observed_evidence for k in ["recommendation", "recsys", "ranking"]):
                errors.append("Unsupported claim: message assumes prospect works on recommendation systems without provider evidence.")

        if re.search(r"\b(?:your\s+backend\s+architecture)\b", text_lower):
            if not any(k in observed_evidence for k in ["backend", "architect", "architecture"]):
                errors.append("Unsupported claim: message assumes prospect works on backend architecture without provider evidence.")

        # 14. Project leadership assumption on individual contributor or unsubstantiated project inquiry
        if re.search(r"\b(?:what\s+projects\s+(?:are\s+you|you(?:'re|\s+are))\s+leading|what\s+projects\s+are\s+you\s+heading|projects\s+you(?:'re|\s+are)\s+leading)\b", text_lower):
            if not any(k in observed_evidence for k in ["project lead", "leading projects", "lead project"]):
                errors.append("Unsupported claim: message assumes project leadership without provider evidence.")

        # 15. LLMs / Foundation models deployment assumption (including in questions)
        llm_patterns = [
            r"\b(?:what|which)\s+(?:llms?|foundation\s+models?)\s+(?:are\s+you|are\s+you\s+guys|you\s+guys\s+are|is\s+your\s+team)\s+(?:deploying|running|using|serving|building)\b",
            r"\b(?:deploying|running|serving|using)\s+(?:llms?|large\s+language\s+models?|foundation\s+models?)\b",
            r"\b(?:your\s+llms?)\b",
        ]
        if any(re.search(pat, text_lower) for pat in llm_patterns):
            if not any(k in observed_evidence for k in ["llm", "large language model", "foundation model", "nlp"]):
                errors.append("Unsupported claim: message assumes/presupposes LLM deployment without provider evidence.")

        # 16. Specific inference stack assumption
        inference_patterns = [
            r"\b(?:your\s+inference\s+(?:stack|pipeline|infrastructure|engine))\b",
            r"\b(?:the\s+inference\s+(?:stack|pipeline|infrastructure|engine))\b",
            r"\b(?:inference\s+stack)\b",
        ]
        if any(re.search(pat, text_lower) for pat in inference_patterns):
            if not any(k in observed_evidence for k in ["inference", "model serving", "serving stack"]):
                errors.append("Unsupported claim: message assumes/presupposes specific inference stack without provider evidence.")

        # 17. Distributed AI / Infrastructure assumption
        distributed_patterns = [
            r"\b(?:your\s+distributed\s+(?:ai|ml|infrastructure|systems?|training))\b",
            r"\b(?:distributed\s+(?:ai|ml)\s+infrastructure)\b",
        ]
        if any(re.search(pat, text_lower) for pat in distributed_patterns):
            if not any(k in observed_evidence for k in ["distributed", "cluster", "distributed systems", "distributed ai", "distributed ml"]):
                errors.append("Unsupported claim: message assumes/presupposes distributed AI infrastructure without provider evidence.")

    return {
        "valid": len(errors) == 0,
        "errors": errors
    }


def validate_generated_claims(text, verified_facts=None, context=None):
    if context is None:
        context = {}
    return validate_generated_content_claims(text, context)


def validate_linkedin_outbound_message(
    message_text: str,
    content_type: str = "first_message",
    workspace=None,
    linkedin_profile=None,
    prospect_facts: dict = None,
    campaign_context: dict = None,
    context: dict = None,
    lead=None,
    deal=None,
) -> dict:
    """
    Canonical final outbound gate for ALL LinkedIn prose.
    Called immediately before any provider send/type/click action.

    Supported content_types:
    - connection_note (connection request note)
    - first_message / post_connection_message (first DM after connection acceptance)
    - follow_up (follow-up in message thread)
    - inbound_reply / ai_reply (reply to inbound message)
    - meeting_transition (transitioning to calendar/call)
    - job_networking (career exploration DM)
    - manual_outreach (operator-triggered message)

    Returns:
        dict: {
            'allowed': bool,
            'reason': 'OK' | 'MESSAGE_BLOCKED_STYLE' | 'MESSAGE_BLOCKED_CLAIMS' | 'MESSAGE_BLOCKED_RELATIONSHIP' | 'MESSAGE_BLOCKED_DUPLICATE' | 'MESSAGE_BLOCKED_UNCERTAIN_WRITE' | 'MESSAGE_BLOCKED_SAFETY_HOLD' | 'MESSAGE_BLOCKED_UNVERIFIED_IDENTITY',
            'block_reason': None | str,
            'cleaned_text': str,
            'errors': list[str],
            'error_codes': list[str],
            'style_metrics': dict,
            'style_result': dict,
            'claims_result': dict,
        }
    """
    if not message_text or not str(message_text).strip():
        return {
            "allowed": False,
            "reason": "MESSAGE_BLOCKED_STYLE",
            "block_reason": "MESSAGE_BLOCKED_STYLE",
            "cleaned_text": "",
            "errors": ["Message is empty or whitespace only."],
            "error_codes": ["EMPTY_MESSAGE"],
            "style_metrics": {"length": 0, "is_chat_like": False},
            "style_result": {"valid": False, "errors": ["Message is empty or whitespace only."], "error_codes": ["EMPTY_MESSAGE"]},
            "claims_result": {"valid": False, "errors": ["Message is empty or whitespace only."]},
        }

    # Resolve context if deal or lead provided
    effective_facts = {}
    if context:
        effective_facts.update(context)
    if prospect_facts:
        effective_facts.update(prospect_facts)
    if campaign_context:
        effective_facts.update(campaign_context)
    if lead and hasattr(lead, "to_profile_dict"):
        effective_facts.update(lead.to_profile_dict())
    if deal and deal.lead and hasattr(deal.lead, "to_profile_dict"):
        effective_facts.update(deal.lead.to_profile_dict())

    # Map aliases
    canon_type = content_type
    if canon_type == "post_connection_message":
        canon_type = "first_message"
    elif canon_type == "ai_reply":
        canon_type = "inbound_reply"

    # Check safety holds and identity if provided in context
    if effective_facts.get("incident_hold_active") is True:
        return {
            "allowed": False,
            "reason": "MESSAGE_BLOCKED_SAFETY_HOLD",
            "block_reason": "MESSAGE_BLOCKED_SAFETY_HOLD",
            "cleaned_text": str(message_text).strip() if message_text else "",
            "errors": ["Outbound messaging is temporarily paused due to an active incident hold."],
            "error_codes": ["INCIDENT_HOLD_ACTIVE"],
            "style_metrics": {},
            "style_result": {"valid": False, "errors": ["Incident hold active"], "error_codes": ["INCIDENT_HOLD_ACTIVE"]},
            "claims_result": {"valid": False, "errors": ["Incident hold active"]},
        }

    if effective_facts.get("identity_unverified") is True:
        return {
            "allowed": False,
            "reason": "MESSAGE_BLOCKED_UNVERIFIED_IDENTITY",
            "block_reason": "MESSAGE_BLOCKED_UNVERIFIED_IDENTITY",
            "cleaned_text": str(message_text).strip() if message_text else "",
            "errors": ["Candidate identity is unverified. Verified portfolio claims required."],
            "error_codes": ["UNVERIFIED_IDENTITY"],
            "style_metrics": {},
            "style_result": {"valid": False, "errors": ["Unverified identity"], "error_codes": ["UNVERIFIED_IDENTITY"]},
            "claims_result": {"valid": False, "errors": ["Unverified identity"]},
        }

    # Relationship Gate for first_message / follow_up when deal is provided
    deal_obj = deal
    if deal_obj is None and isinstance(context, dict):
        deal_obj = context.get("deal")

    if canon_type == "first_message" and deal_obj is not None:
        deal_st = getattr(deal_obj, "state", None)
        if deal_st != "Connected":
            return {
                "allowed": False,
                "reason": "MESSAGE_BLOCKED_RELATIONSHIP",
                "block_reason": "MESSAGE_BLOCKED_RELATIONSHIP",
                "cleaned_text": str(message_text).strip() if message_text else "",
                "errors": [f"first_message requires positive connected relationship state, but deal is '{deal_st}'."],
                "error_codes": ["DEAL_NOT_CONNECTED", "FIRST_MESSAGE_NOT_CONNECTED"],
                "style_metrics": {},
                "style_result": {"valid": True, "errors": [], "error_codes": []},
                "claims_result": {"valid": True, "errors": []},
            }

        target_prof = linkedin_profile
        camp_prof = None
        if deal_obj and hasattr(deal_obj, "campaign") and getattr(deal_obj.campaign, "linkedin_profile", None):
            camp_prof = deal_obj.campaign.linkedin_profile
        if target_prof is None:
            target_prof = camp_prof

        # Check ProviderRelationship positive evidence and freshness
        from openoutreach.linkedin.services.relationship_reconciliation import evaluate_relationship_for_first_message
        rel_ok, rel_reason = evaluate_relationship_for_first_message(
            lead=getattr(deal_obj, "lead", None) or lead,
            deal=deal_obj,
            workspace=workspace,
            linkedin_profile=target_prof,
        )
        if not rel_ok:
            return {
                "allowed": False,
                "reason": "MESSAGE_BLOCKED_RELATIONSHIP",
                "block_reason": "MESSAGE_BLOCKED_RELATIONSHIP",
                "cleaned_text": str(message_text).strip() if message_text else "",
                "errors": [f"first_message requires positive confirmed connected provider relationship: {rel_reason}"],
                "error_codes": ["PROVIDER_RELATIONSHIP_NOT_CONNECTED", "FIRST_MESSAGE_NOT_CONNECTED"],
                "style_metrics": {},
                "style_result": {"valid": True, "errors": [], "error_codes": []},
                "claims_result": {"valid": True, "errors": []},
            }

        # Check profile alignment
        if linkedin_profile and camp_prof and linkedin_profile != camp_prof:
            return {
                "allowed": False,
                "reason": "MESSAGE_BLOCKED_PROFILE_MISMATCH",
                "block_reason": "provider_session_profile_mismatch",
                "cleaned_text": str(message_text).strip() if message_text else "",
                "errors": [f"Session provider profile ({linkedin_profile}) does not match campaign profile ({camp_prof})"],
                "error_codes": ["PROVIDER_SESSION_PROFILE_MISMATCH"],
                "style_metrics": {},
                "style_result": {"valid": True, "errors": [], "error_codes": []},
                "claims_result": {"valid": True, "errors": []},
            }

        # Check duplicate outbound first message
        if getattr(deal_obj, "pk", None):
            from openoutreach.chat.models import ChatMessage
            if ChatMessage.objects.filter(deal=deal_obj, is_outgoing=True).exists():
                return {
                    "allowed": False,
                    "reason": "MESSAGE_BLOCKED_DUPLICATE",
                    "block_reason": "MESSAGE_BLOCKED_DUPLICATE",
                    "cleaned_text": str(message_text).strip() if message_text else "",
                    "errors": ["Outbound message already sent for this connected deal — cannot send duplicate first_message."],
                    "error_codes": ["DUPLICATE_MESSAGE_DETECTED"],
                    "style_metrics": {},
                    "style_result": {"valid": True, "errors": [], "error_codes": []},
                    "claims_result": {"valid": True, "errors": []},
                }

        # Check uncertain write intents
        if getattr(deal_obj, "lead", None) and getattr(deal_obj.lead, "public_identifier", None):
            from openoutreach.linkedin.models import ProviderWriteIntent
            if ProviderWriteIntent.objects.filter(
                target_identity=deal_obj.lead.public_identifier,
                state=ProviderWriteIntent.State.UNCERTAIN,
            ).exists():
                return {
                    "allowed": False,
                    "reason": "MESSAGE_BLOCKED_UNCERTAIN_WRITE",
                    "block_reason": "MESSAGE_BLOCKED_UNCERTAIN_WRITE",
                    "cleaned_text": str(message_text).strip() if message_text else "",
                    "errors": ["Uncertain provider write intent exists for target — write blocked until reconciled."],
                    "error_codes": ["UNCERTAIN_PROVIDER_WRITE_EXISTS"],
                    "style_metrics": {},
                    "style_result": {"valid": True, "errors": [], "error_codes": []},
                    "claims_result": {"valid": True, "errors": []},
                }

    style_res = validate_user_facing_content_style(message_text, content_type=canon_type, context=effective_facts)
    claims_res = validate_generated_content_claims(message_text, context=effective_facts)

    style_valid = style_res["valid"]
    claims_valid = claims_res["valid"]
    allowed = style_valid and claims_valid

    if allowed:
        reason = "OK"
        block_reason = None
    elif not style_valid:
        reason = "MESSAGE_BLOCKED_STYLE"
        block_reason = "MESSAGE_BLOCKED_STYLE"
    else:
        reason = "MESSAGE_BLOCKED_CLAIMS"
        block_reason = "MESSAGE_BLOCKED_CLAIMS"

    combined_errors = list(style_res.get("errors", []))
    for err in claims_res.get("errors", []):
        if err not in combined_errors:
            combined_errors.append(err)

    combined_codes = list(style_res.get("error_codes", []))
    if not claims_valid:
        if "UNSUPPORTED_PROSPECT_CLAIM" not in combined_codes:
            combined_codes.append("UNSUPPORTED_PROSPECT_CLAIM")
        if "UNSUPPORTED_CLAIM" not in combined_codes:
            combined_codes.append("UNSUPPORTED_CLAIM")

    return {
        "allowed": allowed,
        "reason": reason,
        "block_reason": block_reason,
        "cleaned_text": style_res.get("cleaned_text", str(message_text).strip() if message_text else ""),
        "errors": combined_errors,
        "error_codes": combined_codes,
        "style_metrics": style_res.get("style_metrics", {}),
        "style_result": style_res,
        "claims_result": claims_res,
    }

