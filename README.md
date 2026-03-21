# SN Form Automator

Fills out repetitive online intake forms for a special needs child using your existing documents (IEPs, evaluations, applications, etc.) as a knowledge base.

## Setup (one time)

```bash
# 1. Install dependencies
pip3 install -r requirements.txt

# 2. Install Playwright's browser
python3 -m playwright install chromium

# 3. Create your .env file
cp .env.example .env
# Edit .env and add your Anthropic API key

# 4. Drop your documents into knowledge_base/
#    Supported: PDF, .docx, .txt
#    Examples: IEPs, therapy evaluations, school applications, intake forms you've filled before

# 5. Ingest documents and auto-generate profile.yaml
python3 ingest.py

# 6. Open profile.yaml and review/correct the extracted info
#    Fill in anything marked null
```

## Filling a form

```bash
python3 fill_form.py https://example.com/intake-form
```

1. Browser opens and navigates to the URL
2. Log in manually if required, then press Enter
3. The script extracts all form fields and generates answers
4. You see a preview table of every field + proposed answer
5. Type `y` to proceed, or Ctrl+C to cancel
6. Fields are filled in the browser — you can edit anything
7. Submit the form yourself when satisfied

Fields the script is unsure about are marked `*** REVIEW ***` and left blank for you to fill manually.

## Adding new documents

Drop files into `knowledge_base/` and re-run:

```bash
python3 ingest.py
```

Already-indexed documents are skipped automatically.

## Re-extracting profile from scratch

```bash
python3 ingest.py --reset-profile
```
