import os
import json
import zipfile
import tempfile
import requests
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path
from io import BytesIO
from jsonschema import validate


def validate_as_v22_but_save_as_v21(obj):
    if "info" not in obj:
        obj["info"] = {}
    obj["info"]["schema"] = "https://schema.getpostman.com/json/collection/v2.2.0/collection.json"
    validate(instance=obj, schema=schema)
    obj["info"]["schema"] = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


def salvage_partial_json(raw_str):
    start = raw_str.find('{')
    end = raw_str.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return None, "No JSON object boundaries found.", False
    candidate = raw_str[start:end+1]
    try:
        decoder = json.JSONDecoder()
        obj, idx = decoder.raw_decode(candidate)
        if idx < len(candidate):
            return obj, "Partial JSON salvaged from truncated output.", True
        return obj, None, False
    except Exception:
        for i in range(len(candidate), 0, -1):
            try:
                parsed = json.loads(candidate[:i])
                return parsed, "Partial JSON salvaged from truncated output.", i < len(candidate)
            except Exception:
                continue
    open_braces = candidate.count('{') - candidate.count('}')
    open_brackets = candidate.count('[') - candidate.count(']')
    fixed = candidate + ('}' * open_braces) + (']' * open_brackets)
    try:
        return json.loads(fixed), "Partial JSON salvaged by auto-closing braces/brackets.", True
    except Exception:
        st.write("Could not salvage any valid JSON from output.")
        return None, "Could not salvage any valid JSON from output.", False


load_dotenv()
api_url = os.getenv("AZURE_URL")

if not api_url or not api_url.strip():
    st.error("AZURE_URL is not set in your .env file or is empty. Please check your .env configuration.")
    st.stop()

schema = None
try:
    with open("postman_collection_v2.2_schema.json", "r", encoding="utf-8") as f:
        schema = json.load(f)
    st.warning("Loaded Postman v2.2 schema from local fallback.")
except Exception as fallback_error:
    st.error(f"Failed to load Postman schema.\n\nError: {fallback_error}")
    st.stop()

st.set_page_config(page_title="Postman Bulk Converter")
st.title("Convert All Postman JSONs from a Zipped Folder")

uploaded_zip = st.file_uploader("Upload a zipped folder of Postman collections (.zip)", type="zip")


def generate_script_v22(old_script, type):
    prompt = f'''
<|system|>
You are an excellent and helpful assistant that converts old Postman scripts from legacy format (v2.1.0) to the modern format (v2.2.0). Retain the version as v2.1.0 in the schema only. If the script is empty, leave it empty.

<|user|>
Convert the following Postman {type} script to modern syntax. Schema version should stay v2.1.0.

Instructions:
1. Do not change the logic or structure of the script.
2. If the script is empty, return an empty string.
3. Do not add extra sample code or usage examples or the words "javascript" or "js" or add comments in between the code.
4. Retain the original function names and variable names.
5. When using `pm.response.json()`, assign it to a variable named `response`, and assign `response.data || {{}}` to a variable named `nr`. Do **not** try to access `nr.data.property`, instead use `nr.property` — `nr` itself is already the `data` section.
6. Never write `pm.expect(nr.data).to.have.property(...)` — that's incorrect. Use `pm.expect(nr).to.have.property(...)` instead.
7. Keep in mind that there is no function like `pm.response.json(...).has()`. Use `.hasOwnProperty(...)` safely.
8. **DO NOT** give me a script which would lead to a “no tests found” error in Postman.
9. Do not use `pm.response` inside Pre-request scripts.
10. Preserve original test descriptions; do not reword test titles.
11. Do not add any new functions or variables unless they existed in the original test or pre-request script.
12. Do not use `JSON.parse(pm.response.json())` — `pm.response.json()` is already parsed.
13. Do not use `pm.globals.get(...)` in Pre-request scripts and do not use `pm.globals.set(...)` in Test scripts unless the original script used them.

### Response structure:
Assume all scripts reference a JSON structure like this (from `pm.response.json()`):
{{
  code: 0,
  message: "success",
  data: {{
    summary_details: {{
      down_count: ...,
      downtime_duration: ...,
      ...
    }},
    charts: [...],
    info: {{...}},
    availability_details: [...],
    outage_details: [...],
    profile_details: {{...}},
    ...
  }}
}}

14. Use the structure above to correctly navigate nested properties. For example:
    - `data.summary_details.down_count` should be accessed via `nr.summary_details.down_count`
    - Never use `response.down_count` directly unless it is top-level (which it isn't in this structure).
    - Always check if the parent (e.g., `summary_details`) exists before accessing its children.
    - If the property is an array (like `charts` or `outage_details`), use a `for` loop to iterate and check each element.

### Syntax changes:
- Replace `tests["..."] =` with `pm.test(...)`.
- Use `pm.expect(...)` instead of other assertion styles.
- Replace `responseBody` with `pm.response.json()`.
- Replace `responseCode.code` with `pm.response.code`.
- Replace all `postman.setGlobalVariable(...)` with `pm.globals.set(...)`.

### Global utilities:
- If a global function is stored using `postman.setGlobalVariable('function_name', ...)`, convert it as follows:
  - For Pre-request scripts:  
    `pm.globals.set(function_name, function_call() {{ ... }} + 'function_call()');`
  - For Test scripts:
    let function_call = pm.globals.get("function_name");
    eval(function_call);
    function_call();
    Ensure `function_call` and `function_name` are different strings to avoid name collision.

### Validations:
- If a global function is referenced but undefined, add a warning comment.
- Never remove or rename variables or change test count.

### Output:
Return the converted script **as plain JavaScript only**, with no additional comments, markdown, or explanation.

{old_script}
'''
    payload = {
        "systemprompt": "",
        "userprompt": prompt,
        "message": [],
        "model": "gpt-4.1-mini"
    }
    response = requests.post(api_url, json=payload, timeout=3200)
    response.raise_for_status()
    return response.text.strip().strip('`\n"\' ')

def generate_postman_v22_again(oldpm_raw):
    prompt = f"""
<|system|>
You are a helpful assistant that corrects the format of the Postman v2.2.0 collection.

<|user|>
Update this collection to Postman v2.2.0 with proper test scripts (pm.test, pm.expect, pm.response). Retain v2.1.0 in the schema string.

```json
{oldpm_raw}
"""
    payload = {
        "systemprompt": "",
        "userprompt": prompt,
        "message": [],
        "model": "gpt-4.1-mini"
    }
    response = requests.post(api_url, json=payload, timeout=180)
    response.raise_for_status()
    fixed = response.text.strip().removeprefix("```json").removesuffix("```").strip()
    return fixed

def fix_syntax_v22(script):
    prompt = f'''
<|system|>
You are an expert Postman script fixer that corrects syntax issues like extra brackets, missing semicolons, or other common JavaScript syntax errors in Postman scripts.
<|user|>
Fix the syntax issues in the following Postman script and return the corrected script. If its already valid, return it as it is.
**DO NOT** change the logic, structure, or variable names. Only fix the syntax errors and return the corrected script as plain JavaScript only, without any comments or explanations.

Return the fixed script as plain JavaScript only, with no extra comments, explanations, or markdown.
{script}
'''
    payload = {
        "systemprompt": "",
        "userprompt": prompt,
        "message": [],
        "model": "gpt-4.1-mini"
    }
    response = requests.post(api_url, json=payload, timeout=3200)
    response.raise_for_status()
    return response.text.strip().strip('`\n"\' ')

def generate_script_v22_fix(truncated_script, original_script, script_type):
    prompt = f'''
<|system|>
You are a helpful and an excellent assistant that completes truncated or incomplete Postman {script_type} scripts by continuing right off from where the previous LLM's response ended. The previous LLM response was truncated or incomplete. Your job is to finish the script correctly, preserving all logic, function names, and structure from the original input.

Instructions:
1. Do **not** rewrite the entire script.
2. Return **only** the missing or final portion needed to complete the truncated script.
3. Maintain all original function names and variable names.
4. Never change the logic, restructure blocks, or reword test descriptions.
5. Do not return duplicate or rewritten code. Only return what's missing from the end.
6. Return JavaScript only with no extra comments, no explanations, and no markdown.

### Response structure:
Assume all scripts reference a JSON structure like this (from `pm.response.json()`):
{{
  code: 0,
  message: "success",
  data: {{
    summary_details: {{
      down_count: ...,
      downtime_duration: ...,
      ...
    }},
    charts: [...],
    info: {{...}},
    availability_details: [...],
    outage_details: [...],
    profile_details: {{...}},
    ...
  }}
}}
4. Use the structure above to correctly navigate nested properties. For example:
    - `data.summary_details.down_count` should be accessed via `response.summary_details.down_count`
    - Never use `response.down_count` directly unless it is top-level (which it isn't in this structure).
    - Always check if the parent (e.g., `summary_details`) exists before accessing its children.

<|user|>
The following is a truncated or incomplete Postman {script_type} script (output from a previous LLM call):
---
{truncated_script}
---

Here is the original input script that was supposed to be converted to the new format:
---
{original_script}
---

Please complete and repair the truncated output by appending from the end of the above truncated script the correct converted logic of the original script, returning the full, valid, and modernized Postman {script_type} script as plain JavaScript only. Do not add any extra comments, explanations, or markdown. Finally append your output to the input and check if it is a valid function before returning only your output.
'''
    payload = {
        "systemprompt": "",
        "userprompt": prompt,
        "message": [],
        "model": "gpt-4.1-mini"
    }
    response = requests.post(api_url, json=payload, timeout=3200)
    response.raise_for_status()
    return response.text.strip().strip('`\n"\' ')

def convert_scripts_in_collection(obj, parent_listen=None):
    if isinstance(obj, dict):
        if "event" in obj and isinstance(obj["event"], list):
            for event in obj["event"]:
                listen_type = event.get("listen", None)
                if "script" in event:
                    convert_scripts_in_collection(event, parent_listen=listen_type)
        if "script" in obj and isinstance(obj["script"], dict) and "exec" in obj["script"]:
            value = obj["script"]
            old_exec = value["exec"]
            if isinstance(old_exec, list) and all(line.strip() == "" for line in old_exec):
                value["exec"] = []
            else:
                script_text = "\n".join(old_exec) if isinstance(old_exec, list) else str(old_exec)
                try:
                    script_type = parent_listen if parent_listen in ("prerequest", "test") else "test"
                    new_script = generate_script_v22(script_text, script_type)
                    cleaned_script = new_script.strip()
                    for prefix in ["javascript", "js"]:
                        if cleaned_script.lower().startswith(prefix):
                            cleaned_script = cleaned_script[len(prefix):].lstrip(':').lstrip('\n').lstrip()
                    def is_truncated(s):
                        stack = []
                        pairs = {')': '(', '}': '{', ']': '['}
                        for c in s:
                            if c in '({[':
                                stack.append(c)
                            elif c in ')}]':
                                if not stack or stack[-1] != pairs[c]:
                                    return True
                                stack.pop()
                        return bool(stack) 
                    if not cleaned_script:
                        value["exec"] = []
                    elif is_truncated(cleaned_script):
                        max_attempts = 7
                        attempts = 0
                        while is_truncated(cleaned_script) and attempts < max_attempts:
                            fixed_script = generate_script_v22_fix(cleaned_script, script_text, script_type)
                            new_script = fixed_script.strip()
                            for prefix in ["javascript", "js"]:
                                if new_script.lower().startswith(prefix):
                                    new_script = new_script[len(prefix):].lstrip(':').lstrip('\n').lstrip()
                            cleaned_script += new_script
                            attempts += 1
                        if is_truncated(cleaned_script):
                            fixed_script = fix_syntax_v22(cleaned_script)
                            new_script = fixed_script.strip()
                            for prefix in ["javascript", "js"]:
                                if new_script.lower().startswith(prefix):
                                    new_script = new_script[len(prefix):].lstrip(':').lstrip('\n').lstrip()
                            cleaned_script = new_script
                        value["exec"] = cleaned_script.splitlines() if cleaned_script else []
                    else:
                        value["exec"] = cleaned_script.splitlines()
                except Exception as e:
                    st.warning(f"Script conversion failed: {e}")
        if "item" in obj and isinstance(obj["item"], list):
            for subitem in obj["item"]:
                convert_scripts_in_collection(subitem)
        for key, value in obj.items():
            if key not in ("event", "script", "item"):
                convert_scripts_in_collection(value, parent_listen=parent_listen)
    elif isinstance(obj, list):
        for item in obj:
            convert_scripts_in_collection(item, parent_listen=parent_listen)


if uploaded_zip:
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, "uploaded.zip")
        with open(zip_path, "wb") as f:
            f.write(uploaded_zip.read())

        extract_dir = os.path.join(tmpdir, "unzipped")
        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        st.success("Zip extracted. Starting script conversion...")

        converted_dir = os.path.join(tmpdir, "converted")
        os.makedirs(converted_dir, exist_ok=True)
        converted_files = 0

        for root, _, files in os.walk(extract_dir):
            for file in files:
                if file.endswith(".json"):
                    input_path = os.path.join(root, file)
                    try:
                        with open(input_path, "r", encoding="utf-8") as f:
                            raw = f.read()
                        collection_json = json.loads(raw)

                        if "item" not in collection_json:
                            st.warning(f"{file} skipped: No 'item' key found.")
                            continue

                        convert_scripts_in_collection(collection_json)
                        validate_as_v22_but_save_as_v21(collection_json)

                        out_path = os.path.join(converted_dir, f"{Path(file).stem}_converted.json")
                        with open(out_path, "w", encoding="utf-8") as f:
                            f.write(json.dumps(collection_json, indent=2))
                        converted_files += 1
                    except Exception as e:
                        st.error(f"Failed to process {file}: {e}")

        if converted_files == 0:
            st.warning("No valid .json files were converted.")
        else:
            valid_files = []
            invalid_files = []
            for file in os.listdir(converted_dir):
                file_path = os.path.join(converted_dir, file)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    validate_as_v22_but_save_as_v21(data)
                    valid_files.append(file)
                except Exception as e:
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            raw_json = f.read()
                        fixed_json = generate_postman_v22_again(raw_json)
                        parsed = json.loads(fixed_json)
                        validate_as_v22_but_save_as_v21(parsed)
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(json.dumps(parsed, indent=2))
                        valid_files.append(file)
                        st.info(f"{file} was fixed and validated on retry.")
                    except Exception as e2:
                        invalid_files.append((file, f"Initial error: {e}; Retry error: {e2}"))

            if not valid_files:
                st.error("No valid collections were generated.")
                for fname, err in invalid_files:
                    st.info(f"{fname}: {err}")
            else:
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for file in valid_files:
                        file_path = os.path.join(converted_dir, file)
                        zipf.write(file_path, arcname=file)
                zip_buffer.seek(0)

                st.markdown("---")
                st.success(f"Converted {len(valid_files)} collections successfully.")
                if invalid_files:
                    st.warning(f"{len(invalid_files)} file(s) had issues.")
                    for fname, err in invalid_files:
                        st.info(f"{fname}: {err}")
                st.download_button(
                    label="Download Converted Collections (.zip)",
                    data=zip_buffer,
                    file_name="converted_postman_jsons.zip",
                    mime="application/zip"
                )
