"""
PubMed Weekly Digest — v5.1
============================
Same as v5 plus:
- NCBI rate limiter to stay under 3/sec (or 10/sec if NCBI_API_KEY set)
- Automatic retry on 429 Too Many Requests
"""

import os
import time
import asyncio
import tempfile
import re
import html
import smtplib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from groq import Groq
import edge_tts
import nest_asyncio
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, CHAP, CTOC, TIT2, CTOCFlags

nest_asyncio.apply()

# ===================================================================
# CONFIGURATION
# ===================================================================

GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "PASTE_FOR_LOCAL_TESTING")
NCBI_API_KEY        = os.environ.get("NCBI_API_KEY", "")   # optional but recommended
GMAIL_USER          = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO            = os.environ.get("EMAIL_TO", "")

OUTPUT_DIR    = os.environ.get("OUTPUT_DIR", ".")
EPISODES_DIR  = os.path.join(OUTPUT_DIR, "episodes")
RSS_PATH      = os.path.join(OUTPUT_DIR, "feed.xml")
PODCAST_BASE_URL = os.environ.get(
    "PODCAST_BASE_URL",
    "https://YOUR_USERNAME.github.io/YOUR_REPO"
)

PODCAST_TITLE       = "PubMed Weekly Digest — Retina & Ocular Oncology"
PODCAST_DESCRIPTION = ("Weekly research updates in ocular oncology (excluding retinoblastoma), "
                       "vitreoretinal surgery, ophthalmic surgical robotics, and AI applications "
                       "in vitreoretinal surgery and ocular oncology.")
PODCAST_AUTHOR      = "Ivo De Clerck"

TOPICS = {
    "Ocular Oncology (excluding retinoblastoma)": (
        '('
          '"Eye Neoplasms"[MeSH] '
          'OR "Uveal Melanoma"[MeSH] '
          'OR "ocular surface squamous neoplasia"[tiab] '
          'OR "OSSN"[tiab] '
          'OR "conjunctival melanoma"[tiab] '
          'OR "conjunctival lymphoma"[tiab] '
          'OR "conjunctival squamous cell carcinoma"[tiab] '
          'OR "uveal melanoma"[tiab] '
          'OR "iris melanoma"[tiab] '
          'OR "ciliary body melanoma"[tiab] '
          'OR "choroidal melanoma"[tiab] '
          'OR "choroidal metastasis"[tiab] '
          'OR "choroidal metastases"[tiab] '
          'OR "retinal metastasis"[tiab] '
          'OR "uveal metastasis"[tiab] '
          'OR "choroidal hemangioma"[tiab] '
          'OR "vasoproliferative tumor"[tiab] '
          'OR "vasoproliferative tumour"[tiab] '
          'OR "retinal hemangioblastoma"[tiab] '
          'OR "capillary hemangioma"[tiab] '
          'OR "retinal capillary hemangioma"[tiab] '
          'OR "vitreoretinal lymphoma"[tiab] '
          'OR "primary intraocular lymphoma"[tiab] '
          'OR "ocular oncology"[tiab]'
        ') '
        'NOT ("Retinoblastoma"[Majr] OR retinoblastoma[ti])'
    ),

    "Vitreoretinal Surgery": (
        '"Vitrectomy"[MeSH] '
        'OR "vitrectomy"[tiab] '
        'OR "pars plana vitrectomy"[tiab] '
        'OR "vitreoretinal surgery"[tiab] '
        'OR "Retinal Detachment/surgery"[MeSH] '
        'OR "rhegmatogenous retinal detachment"[tiab] '
        'OR "tractional retinal detachment"[tiab] '
        'OR "Vitreoretinopathy, Proliferative"[MeSH] '
        'OR "proliferative vitreoretinopathy"[tiab] '
        'OR "Epiretinal Membrane"[MeSH] '
        'OR "epiretinal membrane"[tiab] '
        'OR "macular pucker"[tiab] '
        'OR "Retinal Perforations"[MeSH] '
        'OR "macular hole"[tiab] '
        'OR "vitreomacular traction"[tiab] '
        'OR "vitreomacular adhesion"[tiab] '
        'OR "scleral buckle"[tiab] '
        'OR "scleral buckling"[tiab] '
        'OR "internal limiting membrane peeling"[tiab] '
        'OR "ILM peeling"[tiab] '
        'OR "submacular hemorrhage"[tiab] '
        'OR "Vitreous Hemorrhage/surgery"[MeSH] '
        'OR "giant retinal tear"[tiab]'
    ),

    "Surgical Robotics in Ophthalmology": (
        '('
          '"Robotic Surgical Procedures"[MeSH] '
          'OR "robotic surgery"[tiab] '
          'OR "robot-assisted surgery"[tiab] '
          'OR "robot assisted surgery"[tiab] '
          'OR "surgical robot"[tiab] '
          'OR "robotic"[tiab]'
        ') AND ('
          '"Ophthalmologic Surgical Procedures"[MeSH] '
          'OR "ophthalmic surgery"[tiab] '
          'OR "eye surgery"[tiab] '
          'OR "ocular surgery"[tiab] '
          'OR "intraocular surgery"[tiab] '
          'OR "vitreoretinal"[tiab] '
          'OR "retinal surgery"[tiab] '
          'OR "subretinal injection"[tiab] '
          'OR "cataract surgery"[tiab] '
          'OR "corneal transplantation"[tiab] '
          'OR "Ophthalmology"[MeSH]'
        ')'
    ),

    "AI in Vitreoretinal Surgery & Ocular Oncology": (
        '('
          '"Artificial Intelligence"[MeSH] '
          'OR "deep learning"[tiab] '
          'OR "machine learning"[tiab] '
          'OR "convolutional neural network"[tiab] '
          'OR "neural network"[tiab] '
          'OR "artificial intelligence"[tiab] '
          'OR "transformer"[tiab] '
          'OR "foundation model"[tiab]'
        ') AND ('
          '"Vitrectomy"[MeSH] '
          'OR "vitrectomy"[tiab] '
          'OR "vitreoretinal surgery"[tiab] '
          'OR "Retinal Detachment/surgery"[MeSH] '
          'OR "Epiretinal Membrane"[MeSH] '
          'OR "Retinal Perforations"[MeSH] '
          'OR "macular hole"[tiab] '
          'OR "Eye Neoplasms"[MeSH] '
          'OR "Uveal Melanoma"[MeSH] '
          'OR "uveal melanoma"[tiab] '
          'OR "ocular melanoma"[tiab] '
          'OR "conjunctival melanoma"[tiab] '
          'OR "vitreoretinal lymphoma"[tiab] '
          'OR "ocular oncology"[tiab] '
          'OR "intraocular tumor"[tiab] '
          'OR "intraocular tumour"[tiab] '
          'OR "ocular tumor"[tiab] '
          'OR "ocular tumour"[tiab]'
        ') NOT ("Retinoblastoma"[Majr])'
    ),
}

DAYS_BACK             = 7
MAX_PAPERS_PER_TOPIC  = 25
GROQ_MODEL            = "meta-llama/llama-4-scout-17b-16e-instruct"
SECONDS_BETWEEN_CALLS = 2
WORDS_ABSTRACT_ONLY   = 300
WORDS_FULL_TEXT       = 450
TTS_VOICE             = "en-US-AndrewNeural"
TTS_RATE              = "+0%"
TTS_PARALLELISM       = 10
MAX_EPISODES_TO_KEEP  = 12

# ===================================================================
# CODE
# ===================================================================

groq = Groq(api_key=GROQ_API_KEY)
NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# ---------- NCBI rate-limited fetcher ----------

_NCBI_LAST_CALL = 0.0
NCBI_MIN_INTERVAL = 0.12 if NCBI_API_KEY else 0.4   # seconds between any two NCBI calls


def _ncbi_get(endpoint, params, timeout=60, retries=4):
    """Throttled GET to an NCBI E-utilities endpoint, with retry on 429."""
    global _NCBI_LAST_CALL

    if NCBI_API_KEY:
        params = {**params, "api_key": NCBI_API_KEY}
    params = {**params, "tool": "pubmed-weekly-digest"}

    for attempt in range(retries):
        elapsed = time.monotonic() - _NCBI_LAST_CALL
        if elapsed < NCBI_MIN_INTERVAL:
            time.sleep(NCBI_MIN_INTERVAL - elapsed)
        _NCBI_LAST_CALL = time.monotonic()

        r = requests.get(f"{NCBI_BASE}/{endpoint}", params=params, timeout=timeout)
        if r.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"    NCBI 429 (rate-limited). Backing off {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r

    r.raise_for_status()
    return r


# ---------- PubMed ----------

# Publication-type exclusions, applied to every search.
#
# The [pt] filter only works after NCBI indexing completes (often 1-7 days
# after publication). Since our 7-day window catches papers that may not yet
# be indexed, we add title-based fallbacks that catch un-indexed papers by
# their title text — commentary and case reports follow predictable patterns.
EXCLUDED_PUB_TYPES = (
    # Indexed publication-type filters
    '"Case Reports"[pt] '
    'OR "Comment"[pt] '
    'OR "Editorial"[pt] '
    'OR "Published Erratum"[pt] '
    # Title-based fallbacks for papers NCBI hasn't indexed yet
    'OR "case report"[ti] '
    'OR "case series"[ti] '
    'OR "comment on"[ti] '
    'OR "in reply"[ti] '
    'OR "reply to"[ti] '
    'OR "letter to the editor"[ti] '
    'OR "author response"[ti] '
    'OR "erratum"[ti]'
    # ' OR "Letter"[pt]'   # uncomment if you also want letters excluded
)


def search_pubmed(query, days_back, max_results):
    full_query = f"({query}) NOT ({EXCLUDED_PUB_TYPES})"
    params = {"db": "pubmed", "term": full_query, "retmax": max_results,
              "retmode": "json", "reldate": days_back,
              "datetype": "pdat", "sort": "date"}
    r = _ncbi_get("esearch.fcgi", params, timeout=30)
    return r.json()["esearchresult"].get("idlist", [])


def fetch_abstracts(pmids):
    if not pmids: return []
    params = {"db": "pubmed", "id": ",".join(pmids),
              "rettype": "abstract", "retmode": "xml"}
    r = _ncbi_get("efetch.fcgi", params)
    root = ET.fromstring(r.text)

    papers = []
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//PMID") or ""
        title = (article.findtext(".//ArticleTitle") or "Untitled").strip()
        journal = (article.findtext(".//Journal/Title") or "").strip()

        abstract_chunks = []
        for node in article.findall(".//Abstract/AbstractText"):
            label = node.get("Label")
            text = "".join(node.itertext()).strip()
            if text:
                abstract_chunks.append(f"{label}: {text}" if label else text)
        abstract = " ".join(abstract_chunks) or "(No abstract available.)"

        authors = []
        first_affiliation = ""
        for i, a in enumerate(article.findall(".//AuthorList/Author")):
            last = a.findtext("LastName") or ""
            initials = a.findtext("Initials") or ""
            full = f"{last} {initials}".strip()
            if full: authors.append(full)
            if i == 0:
                aff = a.find(".//AffiliationInfo/Affiliation")
                if aff is not None and aff.text:
                    first_affiliation = aff.text.strip()

        pmcid = ""
        for art_id in article.findall(".//ArticleId"):
            if art_id.get("IdType") == "pmc":
                pmcid = (art_id.text or "").strip()
                break

        papers.append({
            "pmid": pmid, "title": title, "abstract": abstract,
            "journal": journal, "authors": authors,
            "first_author": authors[0] if authors else "Unknown",
            "last_author": authors[-1] if len(authors) > 1 else "",
            "affiliation": first_affiliation, "pmcid": pmcid,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return papers


def fetch_pmc_full_text(pmcid, max_chars=12000):
    if not pmcid: return None
    try:
        params = {"db": "pmc", "id": pmcid.replace("PMC", ""), "rettype": "xml"}
        r = _ncbi_get("efetch.fcgi", params)
        root = ET.fromstring(r.text)
        body = root.find(".//body")
        if body is None: return None

        keywords = ["result", "discussion", "conclusion", "finding", "implication"]
        priority, other = [], []
        for sec in body.findall("./sec"):
            title_elem = sec.find("./title")
            title = (title_elem.text or "").lower() if title_elem is not None else ""
            text = " ".join(" ".join(sec.itertext()).split())
            if not text: continue
            if any(k in title for k in keywords):
                priority.append(text)
            else:
                other.append(text)

        if not priority:
            combined = " ".join(other)
        elif sum(len(t) for t in priority) < 1500:
            combined = " ".join(priority + other)
        else:
            combined = " ".join(priority)
        combined = " ".join(combined.split())
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "... [truncated]"
        return combined or None
    except Exception:
        return None


# ---------- Summarization ----------

def summarize_paper(paper, retries=3):
    has_ft = bool(paper.get("full_text"))
    target_words = WORDS_FULL_TEXT if has_ft else WORDS_ABSTRACT_ONLY
    affiliation = paper["affiliation"] or "affiliation not given in the record"
    last_author = paper["last_author"] or paper["first_author"]
    first_author = paper["first_author"]

    if has_ft:
        content_block = (f"Abstract:\n{paper['abstract']}\n\n"
                         f"Selected full-text content (Results / Discussion / Conclusion):\n{paper['full_text']}")
        length_rule = (f"Aim for {target_words} words (acceptable range 400–500). "
                       "Since you have the full-text Results and Discussion, weight the segment toward "
                       "specific findings, effect magnitudes, and the authors' interpretation. Cover the "
                       "study design briefly; spend most of the segment on results and their meaning.")
    else:
        content_block = f"Abstract:\n{paper['abstract']}"
        length_rule = (f"Aim for {target_words} words. Use all information present in the abstract. "
                       "Do not pad with general statements to reach length.")

    prompt = f"""You are preparing a spoken audio segment about a research paper for an ophthalmologist subspecialised in retina and ocular oncology, listening while driving. The audience is at senior medical PhD level — they already know basic clinical context.

MUST INCLUDE, EVERY TIME, NEAR THE START:
- The paper's title (you may smooth its phrasing for spoken delivery, but keep the meaning intact).
- The first author: {first_author}.
- The last author: {last_author}.
- The first author's affiliation (use ONLY what is provided below; if "affiliation not given in the record", say so or omit — do NOT invent a city, country, or institution).
- The journal: {paper['journal']}.

REQUIRED CONTENT:
- Specific study aim or hypothesis.
- Design and population / cohort / sample (sizes, follow-up duration, multicentric vs single-centre, retrospective vs prospective, etc.).
- Key results with concrete numbers — effect sizes, anatomical success rates, visual acuity changes in logMAR or letters, AUCs, hazard ratios. Skip p-values and confidence intervals.
- Authors' stated interpretation.
- Limitations the authors note.

DO NOT INCLUDE:
- Generic background such as "AMD is a leading cause of blindness", "OCT is widely used in retinal imaging", "vitrectomy is a common procedure". The listener already knows.
- Evaluative or marketing language: "this innovative work", "this important study", "groundbreaking", "novel approach", "elegant", "promising". Just state findings.
- Encouraging editorialising or summarising sentences.

{length_rule}

STYLE:
- Scientific spoken English at medical PhD level. Direct, precise, no fluff.
- No bullet points, no markdown, no headings.
- Pronounce acronyms phonetically: OCT as "O C T", AMD as "A M D", anti-VEGF as "anti veg eff", IVT as "I V T", BCVA as "B C V A", RPE as "R P E", PVR as "P V R", ILM as "I L M", PPV as "P P V", AUC as "A U C", DR as "D R".
- Begin directly with the paper. No "moving on", no "next paper" — transitions are handled separately.

PAPER DATA:
Title: {paper['title']}
First author: {first_author}
Last author: {last_author}
First author affiliation: {affiliation}
Journal: {paper['journal']}

{content_block}
"""

    for attempt in range(retries):
        try:
            resp = groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(20 * (attempt + 1))
            else:
                return f"Could not generate summary: {type(e).__name__}."


# ---------- Audio with chapter markers ----------

async def _gen_audio(idx, title, text, voice, rate, semaphore):
    async with semaphore:
        data = b""
        comm = edge_tts.Communicate(text, voice=voice, rate=rate)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                data += chunk["data"]
        return idx, title, data


async def build_chaptered_mp3_async(segments, output_path, voice, rate, parallelism):
    sem = asyncio.Semaphore(parallelism)
    tasks = [_gen_audio(i, t, x, voice, rate, sem)
             for i, (t, x) in enumerate(segments)]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda r: r[0])

    durations_ms = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (_, _, data) in enumerate(results):
            p = os.path.join(tmp, f"s{i}.mp3")
            with open(p, "wb") as f: f.write(data)
            durations_ms.append(int(MP3(p).info.length * 1000))

    with open(output_path, "wb") as f:
        for _, _, data in results:
            f.write(data)

    chapters = []
    cumulative = 0
    for (_, title, _), dur in zip(results, durations_ms):
        chapters.append((title, cumulative, cumulative + dur))
        cumulative += dur

    audio = MP3(output_path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    chap_ids = []
    for i, (title, start, end) in enumerate(chapters):
        cid = f"ch{i:03d}"
        chap_ids.append(cid)
        audio.tags.add(CHAP(
            element_id=cid, start_time=start, end_time=end,
            start_offset=0xFFFFFFFF, end_offset=0xFFFFFFFF,
            sub_frames=[TIT2(encoding=3, text=[title])]
        ))
    audio.tags.add(CTOC(
        element_id="toc",
        flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
        child_element_ids=chap_ids,
        sub_frames=[TIT2(encoding=3, text=["Chapters"])]
    ))
    audio.save()
    return len(chapters)


def build_chaptered_mp3(segments, output_path):
    return asyncio.run(build_chaptered_mp3_async(
        segments, output_path, TTS_VOICE, TTS_RATE, TTS_PARALLELISM
    ))


def short_title(text, limit=70):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit-1] + "…"


# ---------- RSS feed ----------

def build_rss_feed(episodes_dir, output_path, base_url):
    items = []
    if os.path.isdir(episodes_dir):
        for fname in sorted(os.listdir(episodes_dir), reverse=True):
            if not fname.endswith(".mp3"): continue
            path = os.path.join(episodes_dir, fname)
            try:
                ep_date = datetime.strptime(fname[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            size = os.path.getsize(path)
            duration = int(MP3(path).info.length)
            ep_url = f"{base_url}/episodes/{fname}"
            pretty_date = ep_date.strftime("%B %d, %Y")
            items.append(f"""    <item>
      <title>Week of {pretty_date}</title>
      <description>Weekly PubMed update for {pretty_date}.</description>
      <pubDate>{format_datetime(ep_date)}</pubDate>
      <enclosure url="{ep_url}" length="{size}" type="audio/mpeg"/>
      <guid isPermaLink="true">{ep_url}</guid>
      <itunes:duration>{duration}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{html.escape(PODCAST_TITLE)}</title>
    <link>{base_url}</link>
    <atom:link href="{base_url}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{html.escape(PODCAST_DESCRIPTION)}</description>
    <language>en-us</language>
    <itunes:author>{html.escape(PODCAST_AUTHOR)}</itunes:author>
    <itunes:summary>{html.escape(PODCAST_DESCRIPTION)}</itunes:summary>
    <itunes:category text="Health &amp; Fitness"><itunes:category text="Medicine"/></itunes:category>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{base_url}/cover.jpg"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(rss)


def prune_old_episodes(episodes_dir, keep=MAX_EPISODES_TO_KEEP):
    if not os.path.isdir(episodes_dir): return
    files = sorted([f for f in os.listdir(episodes_dir) if f.endswith(".mp3")], reverse=True)
    for old in files[keep:]:
        os.remove(os.path.join(episodes_dir, old))
        print(f"  Pruned old episode: {old}")


# ---------- Email ----------

def build_html_email(all_papers, date_str, oa_count, total):
    css = """
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
             color: #222; line-height: 1.5; max-width: 720px; margin: 0 auto; padding: 1.5rem; }
      h1 { font-size: 1.4rem; }
      h2 { font-size: 1.15rem; border-bottom: 2px solid #ddd; padding-bottom: 0.3rem; margin-top: 2rem; }
      h3 { font-size: 1rem; margin: 1.2rem 0 0.3rem; }
      .meta { color: #666; font-size: 0.9rem; margin: 0.1rem 0 0.5rem; }
      .abstract { font-size: 0.95rem; }
      .tag { display: inline-block; background: #d1f0d4; color: #285c2e; padding: 1px 7px;
             border-radius: 4px; font-size: 0.75rem; vertical-align: middle; margin-left: 0.4rem; }
      a { color: #0366d6; text-decoration: none; }
    """
    parts = [f"<html><head><style>{css}</style></head><body>"]
    parts.append(f"<h1>PubMed Weekly Digest — {date_str}</h1>")
    parts.append(f"<p>{total} new papers across {len(TOPICS)} topics. "
                 f"Full text available for {oa_count}.</p>")

    for topic, papers in all_papers.items():
        parts.append(f"<h2>{html.escape(topic)} ({len(papers)})</h2>")
        if not papers:
            parts.append("<p><em>No new papers this week.</em></p>")
            continue
        for p in papers:
            oa_tag = ' <span class="tag">Open Access</span>' if p.get("full_text") else ""
            authors_short = p["first_author"]
            if p["last_author"] and p["last_author"] != p["first_author"]:
                authors_short += f", … {p['last_author']}"
            parts.append(
                f"<h3>{html.escape(p['title'])}{oa_tag}</h3>"
                f"<div class='meta'>{html.escape(authors_short)} — "
                f"<em>{html.escape(p['journal'])}</em> — "
                f"<a href='{p['url']}'>PubMed</a></div>"
                f"<div class='abstract'>{html.escape(p['abstract'])}</div>"
            )
    parts.append("</body></html>")
    return "".join(parts)


def send_email(html_body, subject, to_addr, gmail_user, gmail_pass):
    if not (gmail_user and gmail_pass and to_addr):
        print("  Email skipped (missing GMAIL_USER, GMAIL_APP_PASSWORD, or EMAIL_TO).")
        return
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = gmail_user
    msg['To'] = to_addr
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as server:
            server.login(gmail_user, gmail_pass)
            server.send_message(msg)
        print(f"  Email sent to {to_addr}")
    except Exception as e:
        print(f"  Email failed: {type(e).__name__}: {e}")


# ===================================================================
# MAIN
# ===================================================================

os.makedirs(EPISODES_DIR, exist_ok=True)
date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
print(f"PubMed Weekly Digest — {date_str}\n" + "=" * 60)
if NCBI_API_KEY:
    print("Using NCBI API key (10 req/sec limit).")
else:
    print("No NCBI_API_KEY set. Throttling to 2.5 req/sec to stay safe.")

print("\n[1/5] Searching PubMed...")
all_papers = {}
seen_pmids = set()       # tracks PMIDs already assigned to an earlier topic
for topic, query in TOPICS.items():
    pmids = search_pubmed(query, DAYS_BACK, MAX_PAPERS_PER_TOPIC)
    unique = [p for p in pmids if p not in seen_pmids]
    dups = len(pmids) - len(unique)
    seen_pmids.update(unique)
    all_papers[topic] = fetch_abstracts(unique)
    dup_note = f"  ({dups} duplicate{'s' if dups != 1 else ''} skipped)" if dups else ""
    print(f"  {topic}: {len(all_papers[topic])} papers{dup_note}")
total = sum(len(p) for p in all_papers.values())

print("\n[2/5] Fetching open-access full text from PMC...")
oa = 0
for papers in all_papers.values():
    for p in papers:
        p["full_text"] = fetch_pmc_full_text(p["pmcid"]) if p["pmcid"] else None
        if p["full_text"]: oa += 1
print(f"  Full text obtained for {oa}/{total}")

print("\n[3/5] Sending email digest (titles + abstracts)...")
email_html = build_html_email(all_papers, date_str, oa, total)
send_email(
    email_html,
    f"PubMed Weekly Digest — {date_str} ({total} papers)",
    EMAIL_TO, GMAIL_USER, GMAIL_APP_PASSWORD
)

print(f"\n[4/5] Generating summaries with {GROQ_MODEL}...")
for topic, papers in all_papers.items():
    for i, p in enumerate(papers, 1):
        flag = "📖" if p["full_text"] else "📄"
        print(f"  {flag} [{topic[:35]}] {i}/{len(papers)}: {p['title'][:50]}")
        p["summary"] = summarize_paper(p)
        time.sleep(SECONDS_BETWEEN_CALLS)

print("\n[5/5] Building chaptered audio + RSS feed...")
segments = [(
    "Welcome",
    f"Welcome to your PubMed update for {datetime.now().strftime('%B %d, %Y')}. "
    f"{total} new papers across {len(TOPICS)} areas, with full text for {oa}. "
    f"Each paper is a separate chapter — skip ahead using your next-chapter control."
)]
for topic, papers in all_papers.items():
    if not papers:
        segments.append((f"{topic} (none)",
                         f"No new papers this week in {topic}."))
        continue
    segments.append((f"Topic: {topic}",
                     f"Now turning to {topic}. {len(papers)} papers."))
    for p in papers:
        segments.append((short_title(p["title"]), p["summary"]))
segments.append((
    "Wrap-up",
    "That concludes this week's update. Drive safely."
))

audio_path = os.path.join(EPISODES_DIR, f"{date_str}.mp3")
n_chapters = build_chaptered_mp3(segments, audio_path)
print(f"  Wrote {audio_path}")
print(f"  Embedded {n_chapters} chapter markers")

prune_old_episodes(EPISODES_DIR)
build_rss_feed(EPISODES_DIR, RSS_PATH, PODCAST_BASE_URL)
print(f"  Wrote {RSS_PATH}")

print(f"\n✓ Done. {n_chapters} chapters, ~{total} papers covered.")
