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


def generate_short_answer(label: str, field_type: str, options: list[dict], profile: dict, context: str) -> str:
    """Generate a concise factual answer for a standard input field."""
    options_text = ""
    if options:
        readable = [o["text"] for o in options if o.get("text") and o["text"] != "--"]
        if readable:
            options_text = f"\nAvailable options (you MUST pick one exactly as written): {', '.join(readable)}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                "You are filling out a form for a special needs child. "
                "Return ONLY the answer value — no explanation, no punctuation around it.\n\n"
                f"Field label: {label}\n"
                f"Field type: {field_type}"
                f"{options_text}\n\n"
                f"Profile:\n{yaml.dump(profile, default_flow_style=False)}\n\n"
                f"Relevant document context:\n{context}\n\n"
                "Rules:\n"
                "- Return the answer only\n"
                "- For yes/no or boolean fields, return 'yes' or 'no'\n"
                "- For date fields, use MM/DD/YYYY\n"
                "- For phone fields, use (XXX) XXX-XXXX\n"
                "- For select/dropdown, return one of the available options exactly as written\n"
                "- If you cannot determine a confident answer, return NEEDS_REVIEW\n\n"
                "Answer:"
            ),
        }],
    )
    return response.content[0].text.strip()


def generate_essay_answer(label: str, profile: dict, context: str) -> str:
    """Generate a narrative paragraph answer for an open-ended essay question."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": (
                "You are helping a parent fill out an intake form for their special needs child. "
                "Write a response to the following open-ended question in the first person, "
                "as if the parent is writing it.\n\n"
                f"Question: {label}\n\n"
                f"Child's profile:\n{yaml.dump(profile, default_flow_style=False)}\n\n"
                f"Relevant excerpts from the child's documents (IEPs, evaluations, etc.):\n{context}\n\n"
                "Instructions:\n"
                "- Write 2–5 sentences as a natural, honest parent response\n"
                "- Draw on specific details from the documents and profile where available\n"
                "- Do not use bullet points or headers — write flowing prose\n"
                "- Do not fabricate specific details not found in the documents or profile\n"
                "- If there is genuinely not enough information to answer, respond with exactly: NEEDS_REVIEW\n"
                "- Do not include any preamble, just write the response\n\n"
                "Response:"
            ),
        }],
    )
    return response.content[0].text.strip()


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


async def run(url: str):
    from playwright.async_api import async_playwright

    profile = load_profile()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        print(f"\nOpening: {url}")
        await page.goto(url)

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

        print(f"Found {len(fields)} field(s). Generating answers (this may take a moment)...\n")

        fill_plan: list[dict] = []
        for field in fields:
            label = field["label"] or field["name"] or "(unlabeled)"
            essay = is_essay_field(field)

            # Skip fields already filled by the provider
            if field.get("current_value", "").strip():
                fill_plan.append({**field, "answer": None, "needs_review": False, "essay": essay, "prefilled": True})
                continue

            # Retrieve more context for open-ended questions
            context = query_kb(label, n=10 if essay else 5)
            if essay:
                answer = generate_essay_answer(label, profile, context)
            else:
                answer = generate_short_answer(label, field["type"], field["options"], profile, context)
            needs_review = answer == "NEEDS_REVIEW"
            fill_plan.append({**field, "answer": answer, "needs_review": needs_review, "essay": essay, "prefilled": False})

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

        confirm = input("\nFill the form with these answers? [y/N] ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            await browser.close()
            return

        print("\nFilling fields...")
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

                print(f"  Filled: {item['label'] or item['name']}")

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
    if len(sys.argv) < 2:
        print("Usage: python fill_form.py <url>")
        sys.exit(1)

    asyncio.run(run(sys.argv[1]))
