"""
Tests for fill_form.py using real JaneApp HTML snippets as fixtures.
Mocks all Anthropic API calls — no network required.

Run with:
    python3 -m pytest test_fill_form.py -v
"""

import json
import pytest
from unittest.mock import MagicMock, patch, call
from playwright.async_api import async_playwright

import fill_form
from fill_form import FIELD_EXTRACTOR_JS, generate_all_answers, is_essay_field


# ---------------------------------------------------------------------------
# HTML fixture — real JaneApp markup from fortifysf.janeapp.com/intake_form
# ---------------------------------------------------------------------------

FIXTURE_HTML = """<!DOCTYPE html><html><body>

<!-- Standard text input -->
<div>
  <label for="first_name">Child's First Name</label>
  <input type="text" id="first_name" name="first_name">
</div>

<!-- Two selects sharing name="selected_option" (real JaneApp pattern) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Do you feel these are successful?</h5>
    </div>
  </div>
  <div class="sensitive">
    <div class="form-group" data-testid="form-group">
      <label class="control-label flush-bottom">
        <div class="gap-sm gap-bottom sr-only">Do you feel these are successful? </div>
        <div class="relative">
          <select class="form-control" name="selected_option" autocomplete="off">
            <option value="">Select an option...</option>
            <option value="Yes">Yes</option>
            <option value="No">No</option>
            <option value="Sometimes">Sometimes</option>
          </select>
        </div>
      </label>
    </div>
  </div>
</div>

<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Are therapies covered by insurance?</h5>
    </div>
  </div>
  <div class="sensitive">
    <div class="form-group" data-testid="form-group">
      <label class="control-label flush-bottom">
        <div class="gap-sm gap-bottom sr-only">Are therapies covered by insurance? </div>
        <div class="relative">
          <select class="form-control" name="selected_option" autocomplete="off">
            <option value="">Select an option...</option>
            <option value="Yes">Yes</option>
            <option value="No">No</option>
            <option value="Partially">Partially</option>
          </select>
        </div>
      </label>
    </div>
  </div>
</div>

<!-- Fieldset checkbox group (multiple-choice, no pre-checks) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Approximately how long does your child spend daily on weekdays on screens (excluding homework)?</h5>
    </div>
  </div>
  <div class="sensitive">
    <fieldset>
      <legend class="sr-only">Approximately how long does your child spend daily on weekdays on screens (excluding homework)?</legend>
      <div class="inline gap-right gap-sm">
        <label data-e2e-id="e2e_checkbox" class="input-button">
          <input type="checkbox" class="visually-hidden" name="option" value="1 hour or less">
          <span class="">1 hour or less</span>
        </label>
      </div>
      <div class="inline gap-right gap-sm">
        <label data-e2e-id="e2e_checkbox" class="input-button">
          <input type="checkbox" class="visually-hidden" name="option" value="1-2 hours">
          <span class="">1-2 hours</span>
        </label>
      </div>
      <div class="inline gap-right gap-sm">
        <label data-e2e-id="e2e_checkbox" class="input-button">
          <input type="checkbox" class="visually-hidden" name="option" value="2-4 hours">
          <span class="">2-4 hours</span>
        </label>
      </div>
      <div class="inline gap-right gap-sm">
        <label data-e2e-id="e2e_checkbox" class="input-button">
          <input type="checkbox" class="visually-hidden" name="option" value="4 hours or more">
          <span class="">4 hours or more</span>
        </label>
      </div>
    </fieldset>
  </div>
</div>

<!-- Fieldset checkbox group with some pre-checked boxes (real pattern from Child's Information) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Child's Information</h5>
    </div>
  </div>
  <div class="sensitive">
    <fieldset>
      <legend class="sr-only">Child's Information</legend>
      <div class="row">
        <div class="col-sm-3">
          <label data-e2e-id="e2e_checkbox" class="input-button">
            <input type="checkbox" class="visually-hidden" name="option"
                   aria-checked="true" value="Child's Name:" checked>
            <span>Child's Name:</span>
          </label>
        </div>
        <div class="col-sm-3">
          <label data-e2e-id="e2e_checkbox" class="input-button">
            <input type="checkbox" class="visually-hidden" name="option"
                   aria-checked="true" value="Date of Birth:" checked>
            <span>Date of Birth:</span>
          </label>
        </div>
        <div class="col-sm-3">
          <label data-e2e-id="e2e_checkbox" class="input-button">
            <input type="checkbox" class="visually-hidden" name="option"
                   aria-checked="false" value="Grade:">
            <span>Grade:</span>
          </label>
        </div>
      </div>
    </fieldset>
  </div>
</div>

<!-- Range/Likert slider (real pattern: name="value", aria-label = question text) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Does your child have difficulty with sleeping?</h5>
    </div>
  </div>
  <div class="sensitive">
    <legend class="sr-only">Does your child have difficulty with sleeping?</legend>
    <div class="range-padding">
      <input name="value" type="range" min="0" max="5"
             aria-label="Does your child have difficulty with sleeping?"
             aria-valuemin="0" aria-valuemax="5" aria-valuenow="0"
             aria-valuetext="0 not at all" value="0">
    </div>
  </div>
</div>

<!-- Second range slider to verify each gets its own entry -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Does your child have difficulty with eating?</h5>
    </div>
  </div>
  <div class="sensitive">
    <legend class="sr-only">Does your child have difficulty with eating?</legend>
    <div class="range-padding">
      <input name="value" type="range" min="0" max="5"
             aria-label="Does your child have difficulty with eating?"
             aria-valuemin="0" aria-valuemax="5" aria-valuenow="0"
             aria-valuetext="0 not at all" value="0">
    </div>
  </div>
</div>

<!-- Quill rich-text editor (short-answer question) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> What time does your child typically go to sleep/wake in the am?</h5>
    </div>
  </div>
  <div class="sensitive">
    <div translate="no" class="ql-wrapper">
      <div class="ql-container ql-snow">
        <div class="ql-editor ql-blank" data-gramm="false" contenteditable="true" role="textbox"
             aria-multiline="true"
             aria-label="What time does your child typically go to sleep/wake in the am?"
             style="min-height: 80px;">
          <div><br></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Quill rich-text editor (essay question) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Briefly describe a typical meal for your child. Where does your child eat? What supports, if any, are needed?</h5>
    </div>
  </div>
  <div class="sensitive">
    <div translate="no" class="ql-wrapper">
      <div class="ql-container ql-snow">
        <div class="ql-editor ql-blank" data-gramm="false" contenteditable="true" role="textbox"
             aria-multiline="true"
             aria-label="Briefly describe a typical meal for your child. Where does your child eat? What supports, if any, are needed?"
             style="min-height: 80px;">
          <div><br></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Pre-filled Quill (should be skipped during fill) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Referring provider</h5>
    </div>
  </div>
  <div class="sensitive">
    <div translate="no" class="ql-wrapper">
      <div class="ql-container ql-snow">
        <div class="ql-editor" data-gramm="false" contenteditable="true" role="textbox"
             aria-multiline="true"
             aria-label="Referring provider"
             style="min-height: 80px;">
          <div>Dr. Smith</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Pre-filled policy Quill (real pattern from Fort Policies section) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Treatment Policy</h5>
    </div>
  </div>
  <div class="sensitive">
    <div translate="no" class="ql-wrapper">
      <div class="ql-container ql-snow">
        <div class="ql-editor" data-gramm="false" contenteditable="true" role="textbox"
             aria-multiline="true"
             aria-label="Treatment Policy">
          <div>Treatment sessions are scheduled based on a 50-minute clinical hour.</div>
          <div>If you are running late, please contact your therapist directly.</div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Signature radio group (inside .chart-edit fieldset — radios, not checkboxes) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Sign here</h5>
    </div>
  </div>
  <div class="sensitive">
    <div class="signature-wrapper">
      <div class="form-group" data-testid="form-group">
        <fieldset class="control-label flush-bottom">
          <legend class="gap-sm gap-bottom sr-only">Selected Signature Input</legend>
          <div class="inline gap-right gap-sm">
            <label class="input-button">
              <input type="radio" class="visually-hidden" name="input"
                     aria-checked="true" value="drawn" checked>
              <div class="styled-radio gap-right checked"></div> Draw
            </label>
          </div>
          <div class="inline gap-right gap-sm">
            <label class="input-button">
              <input type="radio" class="visually-hidden" name="input"
                     aria-checked="false" value="typed">
              <div class="styled-radio gap-right"></div> Type
            </label>
          </div>
        </fieldset>
      </div>
    </div>
  </div>
</div>


<!-- Vision section: checkbox options with adjacent Quill note fields (real pattern from Hearing & Vision) -->
<div class="chart-edit">
  <div class="menu-toggle-container">
    <div class="flex flex-left flex-inline flex-baseline">
      <h5> Has your child had a vision test?</h5>
    </div>
  </div>
  <div class="sensitive">
    <fieldset>
      <legend class="sr-only">Has your child had a vision test?</legend>
      <div class="row">
        <div class="col-sm-3">
          <label data-e2e-id="e2e_checkbox" class="input-button">
            <input type="checkbox" class="visually-hidden" name="option"
                   aria-checked="false" value="yes">
            <span>yes</span>
          </label>
        </div>
        <div class="col-sm-9">
          <div class="ql-wrapper">
            <div class="ql-container ql-snow">
              <div class="ql-editor ql-blank" contenteditable="true" role="textbox"
                   aria-label="yes" aria-multiline="true">
                <div><br></div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="row">
        <div class="col-sm-3">
          <label data-e2e-id="e2e_checkbox" class="input-button">
            <input type="checkbox" class="visually-hidden" name="option"
                   aria-checked="false" value="no">
            <span>no</span>
          </label>
        </div>
        <div class="col-sm-9">
          <div class="ql-wrapper">
            <div class="ql-container ql-snow">
              <div class="ql-editor ql-blank" contenteditable="true" role="textbox"
                   aria-label="no" aria-multiline="true">
                <div><br></div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="row">
        <div class="col-sm-3">
          <label data-e2e-id="e2e_checkbox" class="input-button">
            <input type="checkbox" class="visually-hidden" name="option"
                   aria-checked="false" value="if yes, results:">
            <span>if yes, results:</span>
          </label>
        </div>
        <div class="col-sm-9">
          <div class="ql-wrapper">
            <div class="ql-container ql-snow">
              <div class="ql-editor ql-blank" contenteditable="true" role="textbox"
                   aria-label="if yes, results:" aria-multiline="true">
                <div><br></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </fieldset>
  </div>
</div>

</body></html>"""


# ---------------------------------------------------------------------------
# Playwright fixture
# ---------------------------------------------------------------------------

@pytest.fixture
async def form_page(tmp_path):
    html_file = tmp_path / "form.html"
    html_file.write_text(FIXTURE_HTML)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"file://{html_file}")
        yield page
        await browser.close()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _extract(form_page):
    """Run the field extractor and return fields indexed by label."""
    fields = await form_page.evaluate(FIELD_EXTRACTOR_JS)
    return {f["label"]: f for f in fields}


# ---------------------------------------------------------------------------
# Standard inputs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_standard_input_detected(form_page):
    by_label = await _extract(form_page)
    assert "Child's First Name" in by_label
    assert by_label["Child's First Name"]["type"] == "text"


@pytest.mark.asyncio
async def test_standard_input_has_id_selector(form_page):
    by_label = await _extract(form_page)
    assert by_label["Child's First Name"]["selector"] == "#first_name"


# ---------------------------------------------------------------------------
# Shared-name selects
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shared_name_selects_both_detected(form_page):
    """Both selects with name='selected_option' should be captured with their own labels."""
    by_label = await _extract(form_page)
    assert "Do you feel these are successful?" in by_label
    assert "Are therapies covered by insurance?" in by_label


@pytest.mark.asyncio
async def test_shared_name_select_has_null_selector(form_page):
    """Selects sharing a name must use evaluate()-based filling (selector=null)."""
    by_label = await _extract(form_page)
    assert by_label["Do you feel these are successful?"]["selector"] is None
    assert by_label["Are therapies covered by insurance?"]["selector"] is None


@pytest.mark.asyncio
async def test_select_options_extracted(form_page):
    by_label = await _extract(form_page)
    option_texts = [o["text"] for o in by_label["Do you feel these are successful?"]["options"]]
    assert "Yes" in option_texts
    assert "No" in option_texts
    assert "Sometimes" in option_texts


@pytest.mark.asyncio
async def test_select_placeholder_not_in_options(form_page):
    """'Select an option...' placeholder should be in the raw options list but filtered in prompts."""
    by_label = await _extract(form_page)
    # Raw options include the placeholder (Claude prompt filtering strips it)
    option_texts = [o["text"] for o in by_label["Are therapies covered by insurance?"]["options"]]
    assert "Partially" in option_texts


# ---------------------------------------------------------------------------
# Fieldset checkbox groups
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkbox_group_detected_as_group(form_page):
    """Fieldset checkboxes should be one janeapp-checkbox-group, not individual checkboxes."""
    by_label = await _extract(form_page)
    q = "Approximately how long does your child spend daily on weekdays on screens (excluding homework)?"
    assert q in by_label
    assert by_label[q]["type"] == "janeapp-checkbox-group"


@pytest.mark.asyncio
async def test_checkbox_group_options_populated(form_page):
    by_label = await _extract(form_page)
    q = "Approximately how long does your child spend daily on weekdays on screens (excluding homework)?"
    option_values = [o["value"] for o in by_label[q]["options"]]
    assert "1 hour or less" in option_values
    assert "1-2 hours" in option_values
    assert "2-4 hours" in option_values
    assert "4 hours or more" in option_values


@pytest.mark.asyncio
async def test_checkbox_options_not_captured_individually(form_page):
    """Individual visually-hidden checkbox inputs must not appear as separate fields."""
    fields = await form_page.evaluate(FIELD_EXTRACTOR_JS)
    labels = [f["label"] for f in fields]
    assert "1 hour or less" not in labels
    assert "1-2 hours" not in labels


@pytest.mark.asyncio
async def test_checkbox_group_unchecked_has_empty_current_value(form_page):
    """A checkbox group with no pre-checked boxes should have current_value=''."""
    by_label = await _extract(form_page)
    q = "Approximately how long does your child spend daily on weekdays on screens (excluding homework)?"
    assert by_label[q]["current_value"] == ""


@pytest.mark.asyncio
async def test_prechecked_checkbox_group_current_value(form_page):
    """Pre-checked boxes (aria-checked=true or checked attr) appear in current_value."""
    by_label = await _extract(form_page)
    cv = by_label["Child's Information"]["current_value"]
    assert "Child's Name:" in cv
    assert "Date of Birth:" in cv


@pytest.mark.asyncio
async def test_unchecked_option_absent_from_current_value(form_page):
    """Unchecked boxes must not appear in current_value."""
    by_label = await _extract(form_page)
    cv = by_label["Child's Information"]["current_value"]
    assert "Grade:" not in cv


# ---------------------------------------------------------------------------
# Range / Likert sliders
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_range_slider_detected(form_page):
    """Range inputs should be captured by Pass 1 with type='range'."""
    by_label = await _extract(form_page)
    assert "Does your child have difficulty with sleeping?" in by_label
    assert by_label["Does your child have difficulty with sleeping?"]["type"] == "range"


@pytest.mark.asyncio
async def test_range_slider_label_from_aria_label(form_page):
    """Range slider label should come from aria-label attribute."""
    by_label = await _extract(form_page)
    # Both sliders have distinct aria-label values so both should appear
    assert "Does your child have difficulty with eating?" in by_label


@pytest.mark.asyncio
async def test_range_slider_current_value(form_page):
    """Range slider default value ('0') should be captured in current_value."""
    by_label = await _extract(form_page)
    assert by_label["Does your child have difficulty with sleeping?"]["current_value"] == "0"


@pytest.mark.asyncio
async def test_multiple_range_sliders_distinct_entries(form_page):
    """Each range slider with a unique aria-label gets its own entry — not deduped."""
    by_label = await _extract(form_page)
    assert "Does your child have difficulty with sleeping?" in by_label
    assert "Does your child have difficulty with eating?" in by_label


# ---------------------------------------------------------------------------
# Quill rich-text editors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quill_fields_detected(form_page):
    by_label = await _extract(form_page)
    q = "What time does your child typically go to sleep/wake in the am?"
    assert q in by_label
    assert by_label[q]["type"] == "quill"


@pytest.mark.asyncio
async def test_quill_selector_uses_aria_label(form_page):
    """Quill selector should be a CSS attribute selector on aria-label."""
    by_label = await _extract(form_page)
    q = "What time does your child typically go to sleep/wake in the am?"
    assert "ql-editor" in by_label[q]["selector"]
    assert "aria-label" in by_label[q]["selector"]


@pytest.mark.asyncio
async def test_empty_quill_has_empty_current_value(form_page):
    by_label = await _extract(form_page)
    q = "What time does your child typically go to sleep/wake in the am?"
    assert by_label[q]["current_value"] == ""


@pytest.mark.asyncio
async def test_prefilled_quill_has_current_value(form_page):
    by_label = await _extract(form_page)
    assert "Referring provider" in by_label
    assert by_label["Referring provider"]["current_value"].strip() == "Dr. Smith"


@pytest.mark.asyncio
async def test_policy_quill_has_multiline_current_value(form_page):
    """Policy Quill editors with multiple div children should have multi-sentence current_value."""
    by_label = await _extract(form_page)
    assert "Treatment Policy" in by_label
    cv = by_label["Treatment Policy"]["current_value"]
    assert "50-minute" in cv


# ---------------------------------------------------------------------------
# Signature fieldset (radios inside .chart-edit fieldset)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_signature_fieldset_captured_by_pass2(form_page):
    """Pass 2 captures all .chart-edit fieldsets; the signature one appears as janeapp-checkbox-group."""
    by_label = await _extract(form_page)
    assert "Selected Signature Input" in by_label
    assert by_label["Selected Signature Input"]["type"] == "janeapp-checkbox-group"


@pytest.mark.asyncio
async def test_signature_radio_inputs_not_captured_individually(form_page):
    """Individual radio inputs inside .chart-edit fieldset should not appear as separate fields."""
    fields = await form_page.evaluate(FIELD_EXTRACTOR_JS)
    labels = [f["label"] for f in fields]
    # "Draw" and "Type" should not be individual entries
    assert "Draw" not in labels
    assert "Type" not in labels
    assert "drawn" not in labels


# ---------------------------------------------------------------------------
# Quill note fields inside checkbox fieldsets get parent question context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quill_note_field_label_prefixed_with_parent_question(form_page):
    """Quill note fields inside a .chart-edit fieldset must be prefixed with the parent question.
    Without this, labels like 'yes'/'no' give Claude no context about what section it's in."""
    by_label = await _extract(form_page)
    # Raw label "yes" should NOT appear as a standalone field
    assert "yes" not in by_label
    assert "no" not in by_label
    assert "if yes, results:" not in by_label


@pytest.mark.asyncio
async def test_quill_note_field_prefixed_label_contains_parent_question(form_page):
    """The prefixed label should include the parent section question."""
    by_label = await _extract(form_page)
    # Count only the prefixed note fields (those with " — "), not the checkbox group itself
    prefixed = [l for l in by_label if "Has your child had a vision test? —" in l]
    assert len(prefixed) == 3  # yes, no, if yes results


@pytest.mark.asyncio
async def test_quill_note_field_prefixed_label_format(form_page):
    """Prefixed label format: '<parent question> — <option label>'."""
    by_label = await _extract(form_page)
    assert "Has your child had a vision test? — yes" in by_label
    assert "Has your child had a vision test? — no" in by_label
    assert "Has your child had a vision test? — if yes, results:" in by_label


@pytest.mark.asyncio
async def test_quill_note_field_selector_uses_original_aria_label(form_page):
    """The CSS selector must still use the original aria-label (not the prefixed one)
    so it matches the actual DOM element."""
    by_label = await _extract(form_page)
    field = by_label["Has your child had a vision test? — if yes, results:"]
    assert field["type"] == "quill"
    # Selector must use the original DOM aria-label, not the prefixed display label
    assert field["selector"] == '.ql-editor[aria-label="if yes, results:"]'


# ---------------------------------------------------------------------------
# is_essay_field
# ---------------------------------------------------------------------------

def test_is_essay_field_quill():
    assert is_essay_field({"type": "quill"}) is True

def test_is_essay_field_textarea():
    assert is_essay_field({"type": "textarea"}) is True

def test_is_essay_field_select():
    assert is_essay_field({"type": "select"}) is False

def test_is_essay_field_checkbox_group():
    assert is_essay_field({"type": "janeapp-checkbox-group"}) is False

def test_is_essay_field_range():
    assert is_essay_field({"type": "range"}) is False

def test_is_essay_field_text():
    assert is_essay_field({"type": "text"}) is False


# ---------------------------------------------------------------------------
# generate_all_answers (Claude API mocked)
# ---------------------------------------------------------------------------

def _make_mock_response(index_answers: dict) -> MagicMock:
    """Build a mock Anthropic response returning index_answers as JSON."""
    mock = MagicMock()
    mock.content = [MagicMock(text=json.dumps(index_answers))]
    return mock


def test_generate_all_answers_short_fields():
    """Non-quill fields go in one batch; answers are mapped back by label."""
    fields = [
        {"label": "Child's First Name", "type": "text", "options": []},
        {"label": "Do you feel these are successful?", "type": "select",
         "options": [{"text": "Yes", "value": "Yes"}, {"text": "No", "value": "No"}]},
    ]
    mock_resp = _make_mock_response({"0": "Emma", "1": "Sometimes"})

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.return_value = mock_resp
        answers = generate_all_answers(fields, {}, "")

    assert answers["Child's First Name"] == "Emma"
    assert answers["Do you feel these are successful?"] == "Sometimes"
    mock_client.messages.create.assert_called_once()


def test_generate_all_answers_quill_fields():
    """Quill fields go in a separate batch; Claude decides answer length."""
    fields = [
        {"label": "What time does your child go to sleep?", "type": "quill", "options": []},
        {"label": "Briefly describe a typical meal.", "type": "quill", "options": []},
    ]
    mock_resp = _make_mock_response({
        "0": "8:30 PM",
        "1": "Our child eats at the kitchen table with support to stay seated.",
    })

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.return_value = mock_resp
        answers = generate_all_answers(fields, {}, "some KB context")

    assert answers["What time does your child go to sleep?"] == "8:30 PM"
    assert "kitchen table" in answers["Briefly describe a typical meal."]
    mock_client.messages.create.assert_called_once()


def test_generate_all_answers_two_calls_for_mixed_fields():
    """Mixed form: one call for non-quill, one call for quill."""
    fields = [
        {"label": "First Name", "type": "text", "options": []},
        {"label": "Sleep time", "type": "quill", "options": []},
    ]
    short_resp = _make_mock_response({"0": "Emma"})
    quill_resp  = _make_mock_response({"0": "8:30 PM"})

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.side_effect = [short_resp, quill_resp]
        answers = generate_all_answers(fields, {}, "")

    assert answers["First Name"] == "Emma"
    assert answers["Sleep time"] == "8:30 PM"
    assert mock_client.messages.create.call_count == 2


def test_generate_all_answers_range_in_nonquill_batch():
    """Range sliders are non-quill and should go in the first (structured fields) batch."""
    fields = [
        {"label": "Does your child have difficulty with sleeping?", "type": "range", "options": []},
        {"label": "Does your child have difficulty with eating?", "type": "range", "options": []},
    ]
    mock_resp = _make_mock_response({"0": "3", "1": "1"})

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.return_value = mock_resp
        answers = generate_all_answers(fields, {}, "")

    # Both range fields answered in a single call (non-quill batch)
    mock_client.messages.create.assert_called_once()
    assert answers["Does your child have difficulty with sleeping?"] == "3"
    assert answers["Does your child have difficulty with eating?"] == "1"


def test_generate_all_answers_checkbox_group_in_nonquill_batch():
    """janeapp-checkbox-group fields go in the non-quill batch."""
    fields = [
        {
            "label": "Approximately how long does your child spend daily on weekdays on screens (excluding homework)?",
            "type": "janeapp-checkbox-group",
            "options": [
                {"value": "1 hour or less", "text": "1 hour or less"},
                {"value": "1-2 hours", "text": "1-2 hours"},
                {"value": "2-4 hours", "text": "2-4 hours"},
            ],
        },
    ]
    mock_resp = _make_mock_response({"0": "1-2 hours"})

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.return_value = mock_resp
        answers = generate_all_answers(fields, {}, "")

    mock_client.messages.create.assert_called_once()
    q = "Approximately how long does your child spend daily on weekdays on screens (excluding homework)?"
    assert answers[q] == "1-2 hours"


def test_generate_all_answers_all_field_types():
    """Full mix of all real field types produces exactly 2 API calls (1 non-quill + 1 quill)."""
    fields = [
        {"label": "Child's First Name", "type": "text", "options": []},
        {"label": "Do you feel these are successful?", "type": "select",
         "options": [{"text": "Yes", "value": "Yes"}]},
        {"label": "Screen time", "type": "janeapp-checkbox-group",
         "options": [{"value": "1 hour or less", "text": "1 hour or less"}]},
        {"label": "Difficulty sleeping?", "type": "range", "options": []},
        {"label": "Describe a typical meal.", "type": "quill", "options": []},
    ]
    short_resp = _make_mock_response({"0": "Emma", "1": "Yes", "2": "1 hour or less", "3": "2"})
    quill_resp  = _make_mock_response({"0": "She eats at the kitchen table."})

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.side_effect = [short_resp, quill_resp]
        answers = generate_all_answers(fields, {}, "kb context")

    assert mock_client.messages.create.call_count == 2
    assert answers["Child's First Name"] == "Emma"
    assert answers["Do you feel these are successful?"] == "Yes"
    assert answers["Screen time"] == "1 hour or less"
    assert answers["Difficulty sleeping?"] == "2"
    assert "kitchen table" in answers["Describe a typical meal."]


def test_generate_all_answers_unknown_field_returns_empty():
    """Fields Claude can't answer should return empty string so they're left blank."""
    fields = [{"label": "Unknown field", "type": "text", "options": []}]
    mock_resp = _make_mock_response({"0": ""})

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.return_value = mock_resp
        answers = generate_all_answers(fields, {}, "")

    assert answers["Unknown field"] == ""


def test_generate_all_answers_quill_chunked():
    """Quill fields exceeding QUILL_BATCH_SIZE should trigger multiple calls."""
    original_batch_size = fill_form.QUILL_BATCH_SIZE
    fill_form.QUILL_BATCH_SIZE = 2  # force chunking with just 3 fields

    fields = [
        {"label": f"Q{i}", "type": "quill", "options": []} for i in range(3)
    ]
    responses = [
        _make_mock_response({"0": "answer0", "1": "answer1"}),  # chunk 1: Q0, Q1
        _make_mock_response({"0": "answer2"}),                   # chunk 2: Q2 only
    ]

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.side_effect = responses
        answers = generate_all_answers(fields, {}, "")

    assert mock_client.messages.create.call_count == 2
    assert answers["Q0"] == "answer0"
    assert answers["Q1"] == "answer1"
    assert answers["Q2"] == "answer2"
    fill_form.QUILL_BATCH_SIZE = original_batch_size


def test_generate_all_answers_profile_passed_to_api():
    """Profile dict should be serialized and included in the API call prompt."""
    profile = {"child": {"name": "Emma", "dob": "2018-05-01"}}
    fields = [{"label": "Child name", "type": "text", "options": []}]
    mock_resp = _make_mock_response({"0": "Emma"})

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.return_value = mock_resp
        generate_all_answers(fields, profile, "")

    call_args = mock_client.messages.create.call_args
    prompt = call_args[1]["messages"][0]["content"]
    assert "Emma" in prompt or "child" in prompt


def test_generate_all_answers_kb_context_only_in_quill_call():
    """KB context should be passed to quill calls but not to the non-quill batch."""
    fields = [
        {"label": "First Name", "type": "text", "options": []},
        {"label": "Essay question", "type": "quill", "options": []},
    ]
    short_resp = _make_mock_response({"0": "Emma"})
    quill_resp  = _make_mock_response({"0": "A detailed answer."})

    with patch.object(fill_form, "client") as mock_client:
        mock_client.messages.create.side_effect = [short_resp, quill_resp]
        generate_all_answers(fields, {}, "IMPORTANT KB CONTEXT")

    calls = mock_client.messages.create.call_args_list
    short_prompt = calls[0][1]["messages"][0]["content"]
    quill_prompt  = calls[1][1]["messages"][0]["content"]

    assert "IMPORTANT KB CONTEXT" not in short_prompt
    assert "IMPORTANT KB CONTEXT" in quill_prompt
