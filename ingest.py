"""
Ingest documents from knowledge_base/ into the vector store and
extract profile information to populate profile.yaml.

Usage:
    python ingest.py                  # ingest + extract profile
    python ingest.py --reset-profile  # re-extract profile even if profile.yaml exists
"""

import sys
import json
import hashlib
from pathlib import Path

import yaml
import anthropic
import chromadb
import pdfplumber
from docx import Document as DocxDocument
from dotenv import load_dotenv

load_dotenv()

KB_DIR = Path("knowledge_base")
PROFILE_PATH = Path("profile.yaml")
VECTOR_DIR = Path("vector_store")

CHUNK_SIZE = 800   # words per chunk
CHUNK_OVERLAP = 80  # words of overlap between chunks

client = anthropic.Anthropic()
chroma = chromadb.PersistentClient(path=str(VECTOR_DIR))
collection = chroma.get_or_create_collection("knowledge_base")


# ---------------------------------------------------------------------------
# Document parsing
# ---------------------------------------------------------------------------

def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    elif suffix in (".docx", ".doc"):
        doc = DocxDocument(path)
        return "\n".join(p.text for p in doc.paragraphs)
    elif suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="ignore")
    else:
        print(f"  Skipping unsupported file type: {path.name}")
        return ""


def chunk_text(text: str, source: str) -> list[dict]:
    words = text.split()
    chunks = []
    i = 0
    chunk_num = 0
    while i < len(words):
        chunk = " ".join(words[i : i + CHUNK_SIZE])
        chunk_id = hashlib.md5(f"{source}:{chunk_num}".encode()).hexdigest()
        chunks.append({
            "id": chunk_id,
            "text": chunk,
            "metadata": {"source": source, "chunk": chunk_num},
        })
        chunk_num += 1
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def ingest_documents() -> list[tuple[str, str]]:
    """Read all files from knowledge_base/, chunk and index them. Returns (name, text) pairs."""
    KB_DIR.mkdir(exist_ok=True)
    doc_files = [f for f in KB_DIR.glob("**/*") if f.is_file() and not f.name.startswith(".")]

    if not doc_files:
        print("No documents found in knowledge_base/. Add PDFs, .docx, or .txt files.")
        return []

    all_texts = []
    for path in sorted(doc_files):
        print(f"  Processing {path.name}...")
        text = extract_text(path)
        if not text.strip():
            print(f"    (no text extracted, skipping)")
            continue

        all_texts.append((path.name, text))
        chunks = chunk_text(text, path.name)

        # Only add chunks not already in the store
        existing = set(collection.get(ids=[c["id"] for c in chunks])["ids"])
        new_chunks = [c for c in chunks if c["id"] not in existing]
        if new_chunks:
            collection.add(
                documents=[c["text"] for c in new_chunks],
                ids=[c["id"] for c in new_chunks],
                metadatas=[c["metadata"] for c in new_chunks],
            )
            print(f"    Indexed {len(new_chunks)} new chunks ({len(existing)} already stored)")
        else:
            print(f"    Already fully indexed, skipping")

    return all_texts


# ---------------------------------------------------------------------------
# Profile extraction
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "child": {
        "first_name": None,
        "last_name": None,
        "preferred_name": None,
        "date_of_birth": None,          # MM/DD/YYYY
        "gender": None,
        "diagnoses": [],                # e.g. ["Autism Spectrum Disorder", "ADHD"]
        "medications": [],              # e.g. ["Methylphenidate 5mg"]
        "allergies": [],
        "school": None,
        "grade": None,
        "teacher": None,
    },
    "parent_guardian_1": {
        "first_name": None,
        "last_name": None,
        "relationship": None,           # e.g. "Mother", "Father", "Guardian"
        "phone_cell": None,
        "phone_work": None,
        "email": None,
        "employer": None,
        "address": {
            "street": None,
            "city": None,
            "state": None,
            "zip": None,
        },
    },
    "parent_guardian_2": {
        "first_name": None,
        "last_name": None,
        "relationship": None,
        "phone_cell": None,
        "phone_work": None,
        "email": None,
        "employer": None,
    },
    "insurance": {
        "primary": {
            "carrier": None,
            "member_id": None,
            "group_number": None,
            "subscriber_name": None,
        },
        "secondary": {
            "carrier": None,
            "member_id": None,
            "group_number": None,
            "subscriber_name": None,
        },
    },
    "providers": {
        "pediatrician": {"name": None, "phone": None, "practice": None},
        "other": [],                    # list of {role, name, phone, practice}
    },
    "emergency_contacts": [],           # list of {name, relationship, phone}
}


def deep_merge(base: dict, updates: dict) -> dict:
    """Merge updates into base. Existing non-None values in base are preserved."""
    result = base.copy()
    for key, value in updates.items():
        if key not in result:
            result[key] = value
        elif result[key] is None and value is not None:
            result[key] = value
        elif isinstance(result[key], list) and isinstance(value, list) and len(result[key]) == 0:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
    return result


def extract_profile(all_texts: list[tuple[str, str]], reset: bool = False):
    """Use Claude to extract profile fields from documents and write profile.yaml."""
    if not all_texts:
        return

    existing_profile: dict = {}
    if PROFILE_PATH.exists() and not reset:
        with open(PROFILE_PATH) as f:
            existing_profile = yaml.safe_load(f) or {}
        print(f"\nMerging with existing {PROFILE_PATH} (existing values take precedence).")
        print("Run with --reset-profile to re-extract everything from scratch.")

    # Feed up to ~60k chars of document text to Claude
    combined = ""
    for name, text in all_texts:
        combined += f"\n\n=== {name} ===\n{text}"
    combined = combined[:60000]

    print("\nExtracting profile information from documents...")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": (
                "Extract biographical and medical information from the following documents "
                "to populate a profile for a special needs child and their family.\n\n"
                "Return ONLY valid JSON matching this exact schema. Use null for any field "
                "not found in the documents. For list fields, return an array (empty [] if nothing found).\n\n"
                f"Schema:\n{json.dumps(PROFILE_SCHEMA, indent=2)}\n\n"
                f"Documents:\n{combined}\n\n"
                "Return only the JSON object with no explanation or markdown fences."
            ),
        }],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:-1])

    extracted: dict = json.loads(raw)

    # Priority: schema defaults → extracted values → existing profile (wins)
    merged = deep_merge(PROFILE_SCHEMA, extracted)
    merged = deep_merge(merged, existing_profile)

    with open(PROFILE_PATH, "w") as f:
        yaml.dump(merged, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"\nProfile written to {PROFILE_PATH}")
    print("Open it, review for accuracy, and fill in anything marked null before filling forms.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    reset_profile = "--reset-profile" in sys.argv

    print("=== Ingesting documents ===")
    all_texts = ingest_documents()

    if all_texts:
        print("\n=== Extracting profile ===")
        extract_profile(all_texts, reset=reset_profile)
    else:
        print("\nNo documents to process.")

    print("\nDone. Run `python fill_form.py <url>` to fill out a form.")
