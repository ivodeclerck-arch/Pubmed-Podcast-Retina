"""
PubMed Weekly Digest — v4
==========================
- Generates spoken summaries with chapter markers (jumpable in podcast apps)
- Builds an RSS feed for Apple Podcasts / Overcast / any podcast app
- Runs unattended on GitHub Actions
- Reads secrets from environment variables

For Colab testing: set GROQ_API_KEY at the top.
For GitHub Actions: GROQ_API_KEY is loaded from secrets automatically.
"""

import os
import time
import asyncio
import tempfile
import re
import html
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

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

# Pulled from environment in GitHub Actions; falls back for Colab testing.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "PASTE_YOUR_KEY_HERE")

# Where to write output. In GitHub Actions, this is set to "docs" (the
# folder served by GitHub Pages). In Colab, defaults to current dir.
OUTPUT_DIR    = os.environ.get("OUTPUT_DIR", ".")
EPISODES_DIR  = os.path.join(OUTPUT_DIR, "episodes")
RSS_PATH      = os.path.join(OUTPUT_DIR, "feed.xml")

# The public URL where these files will be hosted.
# For GitHub Pages: https://<username>.github.io/<repo>
# Set this via the PODCAST_BASE_URL env var, or edit here.
PODCAST_BASE_URL = os.environ.get(
    "PODCAST_BASE_URL",
    "https://YOUR_USERNAME.github.io/YOUR_REPO"
)

# Podcast metadata — shown in Apple Podcasts
PODCAST_TITLE       = "PubMed Weekly Digest"
PODCAST_DESCRIPTION = "Weekly research updates in ocular oncology, vitreoretinal surgery, robotic eye surgery, and AI in retinal disease."
PODCAST_AUTHOR      = "Your Name"

# Keep only the most recent N episodes (older files get deleted to stay under repo size limits)
MAX_EPISODES_TO_KEEP = 12

# --- Search topics ---
TOPICS = {
    "Ocular Oncology":
        '("uveal melanoma"[MeSH] OR "retinoblastoma"[MeSH] '
        'OR "ocular oncology"[tiab] OR "choroidal melanoma"[tiab] '
        'OR "conjunctival melanoma"[tiab])',

    "Vitreoretinal Surgery":
        '("vitreoretinal surgery"[tiab] OR "vitrectomy"[MeSH] '
        'OR "retinal detachment"[MeSH] OR "macular hole"[MeSH] '
        'OR "epiretinal membrane"[MeSH])',

    "Robotic Eye Surgery":
        '("robotic"[tiab] OR "robot-assisted"[tiab] OR "robotic surgery"[MeSH]) '
        'AND ("eye"[tiab] OR "ophthalmic"[tiab] OR "retinal"[tiab] '
        'OR "intraocular"[tiab] OR "vitreoretinal"[tiab])',

    "AI in Retinal Disease":
        '("artificial intelligence"[tiab] OR "deep learning"[tiab] '
        'OR "machine learning"[tiab] OR "neural network"[tiab]) '
        'AND ("retina"[MeSH] OR "diabetic retinopathy"[MeSH] '
        'OR "macular degeneration"[MeSH] OR "OCT"[tiab] '
        'OR "fundus"[tiab])',
}

DAYS_BACK            = 7
MAX_PAPERS_PER_TOPIC = 20
GROQ_MODEL           = "meta-llama/llama-4-scout-17b-16e-instruct"
SECONDS_BETWEEN_CALLS = 2
WORDS_PER_PAPER      = 300
TTS_VOICE            = "en-US-AndrewNeural"
TTS_RATE             = "+0%"
TTS_PARALLELISM      = 10

# ===================================================================
# CODE
# ===================================================================

groq = Groq(api_key=GROQ_API_KEY)
NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


# ---------- PubMed ----------

def search_pubmed(query, days_back, max_results):
    params = {"db": "pubmed", "term": query, "retmax": max_results,
              "retmode": "json", "reldate": days_back,
              "datetype": "pdat", "sort": "date"}
    r = requests.get(f"{NCBI_BASE}/esearch.fcgi", params=params, timeout=30)
    r.raise_for_status()
    return r.json()["esearchresult"].get("idlist", [])


def fetch_abstracts(pmids):
    if not pmids: return []
    params = {"db": "pubmed", "id": ",".join(pmids),
              "rettype": "abstract", "retmode": "xml"}
    r = requests.get(f"{NCBI_BASE}/efetch.fcgi", params=params, timeout=60)
    r.raise_for_status()
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

        authors, first_affiliation = [], ""
        for i, a in enumerate(article.findall(".//AuthorList/Author")):
            last = a.findtext("LastName") or ""
            initials = a.findtext("Initials") or ""
            if last:
                authors.append(f"{last} {initials}".strip())
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
            "affiliation": first_affiliation, "pmcid": pmcid,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        })
    return papers


def fetch_pmc_full_text(pmcid, max_chars=10000):
    if not pmcid: return None
    try:
        params = {"db": "pmc", "id": pmcid.replace("PMC", ""),
                  "rettype": "xml"}
        r = requests.get(f"{NCBI_BASE}/efetch.fcgi", params=params, timeout=60)
        r.raise_for_status()
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

def summarize_paper(paper, target_words=WORDS_PER_PAPER, retries=3):
    affiliation = paper["affiliation"] or "not specified in the metadata"
    has_ft = bool(paper.get("full_text"))
    base = (f"Title: {paper['title']}\n"
            f"First author: {paper['first_author']}\n"
            f"Author affiliation: {affiliation}\n"
            f"Journal: {paper['journal']}\n")
    if has_ft:
        content = (f"Abstract:\n{paper['abstract']}\n\n"
                   f"Selected full-text (Results / Discussion / Conclusion):\n{paper['full_text']}")
        depth = ("You have portions of the full article. Go beyond the abstract: "
                 "discuss specific findings, effect sizes, and what the authors "
                 "note as limitations and clinical implications.")
    else:
        content = f"Abstract:\n{paper['abstract']}"
        depth = "Only the abstract is available, so work from that."

    prompt = f"""You are preparing a spoken audio segment about a research paper for an ophthalmologist listening while driving. Target length: about {target_words} words.

{base}
{content}

Cover these points in flowing spoken English:
- Where the work comes from (use ONLY the affiliation given; if "not specified", say "in a recent study" — do NOT invent a city or country)
- The clinical question or motivation
- The study design and population, or experimental setup
- Key findings, with one or two specific numbers if striking
- Clinical implications and limitations the authors note

{depth}

Style:
- Conversational spoken English. No bullets, no markdown, no headings.
- Acronyms phonetic: OCT as "O C T", AMD as "A M D", anti-VEGF as "anti veg eff", IVT as "I V T", BCVA as "B C V A", RPE as "R P E".
- Vary openings; don't start every paper with "Researchers from...".
- Do NOT say "moving on" or "next paper" — transitions are handled separately.
- Begin directly with content. No preamble.
"""
    for attempt in range(retries):
        try:
            resp = groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(20 * (attempt + 1))
            else:
                return f"Could not generate summary: {type(e).__name__}."


# ---------- Audio generation (parallel + chapter markers) ----------

async def _gen_audio(idx, title, text, voice, rate, semaphore):
    async with semaphore:
        data = b""
        comm = edge_tts.Communicate(text, voice=voice, rate=rate)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                data += chunk["data"]
        return idx, title, data


async def build_chaptered_mp3_async(segments, output_path, voice, rate, parallelism):
    """segments: list of (chapter_title, text). Generates audio in parallel,
    concatenates, and embeds ID3 chapter markers."""
    sem = asyncio.Semaphore(parallelism)
    tasks = [_gen_audio(i, t, x, voice, rate, sem)
             for i, (t, x) in enumerate(segments)]
    results = await asyncio.gather(*tasks)
    results.sort(key=lambda r: r[0])

    # Measure each segment's duration via a temp file
    durations_ms = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (_, _, data) in enumerate(results):
            p = os.path.join(tmp, f"s{i}.mp3")
            with open(p, "wb") as f: f.write(data)
            durations_ms.append(int(MP3(p).info.length * 1000))

    # Concatenate audio bytes
    with open(output_path, "wb") as f:
        for _, _, data in results:
            f.write(data)

    # Build chapter timing
    chapters = []
    cumulative = 0
    for (_, title, _), dur in zip(results, durations_ms):
        chapters.append((title, cumulative, cumulative + dur))
        cumulative += dur

    # Embed ID3 chapter tags
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


def build_chaptered_mp3(segments, output_path):
    asyncio.run(build_chaptered_mp3_async(
        segments, output_path, TTS_VOICE, TTS_RATE, TTS_PARALLELISM
    ))


def short_title(text, limit=70):
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit-1] + "…"


# ---------- RSS feed ----------

def build_rss_feed(episodes_dir, output_path, base_url):
    """Scan episodes/ for MP3s and produce a podcast-app-ready RSS feed."""
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
    <itunes:category text="Health &amp; Fitness">
      <itunes:category text="Medicine"/>
    </itunes:category>
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


# ===================================================================
# MAIN
# ===================================================================

os.makedirs(EPISODES_DIR, exist_ok=True)
date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
print(f"PubMed Weekly Digest — {date_str}")
print("=" * 60)

# 1. Search
print("\n[1/4] Searching PubMed...")
all_papers = {}
for topic, query in TOPICS.items():
    pmids = search_pubmed(query, DAYS_BACK, MAX_PAPERS_PER_TOPIC)
    all_papers[topic] = fetch_abstracts(pmids)
    print(f"  {topic}: {len(all_papers[topic])} papers")
    time.sleep(0.5)
total = sum(len(p) for p in all_papers.values())

# 2. Full text
print("\n[2/4] Fetching open-access full text from PMC...")
oa = 0
for papers in all_papers.values():
    for p in papers:
        p["full_text"] = fetch_pmc_full_text(p["pmcid"]) if p["pmcid"] else None
        if p["full_text"]: oa += 1
        time.sleep(0.3)
print(f"  Full text obtained for {oa}/{total}")

# 3. Summarize
print(f"\n[3/4] Generating summaries with {GROQ_MODEL}...")
for topic, papers in all_papers.items():
    for i, p in enumerate(papers, 1):
        flag = "📖" if p["full_text"] else "📄"
        print(f"  {flag} [{topic}] {i}/{len(papers)}: {p['title'][:55]}")
        p["summary"] = summarize_paper(p)
        time.sleep(SECONDS_BETWEEN_CALLS)

# 4. Build segments with chapter titles
print("\n[4/4] Building chaptered audio and RSS feed...")
segments = [(
    "Welcome",
    f"Welcome to your PubMed update for {datetime.now().strftime('%B %d, %Y')}. "
    f"This week, {total} new papers across {len(TOPICS)} areas. "
    f"Full text was available for {oa} of them, allowing deeper coverage. "
    f"Each paper is its own chapter — skip ahead if anything isn't relevant."
)]

for topic, papers in all_papers.items():
    if not papers:
        segments.append((f"Topic: {topic} (no papers)",
                         f"No new papers this week in {topic}."))
        continue
    segments.append((f"Topic: {topic}",
                     f"Now turning to {topic}. {len(papers)} papers this week."))
    for i, p in enumerate(papers):
        chapter_title = short_title(p["title"])
        # Add a tiny lead-in so chapter doesn't start mid-sentence
        text = p["summary"]
        segments.append((chapter_title, text))

segments.append((
    "Wrap-up",
    "That concludes this week's update. Drive safely, and see you next Monday."
))

# Generate the MP3 (parallel + chapters)
audio_path = os.path.join(EPISODES_DIR, f"{date_str}.mp3")
build_chaptered_mp3(segments, audio_path)
print(f"  Wrote {audio_path}")

# Prune old episodes
prune_old_episodes(EPISODES_DIR)

# Build the RSS feed
build_rss_feed(EPISODES_DIR, RSS_PATH, PODCAST_BASE_URL)
print(f"  Wrote {RSS_PATH}")

# Save a markdown digest too (for reference)
digest_path = os.path.join(OUTPUT_DIR, f"digest_{date_str}.md")
with open(digest_path, "w", encoding="utf-8") as f:
    f.write(f"# PubMed Weekly Digest — {date_str}\n\n")
    f.write(f"_{total} papers, {oa} with full text._\n\n")
    for topic, papers in all_papers.items():
        f.write(f"\n## {topic}  ({len(papers)})\n\n")
        for p in papers:
            f.write(f"### {p['title']}\n")
            f.write(f"_{p['first_author']} et al. — {p['journal']}_  \n")
            f.write(f"[PubMed]({p['url']})\n\n")
            f.write(f"**Spoken:** {p['summary']}\n\n")
            f.write(f"**Abstract:** {p['abstract']}\n\n---\n\n")
print(f"  Wrote {digest_path}")

print(f"\n✓ Done. {len(segments)} chapters. Listen on the road.")
