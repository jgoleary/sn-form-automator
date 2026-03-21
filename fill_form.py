"""
Fill a web form using the knowledge base and profile.yaml.

Usage:
    python fill_form.py <url>

Flow:
    1. Open browser and navigate to the URL
    2. Pause so you can log in manually
    3. Extract all form fields from the page
    4. Use Claude + the knowledge base to generate answers
    5. Print the fill plan for your review
    6. Fill the fields in the browser
    7. You review / edit / submit manually
"""

import sys
import json
import asyncio
from pathlib import Path

import yaml
import anthropic
import chromadb
from dotenv import load_dotenv

load_dotenv()

PROFILE_PATH = Path("profile.yaml")
VECTOR_DIR = Path("vector_store")

client = anthropic.Anthropic()
chroma = chromadb.PersistentClient(path=str(VECTOR_DIR))
collection = chroma.get_or_create_collection("knowledge_base")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_profile() -> dict:
    if not PROFILE_PATH.exists():
        print("profile.yaml not found. Run `python ingest.py` first.")
        sys.exit(1)
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f) or {}


def query_kb(question: str, n: int = 5) -> str:
    try:
        results = collection.query(query_texts=[question], n_results=n)
        docs = results["documents"][0] if results["documents"] else []
        return "\n\n".join(docs)
    except Exception:
        return ""


def is_essay_field(field: dict) -> bool:
    """Treat textareas and Quill rich-text editors as essay fields."""
    return field["type"] in ("textarea", "quill")


def generate_all_answers(fields: list[dict], profile: dict, kb_context: str) -> dict[str, str]:
    """
    Answer every form field — short and essay — in a single API call.
    Returns a dict mapping field label → answer string.
    """
    field_specs = []
    for f in fields:
        spec = {"label": f["label"], "type": f["type"]}
        if f["options"]:
            readable = [o["text"] for o in f["options"] if o.get("text") and o["text"] not in ("--", "Select an option...")]
            if readable:
                spec["options"] = readable
        if is_essay_field(f):
            spec["essay"] = True
        field_specs.append(spec)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": (
                "You are filling out an intake form for a special needs child, written from the parent's perspective.\n\n"
                "Answer EVERY field below. Return a single JSON object where each key is the exact field label "
                "and the value is the answer string. No explanation, no markdown fences.\n\n"
                "Rules:\n"
                "- Short fields: return a concise value only\n"
                "- Essay fields (marked essay:true): write 2–5 sentences in first person as the parent, "
                "drawing on specific details from the profile and documents. Flowing prose, no bullet points.\n"
                "- For yes/no fields: return 'yes' or 'no'\n"
                "- For date fields: use MM/DD/YYYY\n"
                "- For phone fields: use (XXX) XXX-XXXX\n"
                "- For select/checkbox fields with options: return one of the listed options exactly as written\n"
                "- Do not fabricate details not found in the profile or documents\n"
                "- If you cannot determine a confident answer: use NEEDS_REVIEW\n\n"
                f"Profile:\n{yaml.dump(profile, default_flow_style=False)}\n\n"
                f"Relevant document excerpts:\n{kb_context}\n\n"
                f"Fields:\n{json.dumps(field_specs, indent=2)}\n\n"
                "JSON:"
            ),
        }],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:-1])
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Browser automation
# ---------------------------------------------------------------------------

FIELD_EXTRACTOR_JS = """
() => {
    const fields = [];
    const seen = new Set();

    // --- Pass 1: standard input / select / textarea elements ---
    const inputs = document.querySelectorAll('input, select, textarea');
    for (const el of inputs) {
        if (['hidden', 'submit', 'button', 'reset', 'image'].includes(el.type)) continue;
        if (el.offsetParent === null) continue;
        // Skip inputs inside .chart-edit fieldsets — Pass 2 handles those as a group
        if (el.closest('.chart-edit fieldset')) continue;

        // Resolve label first so we can use it as the dedup key.
        // This handles JaneApp reusing generic names like "selected_option" across many selects.
        let label = '';
        if (el.id) {
            const lel = document.querySelector(`label[for="${el.id}"]`);
            if (lel) label = lel.innerText.trim();
        }
        if (!label) {
            const wrapped = el.closest('label');
            if (wrapped) label = wrapped.innerText.replace(el.value || '', '').trim();
        }
        // For selects inside .chart-edit, prefer the h5/legend question text as the label
        if (!label || label.includes('Select an option')) {
            const block = el.closest('.chart-edit');
            if (block) {
                const h5 = block.querySelector('h5');
                if (h5) label = h5.innerText.trim();
            }
        }
        if (!label && el.getAttribute('aria-label')) label = el.getAttribute('aria-label');
        if (!label && el.placeholder) label = el.placeholder;
        if (!label && el.name) label = el.name.replace(/[_\\-]/g, ' ');
        if (!label) {
            const parent = el.closest('div, td, li, p, fieldset');
            if (parent) label = parent.innerText.split('\\n')[0].trim().slice(0, 120);
        }

        // Deduplicate by label (preferred) or id/name fallback
        const dedupKey = label || el.id || el.name;
        if (dedupKey && seen.has(dedupKey)) continue;
        if (dedupKey) seen.add(dedupKey);

        let options = [];
        if (el.tagName === 'SELECT') {
            options = Array.from(el.options).map(o => ({ value: o.value, text: o.text.trim() }));
        }

        // Normalize select type; browsers return "select-one" / "select-multiple"
        const type = el.tagName === 'SELECT' ? 'select' : (el.type || el.tagName.toLowerCase());

        // For selects that share a generic name (e.g. "selected_option"), build the
        // selector from the question label via evaluate — store null and handle in fill loop.
        let selector = null;
        const sharedName = el.tagName === 'SELECT' && el.name &&
                           document.querySelectorAll(`select[name="${el.name}"]`).length > 1;
        if (el.id) selector = `#${CSS.escape(el.id)}`;
        else if (el.name && !sharedName) selector = `[name="${el.name}"]`;
        // else selector stays null → filled via evaluate() by question text

        fields.push({
            label,
            type,
            name: el.name,
            id: el.id,
            selector,
            options,
            required: el.required,
            current_value: el.value || '',
        });
    }

    // --- Pass 2: JaneApp checkbox/radio groups inside .chart-edit fieldsets ---
    // These use visually-hidden inputs that all share name="option", so standard
    // input detection misses the question and collapses all options into one entry.
    const fieldsets = document.querySelectorAll('.chart-edit fieldset');
    for (const fs of fieldsets) {
        if (fs.offsetParent === null) continue;

        // Question is in the <legend> (screen-reader text) or parent <h5>
        let label = '';
        const legend = fs.querySelector('legend');
        if (legend) label = legend.innerText.trim();
        if (!label) {
            const block = fs.closest('.chart-edit');
            if (block) {
                const h5 = block.querySelector('h5');
                if (h5) label = h5.innerText.trim();
            }
        }
        if (!label || seen.has(label)) continue;
        seen.add(label);

        // Collect options and which are currently checked
        const checkboxes = Array.from(fs.querySelectorAll('input[type="checkbox"]'));
        const options = checkboxes.map(cb => ({ value: cb.value, text: cb.value }));
        const checked = checkboxes.filter(cb => cb.getAttribute('aria-checked') === 'true' || cb.checked)
                                   .map(cb => cb.value);
        const current_value = checked.join(', ');

        fields.push({
            label,
            type: 'janeapp-checkbox-group',
            name: 'option',
            id: '',
            selector: null,          // filled via evaluate(), not a CSS selector
            options,
            required: false,
            current_value,
        });
    }

    // --- Pass 3: Quill rich-text editors (JaneApp uses these for open-ended questions) ---
    const editors = document.querySelectorAll('.ql-editor[contenteditable="true"]');
    for (const el of editors) {
        if (el.offsetParent === null) continue;

        // Label comes from aria-label (set by JaneApp to the question text)
        // or from the nearest h5 inside the parent .chart-edit block
        let label = el.getAttribute('aria-label') || '';
        if (!label) {
            const block = el.closest('.chart-edit');
            if (block) {
                const h5 = block.querySelector('h5');
                if (h5) label = h5.innerText.trim();
            }
        }
        if (!label || seen.has(label)) continue;
        seen.add(label);

        // Current content: empty Quill editors contain only whitespace / a lone <br>
        const rawText = el.innerText.replace(/\\n/g, '').trim();
        const current_value = (rawText === '' || rawText === '\\u200B') ? '' : el.innerText.trim();

        // Selector: match on aria-label so it survives React re-renders
        const escaped = label.replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
        const selector = `.ql-editor[aria-label="${escaped}"]`;

        fields.push({
            label,
            type: 'quill',
            name: '',
            id: '',
            selector,
            options: [],
            required: false,
            current_value,
        });
    }

    return fields;
}
"""


async def run(url: str, sample: int = 0):
    from playwright.async_api import async_playwright

    profile = load_profile()

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)

        print(f"\nOpening: {url}")
        await page.goto(url, wait_until="commit")

        print("\nIf the site requires login, do it now in the browser window.")
        input("Press Enter when you are on the form page and ready to continue... ")

        print("\nAnalyzing form fields...")
        # Wait for Quill editors to render (JaneApp is a React SPA)
        try:
            await page.wait_for_selector('.ql-editor', timeout=8000)
        except Exception:
            pass  # No Quill editors on this form; continue with standard fields
        fields: list[dict] = await page.evaluate(FIELD_EXTRACTOR_JS)

        if not fields:
            print("No fillable fields detected on this page.")
            await browser.close()
            return

        if sample:
            # Pick a representative sample: try to include each field type
            import random
            by_type: dict[str, list] = {}
            for f in fields:
                by_type.setdefault(f["type"], []).append(f)
            sampled = []
            # Round-robin across types until we hit the sample size
            buckets = list(by_type.values())
            for bucket in buckets:
                random.shuffle(bucket)
            i = 0
            while len(sampled) < sample and any(buckets):
                bucket = buckets[i % len(buckets)]
                if bucket:
                    sampled.append(bucket.pop())
                i += 1
            fields = sampled
            print(f"-- SAMPLE MODE: testing {len(fields)} of {len(by_type)} field types (no browser filling) --\n")

        print(f"Found {len(fields)} field(s). Generating answers (this may take a moment)...\n")

        fill_plan: list[dict] = []
        pending = []  # fields that need answers

        for field in fields:
            essay = is_essay_field(field)
            if field.get("current_value", "").strip():
                fill_plan.append({**field, "answer": None, "needs_review": False, "essay": essay, "prefilled": True})
            else:
                pending.append(field)

        # --- Single API call for all fields ---
        if pending:
            # Use essay questions as the KB query — they benefit most from document context
            essay_labels = [f["label"] for f in pending if is_essay_field(f)]
            kb_query = " ".join(essay_labels) if essay_labels else "child development therapy school"
            kb_context = query_kb(kb_query, n=20)

            print(f"Answering {len(pending)} fields in one API call...", flush=True)
            answers = generate_all_answers(pending, profile, kb_context)
            print(f"Done.", flush=True)

            for field in pending:
                essay = is_essay_field(field)
                answer = answers.get(field["label"], "NEEDS_REVIEW")
                fill_plan.append({**field, "answer": answer, "needs_review": answer == "NEEDS_REVIEW", "essay": essay, "prefilled": False})

        # --- Preview: short fields as a table, essays printed in full ---
        short_fields = [i for i in fill_plan if not i["essay"]]
        essay_fields = [i for i in fill_plan if i["essay"]]

        if short_fields:
            print("=" * 70)
            print(f"{'FIELD':<35} {'ANSWER':<30} NOTE")
            print("=" * 70)
            for item in short_fields:
                label = (item["label"] or item["name"] or "")[:34]
                if item.get("prefilled"):
                    answer = item["current_value"][:29]
                    note = "(already filled, skipping)"
                elif item["needs_review"]:
                    answer = ""
                    note = "*** REVIEW ***"
                else:
                    answer = item["answer"][:29]
                    note = ""
                print(f"{label:<35} {answer:<30} {note}")
            print("=" * 70)

        if essay_fields:
            print("\n--- Open-ended questions ---")
            for item in essay_fields:
                print(f"\nQ: {item['label'] or item['name']}")
                if item.get("prefilled"):
                    print(f"A: (already filled, skipping) {item['current_value'][:80]}...")
                elif item["needs_review"]:
                    print("A: *** NEEDS_REVIEW — will be skipped, fill manually ***")
                else:
                    print(f"A: {item['answer']}")
                print()

        flagged = sum(1 for i in fill_plan if i["needs_review"])
        if flagged:
            print(f"{flagged} field(s) marked NEEDS_REVIEW will be skipped — fill them manually.")

        if sample:
            print("\n-- SAMPLE MODE: review complete. Re-run without --sample to fill the real form. --")
            await browser.close()
            return

        confirm = input("\nFill the form with these answers? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            await browser.close()
            return

        n_to_fill = sum(1 for i in fill_plan if not i.get("prefilled") and not i["needs_review"] and i["answer"])
        print(f"\nFilling {n_to_fill} fields...")
        filled = 0
        for item in fill_plan:
            if item.get("prefilled") or item["needs_review"] or not item["answer"]:
                continue

            try:
                ftype = item["type"]
                locator = page.locator(item["selector"]).first if item.get("selector") else None

                if ftype == "janeapp-checkbox-group":
                    # Click the label whose input value matches the answer.
                    # Scoped by question text so same-named inputs in other fieldsets aren't touched.
                    clicked = await page.evaluate(
                        """([question, answer]) => {
                            for (const fs of document.querySelectorAll('.chart-edit fieldset')) {
                                const legend = fs.querySelector('legend');
                                if (!legend || legend.innerText.trim() !== question) continue;
                                for (const cb of fs.querySelectorAll('input[type="checkbox"]')) {
                                    if (cb.value.toLowerCase() === answer.toLowerCase()) {
                                        cb.closest('label').click();
                                        return true;
                                    }
                                }
                            }
                            return false;
                        }""",
                        [item["label"], item["answer"]],
                    )
                    if not clicked:
                        print(f"  Could not match option '{item['answer']}' for: {item['label']}")

                elif ftype == "quill":
                    # Playwright's fill() handles contenteditable natively and cross-platform
                    await locator.fill(item["answer"])

                elif ftype == "select":
                    opts = item["options"]
                    match = next((o for o in opts if o["text"].lower() == item["answer"].lower()), None)
                    target_value = match["value"] if match else item["answer"]
                    if locator:
                        await locator.select_option(value=target_value)
                    else:
                        # Shared name — scope to the right .chart-edit block by question text
                        await page.evaluate(
                            """([question, value]) => {
                                for (const block of document.querySelectorAll('.chart-edit')) {
                                    const h5 = block.querySelector('h5');
                                    if (!h5 || h5.innerText.trim() !== question) continue;
                                    const sel = block.querySelector('select');
                                    if (sel) { sel.value = value; sel.dispatchEvent(new Event('change', {bubbles: true})); }
                                }
                            }""",
                            [item["label"], target_value],
                        )

                elif ftype == "checkbox":
                    if item["answer"].lower() in ("yes", "true", "1", "on"):
                        await locator.check()
                    else:
                        await locator.uncheck()

                elif ftype == "radio":
                    radios = page.locator(f'[name="{item["name"]}"]')
                    count = await radios.count()
                    for i in range(count):
                        r = radios.nth(i)
                        val = await r.get_attribute("value") or ""
                        if val.lower() == item["answer"].lower():
                            await r.check()
                            break

                else:
                    await locator.fill(item["answer"])

                filled += 1
                print(f"  [{filled}/{n_to_fill}] {item['label'] or item['name']}", flush=True)

            except Exception as e:
                print(f"  Could not fill '{item['label'] or item['name']}': {e}")

        print("\nAll done. The form is filled in the browser.")
        print("Review and edit any fields, then submit when you're ready.")
        input("Press Enter here when finished to close the browser... ")
        await browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="URL of the form to fill")
    parser.add_argument("--sample", type=int, default=0,
                        help="Test mode: only process this many fields (no browser filling)")
    args = parser.parse_args()

    asyncio.run(run(args.url, sample=args.sample))
